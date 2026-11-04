"""Подписки на автодайджест (таблица brief_subs в data/journal.db)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _PROJECT_ROOT / "data" / "journal.db"
_db_path: Path = _DEFAULT_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS brief_subs (
    user_id      INTEGER PRIMARY KEY,
    interval_h   REAL    NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    last_sent_at TEXT
);
"""


class AutobriefError(Exception):
    """Понятная наружу ошибка подписки на автодайджест."""


def set_db_path(path) -> None:
    global _db_path
    _db_path = Path(path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_db_path)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA)
    return con


def set_autobrief(user_id: int, active: bool, interval_h: float = 4) -> dict:
    """Включить/выключить автодайджест для пользователя."""
    try:
        interval_h = float(interval_h)
    except (TypeError, ValueError):
        raise AutobriefError("Интервал должен быть числом часов.")
    if interval_h < 1:
        raise AutobriefError("Интервал должен быть не меньше 1 часа.")

    with _connect() as con:
        con.execute(
            "INSERT INTO brief_subs (user_id, interval_h, active, last_sent_at) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(user_id) DO UPDATE SET interval_h=excluded.interval_h, "
            "active=excluded.active",
            (int(user_id), interval_h, 1 if active else 0),
        )
    return {"user_id": int(user_id), "active": bool(active), "interval_h": interval_h}


def get_autobrief(user_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM brief_subs WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return dict(row) if row else None


def mark_brief_sent(user_id: int, when: datetime | None = None) -> None:
    when = when or _now()
    with _connect() as con:
        con.execute(
            "UPDATE brief_subs SET last_sent_at = ? WHERE user_id = ?",
            (when.isoformat(), int(user_id)),
        )


def due_subscriptions(now: datetime | None = None) -> list[int]:
    """Пользователи, которым пора прислать автодайджест."""
    now = now or _now()
    due: list[int] = []
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM brief_subs WHERE active = 1"
        ).fetchall()
    for row in rows:
        last = row["last_sent_at"]
        if last is None:
            due.append(int(row["user_id"]))
            continue
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            due.append(int(row["user_id"]))
            continue
        if now - last_dt >= timedelta(hours=float(row["interval_h"])):
            due.append(int(row["user_id"]))
    return due
