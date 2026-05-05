from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from models import Base
import config

engine = create_engine(
    config.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False}
)


def _ensure_schema():
    """
    Лёгкая миграция для SQLite: добавляет недостающие колонки в существующую
    таблицу `doctors`, если БД была создана до появления Excel-фичи.
    Новые таблицы (`bonus_uploads`, `bonus_entries`) создаст Base.metadata.create_all.
    """
    insp = inspect(engine)
    if 'doctors' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('doctors')}
    additions = []
    if 'specialty' not in existing:
        additions.append("ALTER TABLE doctors ADD COLUMN specialty VARCHAR")
    if 'yearly_bonus' not in existing:
        additions.append("ALTER TABLE doctors ADD COLUMN yearly_bonus FLOAT DEFAULT 0.0")
    if not additions:
        return
    with engine.begin() as conn:
        for sql in additions:
            conn.execute(text(sql))


Base.metadata.create_all(engine)
_ensure_schema()

SessionLocal = sessionmaker(bind=engine)


def get_session():
    return SessionLocal()
