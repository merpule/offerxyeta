from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from models import Base

DATABASE_URL = "sqlite+aiosqlite:///./otc_elf.db"
engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # авто-добавление новых колонок в уже существующую SQLite БД
        migrations = [
            ("users", "language", "VARCHAR(8) DEFAULT 'ru'"),
            ("users", "balance_ton", "NUMERIC(18, 4) DEFAULT 0"),
            ("users", "balance_rub", "NUMERIC(18, 2) DEFAULT 0"),
            ("users", "balance_stars", "NUMERIC(18, 2) DEFAULT 0"),
            ("users", "held_ton", "NUMERIC(18, 4) DEFAULT 0"),
            ("users", "ton_wallet", "VARCHAR(128) DEFAULT ''"),
            ("users", "card_sbp", "VARCHAR(128) DEFAULT ''"),
            ("users", "fake_success_deals", "INTEGER DEFAULT 0"),
            ("deals", "pay_method", "VARCHAR(16) DEFAULT 'ton'"),
            ("deals", "gift_link", "VARCHAR(512) DEFAULT ''"),
            ("topup_requests", "method", "VARCHAR(16) DEFAULT 'ton'"),
        ]
        for table, column, coltype in migrations:
            try:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                )
            except Exception:
                # колонка уже есть — ок
                pass
