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
    """SQLite-only мини-миграция: добавляет недостающие колонки.
    Новые таблицы (`bonus_uploads`, `bonus_entries`, `bonus_acks`,
    `feedback_messages`) создаёт Base.metadata.create_all."""
    insp = inspect(engine)
    table_names = set(insp.get_table_names())
    additions = []

    if 'doctors' in table_names:
        existing = {c['name'] for c in insp.get_columns('doctors')}
        if 'specialty' not in existing:
            additions.append("ALTER TABLE doctors ADD COLUMN specialty VARCHAR")
        if 'yearly_bonus' not in existing:
            additions.append("ALTER TABLE doctors ADD COLUMN yearly_bonus FLOAT DEFAULT 0.0")

    if 'bonus_acks' in table_names:
        existing = {c['name'] for c in insp.get_columns('bonus_acks')}
        if 'message_id' not in existing:
            additions.append("ALTER TABLE bonus_acks ADD COLUMN message_id INTEGER")

    if not additions:
        return
    with engine.begin() as conn:
        for sql in additions:
            conn.execute(text(sql))


Base.metadata.create_all(engine)
_ensure_schema()


def _reconcile_orphans():
    """Mark any 'running' broadcasts/surveys as 'cancelled' on bot restart."""
    insp = inspect(engine)
    table_names = insp.get_table_names()
    with engine.begin() as conn:
        if 'broadcast_history' in table_names:
            conn.execute(text("""
                UPDATE broadcast_history
                SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
            """))
        if 'surveys' in table_names:
            conn.execute(text("""
                UPDATE surveys
                SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
            """))
        # NEW: cancel in-progress doctor responses so they don't become stuck
        if 'survey_responses' in table_names:
            conn.execute(text("""
                UPDATE survey_responses
                SET status = 'cancelled'
                WHERE status = 'in_progress'
            """))


_reconcile_orphans()

SessionLocal = sessionmaker(bind=engine)


def get_session():
    return SessionLocal()
