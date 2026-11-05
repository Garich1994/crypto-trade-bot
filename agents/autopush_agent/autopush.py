"""Autopush Agent (Signal Watcher) — автономный пуш наблюдений.

По кругу прогоняет coordinator.build_brief() и пушит только сильные наблюдения:
сила сетапа ≥ 75 И риск-гейт PASS (идея не отсеяна) И скептик без FAIL.
Антиспам: пара+сторона не чаще раза в 12ч, максимум 5 пушей в сутки на
пользователя, тихий час 00:00–07:00 America/New_York. Это наблюдения, не советы.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.coordinator_agent import build_brief

try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 — на всякий случай фиксированный сдвиг
    _NY = timezone(timedelta(hours=-5))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _PROJECT_ROOT / "data" / "journal.db"
_db_path: Path = _DEFAULT_DB

PUSH_SCORE_MIN = 75          # минимальная сила сетапа
_ANTISPAM_HOURS = 12         # одна пара+сторона не чаще раза в 12ч
_DAILY_CAP = 5               # максимум пушей в сутки на пользователя
_QUIET_START, _QUIET_END = 0, 7   # тихий час 00:00–07:00 NY

_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (
    user_id      INTEGER PRIMARY KEY,
    interval_h   REAL    NOT NULL,
    active       INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS push_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    pair      TEXT    NOT NULL,
    direction TEXT    NOT NULL,
    pushed_at TEXT    NOT NULL
);
"""


class AutopushError(Exception):
    """Понятная наружу ошибка автопуша."""


def set_db_path(path) -> None:
    global _db_path
    _db_path = Path(path)


def _connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------- подписки

def set_autopush(user_id: int, active: bool, interval_h: float = 2) -> dict:
    """Включить/выключить автопуш. По умолчанию выключен (включает пользователь)."""
    try:
        interval_h = float(interval_h)
    except (TypeError, ValueError):
        raise AutopushError("Интервал должен быть числом часов.")
    if interval_h < 1:
        raise AutopushError("Интервал должен быть не меньше 1 часа.")
    with _connect() as con:
        con.execute(
            "INSERT INTO push_subs (user_id, interval_h, active, last_sent_at) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(user_id) DO UPDATE SET interval_h=excluded.interval_h, "
            "active=excluded.active",
            (int(user_id), interval_h, 1 if active else 0),
        )
    return {"user_id": int(user_id), "active": bool(active), "interval_h": interval_h}


def get_autopush(user_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM push_subs WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return dict(row) if row else None


def mark_sent(user_id: int, when: datetime | None = None) -> None:
    when = when or _now()
    with _connect() as con:
        con.execute(
            "UPDATE push_subs SET last_sent_at = ? WHERE user_id = ?",
            (when.isoformat(), int(user_id)),
        )


def due_subscriptions(now: datetime | None = None) -> list[int]:
    now = now or _now()
    due: list[int] = []
    with _connect() as con:
        rows = con.execute("SELECT * FROM push_subs WHERE active = 1").fetchall()
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


# ----------------------------------------------------------- антиспам/тихий час

def _is_quiet_hour(now: datetime) -> bool:
    hour = now.astimezone(_NY).hour
    return _QUIET_START <= hour < _QUIET_END


def _recent_push(con, user_id: int, pair: str, direction: str, now: datetime) -> bool:
    cutoff = (now - timedelta(hours=_ANTISPAM_HOURS)).isoformat()
    row = con.execute(
        "SELECT 1 FROM push_log WHERE user_id=? AND pair=? AND direction=? "
        "AND pushed_at >= ? LIMIT 1",
        (int(user_id), pair.upper(), direction, cutoff),
    ).fetchone()
    return row is not None


def _daily_count(con, user_id: int, now: datetime) -> int:
    cutoff = (now - timedelta(hours=24)).isoformat()
    row = con.execute(
        "SELECT COUNT(*) FROM push_log WHERE user_id=? AND pushed_at >= ?",
        (int(user_id), cutoff),
    ).fetchone()
    return int(row[0])


def _record_push(con, user_id: int, pair: str, direction: str, now: datetime) -> None:
    con.execute(
        "INSERT INTO push_log (user_id, pair, direction, pushed_at) VALUES (?, ?, ?, ?)",
        (int(user_id), pair.upper(), direction, now.isoformat()),
    )


def _idea_qualifies(idea: dict) -> bool:
    score = idea.get("setup_score")
    if score is None:
        score = idea.get("score", 0)
    if score is None or score < PUSH_SCORE_MIN:
        return False
    # скептик без FAIL (все проверки пройдены)
    if idea.get("skeptic_passed") != idea.get("skeptic_total"):
        return False
    return True


def evaluate_push(brief: dict, user_id: int, now: datetime | None = None) -> list[dict]:
    """Отобрать и записать пуши для пользователя из брифа (с учётом всех правил).

    Идеи в brief['ideas'] уже прошли риск-гейт координатора (FAIL ушли в
    filtered_out). Возвращает список идей, которые реально нужно запушить.
    """
    now = now or _now()
    if _is_quiet_hour(now):
        return []

    pushes: list[dict] = []
    with _connect() as con:
        sent_today = _daily_count(con, user_id, now)
        for idea in brief.get("ideas", []):
            if sent_today + len(pushes) >= _DAILY_CAP:
                break
            if not _idea_qualifies(idea):
                continue
            pair, direction = str(idea["symbol"]).upper(), idea["side"]
            if _recent_push(con, user_id, pair, direction, now):
                continue
            _record_push(con, user_id, pair, direction, now)
            pushes.append(idea)
    return pushes


async def run_push_cycle(builder=None, now: datetime | None = None) -> list[dict]:
    """Прогнать цикл автопуша по всем due-подписчикам. Возвращает события пуша."""
    builder = builder or build_brief
    now = now or _now()
    due = due_subscriptions(now)
    if not due:
        return []

    brief = await builder(top=5)
    events: list[dict] = []
    for user_id in due:
        for idea in evaluate_push(brief, user_id, now):
            events.append({"user_id": user_id, "idea": idea})
        mark_sent(user_id, now)
    return events
