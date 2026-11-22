from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from . import config

engine = create_engine(
    config.DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 既有表上的增量列（迭代升级时 create_all 不会补列，这里做幂等 ADD COLUMN）
_ADDED_COLUMNS = {
    "receive_log": {"scenario_id": "VARCHAR(64)"},
    "batch_items": {"scenario_id": "VARCHAR(64)"},
    "quality_conclusions": {"scenario_id": "VARCHAR(64)"},
    "batches": {"scenario_id": "VARCHAR(64)", "create_event_seq": "BIGINT"},
    "policies": {"effective_ts": "TIMESTAMP", "event_seq": "BIGINT"},
}


def ensure_schema() -> None:
    """幂等升级：补列 + 建索引。仅添加，不删除、不改写既有数据。"""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_receive_log_scenario ON receive_log (scenario_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_batches_scenario ON batches (scenario_id)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_review_runs_lane_scope "
                "ON review_runs (lane, scope_kind, scope_key, version)"
            )
        )
