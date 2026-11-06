from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    language: Mapped[str] = mapped_column(String(8), default="ru")
    # балансы по валютам
    balance_ton: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    balance_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    balance_stars: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    held_ton: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    # реквизиты пользователя
    ton_wallet: Mapped[str] = mapped_column(String(128), default="")
    card_sbp: Mapped[str] = mapped_column(String(128), default="")  # legacy, no longer used
    alipay_account: Mapped[str] = mapped_column(String(128), default="")
    wechat_account: Mapped[str] = mapped_column(String(128), default="")
    unionpay_details: Mapped[str] = mapped_column(String(256), default="")
    fake_success_deals: Mapped[int] = mapped_column(Integer, default=0)  # накрутка успешных сделок


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    buyer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    deal_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(16), default="GRAM")  # GRAM | CNY | Stars
    pay_method: Mapped[str] = mapped_column(String(16), default="gram")  # gram | alipay | wechat | unionpay | stars
    description: Mapped[str] = mapped_column(Text, default="")
    gift_link: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(24), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LedgerEntry(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TopUpRequest(Base):
    __tablename__ = "topup_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    method: Mapped[str] = mapped_column(String(16), default="ton")  # ton | stars
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
