"""Journal Agent — журнал сделок на SQLite с изоляцией по user_id.

Хранит записи и считает статистику по прошлому. Ничего не советует.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _PROJECT_ROOT / "data" / "journal.db"
_db_path: Path = _DEFAULT_DB

_SIDES = {"long", "short"}
_SIDE_ALIASES = {"лонг": "long", "шорт": "short"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    symbol     TEXT    NOT NULL,
    side       TEXT    NOT NULL,
    entry      REAL    NOT NULL,
    stop       REAL    NOT NULL,
    take       REAL,
    qty        REAL    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open',
    exit_price REAL,
    pnl_usdt   REAL,
    pnl_r      REAL,
    opened_at  TEXT    NOT NULL,
    closed_at  TEXT
);
"""


class JournalError(Exception):
    """Понятная наружу ошибка журнала (валидация, нет сделки и т.п.)."""


def set_db_path(path) -> None:
    """Переопределить путь к файлу БД (для тестов)."""
    global _db_path
    _db_path = Path(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_db_path)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA)
    return con


def _normalize_side(side: str) -> str:
    s = str(side).strip().lower()
    s = _SIDE_ALIASES.get(s, s)
    if s not in _SIDES:
        raise JournalError("Сторона должна быть long или short.")
    return s


def add_trade(
    user_id: int,
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    take: float,
    qty: float,
) -> int:
    """Записать новую открытую сделку, вернуть её id."""
    side = _normalize_side(side)
    try:
        entry = float(entry)
        stop = float(stop)
        take = float(take)
        qty = float(qty)
    except (TypeError, ValueError):
        raise JournalError("Цены и объём должны быть числами.")

    if entry <= 0 or stop <= 0 or qty <= 0:
        raise JournalError("Вход, стоп и объём должны быть больше нуля.")
    if entry == stop:
        raise JournalError("Стоп не может совпадать со входом.")

    with _connect() as con:
        cur = con.execute(
            "INSERT INTO trades "
            "(user_id, symbol, side, entry, stop, take, qty, status, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (int(user_id), symbol.upper().strip(), side, entry, stop, take, qty, _now()),
        )
        return int(cur.lastrowid)


def close_trade(user_id: int, trade_id: int, exit_price: float) -> dict:
    """Закрыть сделку пользователя по цене выхода, вернуть PnL.

    Чужая сделка для пользователя «не найдена» (фильтр по user_id).
    """
    try:
        exit_price = float(exit_price)
    except (TypeError, ValueError):
        raise JournalError("Цена выхода должна быть числом.")
    if exit_price <= 0:
        raise JournalError("Цена выхода должна быть больше нуля.")

    with _connect() as con:
        row = con.execute(
            "SELECT * FROM trades WHERE id = ? AND user_id = ?",
            (int(trade_id), int(user_id)),
        ).fetchone()
        if row is None:
            raise JournalError(
                f"Сделка №{trade_id} не найдена (или принадлежит другому пользователю)."
            )
        if row["status"] == "closed":
            raise JournalError(f"Сделка №{trade_id} уже закрыта.")

        direction = 1.0 if row["side"] == "long" else -1.0
        qty = float(row["qty"])
        entry = float(row["entry"])
        stop = float(row["stop"])
        pnl_usdt = (exit_price - entry) * qty * direction
        risk = abs(entry - stop) * qty
        pnl_r = (pnl_usdt / risk) if risk > 0 else 0.0

        con.execute(
            "UPDATE trades SET status='closed', exit_price=?, pnl_usdt=?, pnl_r=?, "
            "closed_at=? WHERE id=? AND user_id=?",
            (exit_price, pnl_usdt, pnl_r, _now(), int(trade_id), int(user_id)),
        )

    return {
        "id": int(trade_id),
        "symbol": row["symbol"],
        "side": row["side"],
        "entry": entry,
        "exit_price": exit_price,
        "qty": qty,
        "pnl_usdt": pnl_usdt,
        "pnl_r": pnl_r,
    }


def open_trades(user_id: int) -> list[dict]:
    """Список открытых сделок пользователя."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE user_id = ? AND status = 'open' "
            "ORDER BY id",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def stats(user_id: int) -> dict:
    """Статистика по закрытым сделкам пользователя."""
    with _connect() as con:
        rows = con.execute(
            "SELECT pnl_usdt, pnl_r FROM trades "
            "WHERE user_id = ? AND status = 'closed'",
            (int(user_id),),
        ).fetchall()

    total = len(rows)
    if total == 0:
        return {
            "total": 0,
            "wins": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "expectancy": 0.0,
            "total_pnl_usdt": 0.0,
            "best_r": None,
            "worst_r": None,
        }

    rs = [float(r["pnl_r"]) for r in rows]
    pnls = [float(r["pnl_usdt"]) for r in rows]
    wins = sum(1 for v in pnls if v > 0)
    avg_r = sum(rs) / total

    return {
        "total": total,
        "wins": wins,
        "win_rate": wins / total,
        "avg_r": avg_r,
        "expectancy": avg_r,  # ожидаемый R на сделку = средний R
        "total_pnl_usdt": sum(pnls),
        "best_r": max(rs),
        "worst_r": min(rs),
    }
