"""数量事件账本持久化。

一次数量事件在一个 SQLite 写事务内同时落事件与余额：
- 事件编号（business_key + event_id）唯一，重试同一笔不会重复扣补。
- 找回必须携带预期版本，并发下过期版本被拒绝。
- 任一步失败整体回滚，原余额保留，恢复后可安全重试。
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import app.store as store
from app.config import TIMEZONE
from app.errors import DomainError

from .ledger import (
    PERFORMANCE_SOURCES,
    PHYSICAL_SOURCES,
    RECOVERY,
    LedgerState,
    apply_quantity_event,
    build_snapshot,
    format_quantity,
    validate_event,
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS quantity_state (
        business_key TEXT PRIMARY KEY,
        ordered TEXT NOT NULL,
        accepted TEXT NOT NULL,
        returned TEXT NOT NULL,
        shrinkage TEXT NOT NULL,
        recovered TEXT NOT NULL,
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS quantity_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        business_key TEXT NOT NULL,
        event_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        quantity TEXT NOT NULL,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (business_key, event_id)
    )""",
)


def init_quantity_db() -> None:
    store.init_db()
    with store.connect() as db:
        for statement in SCHEMA:
            db.execute(statement)


def _now() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds")


def _connect_writer():
    db = store.connect()
    db.isolation_level = None
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def _load_state(db, business_key: str) -> LedgerState:
    row = db.execute(
        "SELECT ordered, accepted, returned, shrinkage, recovered, version FROM quantity_state WHERE business_key = ?",
        (business_key,),
    ).fetchone()
    if row is None:
        return LedgerState()
    return LedgerState(
        ordered=Decimal(row[0]),
        accepted=Decimal(row[1]),
        returned=Decimal(row[2]),
        shrinkage=Decimal(row[3]),
        recovered=Decimal(row[4]),
        version=int(row[5]),
    )


def _insert_event(db, business_key: str, event, version: int, created_at: str) -> None:
    db.execute(
        "INSERT INTO quantity_events (business_key, event_id, kind, quantity, version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (business_key, event.event_id, event.kind, format_quantity(event.quantity), version, created_at),
    )


def _upsert_state(db, business_key: str, state: LedgerState, updated_at: str) -> None:
    db.execute(
        """INSERT INTO quantity_state (business_key, ordered, accepted, returned, shrinkage, recovered, version, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (business_key) DO UPDATE SET
             ordered = excluded.ordered,
             accepted = excluded.accepted,
             returned = excluded.returned,
             shrinkage = excluded.shrinkage,
             recovered = excluded.recovered,
             version = excluded.version,
             updated_at = excluded.updated_at""",
        (
            business_key,
            format_quantity(state.ordered),
            format_quantity(state.accepted),
            format_quantity(state.returned),
            format_quantity(state.shrinkage),
            format_quantity(state.recovered),
            state.version,
            updated_at,
        ),
    )


def _event_source(kind: str) -> str:
    in_performance = kind in PERFORMANCE_SOURCES
    in_physical = kind in PHYSICAL_SOURCES
    if in_performance and in_physical:
        return "both"
    return "performance" if in_performance else "physical"


def _event_rows(db, business_key: str) -> list[dict]:
    rows = db.execute(
        "SELECT event_id, kind, quantity, version, created_at FROM quantity_events WHERE business_key = ? ORDER BY seq",
        (business_key,),
    ).fetchall()
    return [
        {
            "event_id": row[0],
            "kind": row[1],
            "quantity": format_quantity(Decimal(row[2])),
            "version": int(row[3]),
            "created_at": row[4],
            "source": _event_source(row[1]),
        }
        for row in rows
    ]


def _read_snapshot(business_key: str) -> dict:
    with store.connect() as db:
        state = _load_state(db, business_key)
        events = _event_rows(db, business_key)
    return build_snapshot(business_key, state, events)


def _invalid_version(value) -> DomainError:
    return DomainError("INVALID_VERSION", f"预期版本必须是整数：{value}", 422)


def apply_event(
    business_key: str,
    event_id: str,
    kind: str,
    quantity,
    expected_version=None,
) -> dict:
    event = validate_event(event_id, business_key, kind, quantity)
    if event.kind == RECOVERY and expected_version in (None, ""):
        raise DomainError("VERSION_REQUIRED", "找回必须携带预期版本，以拒绝过期状态", 422)
    expected = None
    if expected_version not in (None, ""):
        try:
            expected = int(str(expected_version))
        except (TypeError, ValueError):
            raise _invalid_version(expected_version)
        if expected < 0:
            raise _invalid_version(expected_version)

    init_quantity_db()
    db = _connect_writer()
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT kind, quantity FROM quantity_events WHERE business_key = ? AND event_id = ?",
            (event.business_key, event.event_id),
        ).fetchone()
        if existing is not None:
            db.execute("ROLLBACK")
            same = existing[0] == event.kind and Decimal(existing[1]) == event.quantity
            if not same:
                raise DomainError(
                    "EVENT_ID_CONFLICT",
                    f"事件编号 {event.event_id} 已用于其他数量或类型",
                    409,
                )
            # 同一笔事件重放：既不重新扣补，也不要求版本仍是最新。
            return {"applied": False, "replayed": True, "snapshot": _read_snapshot(event.business_key)}

        state = _load_state(db, event.business_key)
        if expected is not None and expected != state.version:
            raise DomainError(
                "STALE_VERSION",
                f"预期版本 {expected} 已过期，当前版本为 {state.version}",
                409,
            )
        new_state = apply_quantity_event(state, event.kind, event.quantity)
        _insert_event(db, event.business_key, event, new_state.version, _now())
        _upsert_state(db, event.business_key, new_state, _now())
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        db.close()

    return {"applied": True, "replayed": False, "snapshot": _read_snapshot(event.business_key)}


def get_snapshot(business_key: str) -> dict | None:
    init_quantity_db()
    key = str(business_key or "").strip()
    if not key:
        raise DomainError("MISSING_BUSINESS_KEY", "业务键不能为空", 422)
    with store.connect() as db:
        exists = db.execute(
            "SELECT 1 FROM quantity_state WHERE business_key = ? UNION ALL SELECT 1 FROM quantity_events WHERE business_key = ? LIMIT 1",
            (key, key),
        ).fetchone()
        if exists is None:
            return None
    return _read_snapshot(key)


def list_snapshots() -> list[dict]:
    init_quantity_db()
    with store.connect() as db:
        keys = [
            row[0]
            for row in db.execute(
                "SELECT business_key FROM quantity_events GROUP BY business_key ORDER BY MIN(seq)"
            ).fetchall()
        ]
    return [_read_snapshot(key) for key in keys]
