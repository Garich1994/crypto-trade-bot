"""Paper Executor Agent — строго ВИРТУАЛЬНЫЙ портфель.

Никаких реальных ордеров, никаких ключей биржи. Цена входа/выхода берётся из
публичного тикера Binance, но все сделки — бумажные (paper). Плечо 1x: на одну
сделку нельзя задействовать больше свободного баланса.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agents.data_agent import get_ticker_24h

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _PROJECT_ROOT / "data" / "journal.db"
_db_path: Path = _DEFAULT_DB

START_BALANCE = 10_000.0  # стартовый виртуальный баланс, USDT
_SIDES = {"long", "short"}
_SIDE_ALIASES = {"лонг": "long", "шорт": "short"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_balance (
    user_id      INTEGER PRIMARY KEY,
    balance_usdt REAL    NOT NULL,
    updated_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    qty         REAL    NOT NULL,
    stop        REAL,
    take        REAL,
    status      TEXT    NOT NULL DEFAULT 'open',
    opened_at   TEXT    NOT NULL,
    closed_at   TEXT,
    pnl_usdt    REAL
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    qty         REAL    NOT NULL,
    limit_price REAL    NOT NULL,
    stop        REAL,
    take        REAL,
    status      TEXT    NOT NULL DEFAULT 'open',
    created_at  TEXT    NOT NULL,
    filled_at   TEXT
);
"""


class PaperError(Exception):
    """Понятная наружу ошибка виртуального портфеля."""


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
    con.executescript(_SCHEMA)
    return con


def _normalize_side(side: str) -> str:
    s = str(side).strip().lower()
    s = _SIDE_ALIASES.get(s, s)
    if s not in _SIDES:
        raise PaperError("Сторона должна быть long или short.")
    return s


def _ensure_balance(con: sqlite3.Connection, user_id: int) -> float:
    row = con.execute(
        "SELECT balance_usdt FROM paper_balance WHERE user_id = ?", (int(user_id),)
    ).fetchone()
    if row is None:
        con.execute(
            "INSERT INTO paper_balance (user_id, balance_usdt, updated_at) "
            "VALUES (?, ?, ?)",
            (int(user_id), START_BALANCE, _now()),
        )
        return START_BALANCE
    return float(row["balance_usdt"])


def _set_balance(con: sqlite3.Connection, user_id: int, balance: float) -> None:
    con.execute(
        "UPDATE paper_balance SET balance_usdt = ?, updated_at = ? WHERE user_id = ?",
        (balance, _now(), int(user_id)),
    )


def get_balance(user_id: int) -> float:
    with _connect() as con:
        return _ensure_balance(con, user_id)


def open_positions(user_id: int) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM paper_positions WHERE user_id = ? AND status = 'open' "
            "ORDER BY id",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_position(user_id: int, position_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM paper_positions WHERE id = ? AND user_id = ?",
            (int(position_id), int(user_id)),
        ).fetchone()
    return dict(row) if row else None


def portfolio(user_id: int) -> dict:
    """Свободный баланс и открытые виртуальные позиции."""
    return {"balance": get_balance(user_id), "positions": open_positions(user_id)}


def _validate_levels(side: str, price: float, stop, take) -> None:
    if stop is not None:
        if side == "long" and not stop < price:
            raise PaperError("Для лонга стоп должен быть ниже цены входа.")
        if side == "short" and not stop > price:
            raise PaperError("Для шорта стоп должен быть выше цены входа.")
    if take is not None:
        if side == "long" and not take > price:
            raise PaperError("Для лонга тейк должен быть выше цены входа.")
        if side == "short" and not take < price:
            raise PaperError("Для шорта тейк должен быть ниже цены входа.")


def open_position(
    user_id: int,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    stop: float | None = None,
    take: float | None = None,
) -> dict:
    """Открыть виртуальную позицию по цене `price`. Плечо 1x.

    Резервирует номинал (qty*price) из свободного баланса.
    """
    side = _normalize_side(side)
    symbol = symbol.upper().strip()
    try:
        qty = float(qty)
        price = float(price)
        stop = float(stop) if stop is not None else None
        take = float(take) if take is not None else None
    except (TypeError, ValueError):
        raise PaperError("Количество, цена, стоп и тейк должны быть числами.")

    if qty <= 0 or price <= 0:
        raise PaperError("Количество и цена должны быть больше нуля.")
    _validate_levels(side, price, stop, take)

    notional = qty * price
    with _connect() as con:
        balance = _ensure_balance(con, user_id)
        if notional > balance + 1e-9:
            raise PaperError(
                f"Недостаточно виртуального баланса: нужно {notional:.2f} USDT, "
                f"свободно {balance:.2f} USDT (плечо 1x, не более 100% на сделку)."
            )
        _set_balance(con, user_id, balance - notional)
        cur = con.execute(
            "INSERT INTO paper_positions "
            "(user_id, symbol, side, entry_price, qty, stop, take, status, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (int(user_id), symbol, side, price, qty, stop, take, _now()),
        )
        position_id = int(cur.lastrowid)
        balance_after = balance - notional

    return {
        "id": position_id,
        "symbol": symbol,
        "side": side,
        "entry_price": price,
        "qty": qty,
        "stop": stop,
        "take": take,
        "notional": notional,
        "balance_after": balance_after,
    }


def _close_row(user_id: int, position_id: int, exit_price: float, reason: str) -> dict:
    exit_price = float(exit_price)
    if exit_price <= 0:
        raise PaperError("Цена выхода должна быть больше нуля.")

    with _connect() as con:
        row = con.execute(
            "SELECT * FROM paper_positions WHERE id = ? AND user_id = ?",
            (int(position_id), int(user_id)),
        ).fetchone()
        if row is None:
            raise PaperError(
                f"Позиция №{position_id} не найдена (или принадлежит другому пользователю)."
            )
        if row["status"] == "closed":
            raise PaperError(f"Позиция №{position_id} уже закрыта.")

        side = row["side"]
        qty = float(row["qty"])
        entry = float(row["entry_price"])
        direction = 1.0 if side == "long" else -1.0
        notional = entry * qty
        pnl = (exit_price - entry) * qty * direction
        pnl_pct = (pnl / notional * 100) if notional > 0 else 0.0

        balance = _ensure_balance(con, user_id)
        _set_balance(con, user_id, balance + notional + pnl)
        con.execute(
            "UPDATE paper_positions SET status='closed', closed_at=?, pnl_usdt=? "
            "WHERE id=? AND user_id=?",
            (_now(), pnl, int(position_id), int(user_id)),
        )
        balance_after = balance + notional + pnl

    return {
        "id": int(position_id),
        "symbol": row["symbol"],
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "qty": qty,
        "pnl_usdt": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "balance_after": balance_after,
    }


def close_position(user_id: int, position_id: int, exit_price: float) -> dict:
    """Закрыть виртуальную позицию по цене выхода. Возвращает PnL."""
    return _close_row(user_id, position_id, exit_price, reason="manual")


def reset(user_id: int) -> float:
    """Сбросить портфель пользователя к стартовому балансу, убрать позиции."""
    with _connect() as con:
        con.execute("DELETE FROM paper_positions WHERE user_id = ?", (int(user_id),))
        _ensure_balance(con, user_id)
        _set_balance(con, user_id, START_BALANCE)
    return START_BALANCE


def _all_open_with_levels() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM paper_positions WHERE status = 'open' "
            "AND (stop IS NOT NULL OR take IS NOT NULL) ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


async def _default_price(symbol: str) -> float:
    ticker = await get_ticker_24h(symbol)
    return float(ticker["lastPrice"])


def _hit_level(pos: dict, price: float) -> tuple[float, str] | None:
    """Вернуть (цена_выхода, причина) если задет стоп/тейк, иначе None."""
    side, stop, take = pos["side"], pos["stop"], pos["take"]
    if side == "long":
        if stop is not None and price <= stop:
            return stop, "stop"
        if take is not None and price >= take:
            return take, "take"
    else:  # short
        if stop is not None and price >= stop:
            return stop, "stop"
        if take is not None and price <= take:
            return take, "take"
    return None


# ----------------------------------------------------------- лимитные ордера

def _long_qty(con, user_id: int, symbol: str) -> float:
    rows = con.execute(
        "SELECT qty FROM paper_positions WHERE user_id=? AND symbol=? "
        "AND side='long' AND status='open'",
        (int(user_id), symbol.upper()),
    ).fetchall()
    return sum(float(r["qty"]) for r in rows)


def add_limit_order(
    user_id: int,
    symbol: str,
    side: str,
    qty: float,
    limit_price: float,
    stop: float | None = None,
    take: float | None = None,
) -> dict:
    """Поставить локальный лимитный ордер (paper). Баланс не трогается до исполнения."""
    symbol = symbol.upper().strip()
    side = str(side).strip().lower()
    if side not in ("buy", "sell"):
        raise PaperError("Сторона должна быть buy или sell.")
    try:
        qty = float(qty)
        limit_price = float(limit_price)
        stop = float(stop) if stop is not None else None
        take = float(take) if take is not None else None
    except (TypeError, ValueError):
        raise PaperError("Количество и цены должны быть числами.")
    if qty <= 0 or limit_price <= 0:
        raise PaperError("Количество и цена должны быть больше нуля.")

    with _connect() as con:
        if side == "sell":
            # Спот-семантика: продать можно только имеющийся базовый актив (лонг).
            held = _long_qty(con, user_id, symbol)
            if held + 1e-12 < qty:
                raise PaperError(
                    f"Недостаточно базового актива: в наличии {held}, нужно {qty} "
                    f"(сначала купи {symbol})."
                )
        cur = con.execute(
            "INSERT INTO paper_orders "
            "(user_id, symbol, side, qty, limit_price, stop, take, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (int(user_id), symbol, side, qty, limit_price, stop, take, _now()),
        )
        order_id = int(cur.lastrowid)

    return {"orderId": order_id, "symbol": symbol, "side": side, "qty": qty,
            "limit_price": limit_price, "stop": stop, "take": take}


def open_limit_orders(user_id: int) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM paper_orders WHERE user_id=? AND status='open' ORDER BY id",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_limit_order(user_id: int, order_id: int) -> bool:
    with _connect() as con:
        cur = con.execute(
            "UPDATE paper_orders SET status='cancelled' "
            "WHERE id=? AND user_id=? AND status='open'",
            (int(order_id), int(user_id)),
        )
        if cur.rowcount == 0:
            raise PaperError(
                f"Лимитный ордер №{order_id} не найден или уже исполнен/отменён."
            )
    return True


def _all_open_orders() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM paper_orders WHERE status='open' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _mark_order_filled(order_id: int) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE paper_orders SET status='filled', filled_at=? WHERE id=?",
            (_now(), int(order_id)),
        )


def _oldest_long(user_id: int, symbol: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM paper_positions WHERE user_id=? AND symbol=? "
            "AND side='long' AND status='open' ORDER BY id LIMIT 1",
            (int(user_id), symbol.upper()),
        ).fetchone()
    return dict(row) if row else None


async def run_limit_check(fetcher=None) -> list[dict]:
    """Исполнить лимитные ордера (paper), чья цена задета текущим рынком.

    buy — при цене ≤ лимита (открывает лонг); sell — при цене ≥ лимита
    (закрывает старейший лонг по лимитной цене). Возвращает события исполнения.
    """
    fetcher = fetcher or _default_price
    orders = _all_open_orders()

    prices: dict[str, float] = {}
    events: list[dict] = []
    for o in orders:
        symbol = o["symbol"]
        if symbol not in prices:
            try:
                prices[symbol] = await fetcher(symbol)
            except Exception:  # noqa: BLE001
                continue
        price = prices.get(symbol)
        if price is None:
            continue
        limit = float(o["limit_price"])
        side = o["side"]

        if side == "buy" and price <= limit:
            pos = open_position(o["user_id"], symbol, "long", float(o["qty"]),
                                limit, o["stop"], o["take"])
            _mark_order_filled(o["id"])
            events.append({"user_id": o["user_id"], "orderId": o["id"], "symbol": symbol,
                           "side": "buy", "fill_price": limit, "qty": float(o["qty"]),
                           "kind": "limit_fill", "position": pos})
        elif side == "sell" and price >= limit:
            lp = _oldest_long(o["user_id"], symbol)
            if lp is None:
                cancel_limit_order(o["user_id"], o["id"])
                continue
            closed = _close_row(o["user_id"], lp["id"], limit, "limit_sell")
            _mark_order_filled(o["id"])
            events.append({"user_id": o["user_id"], "orderId": o["id"], "symbol": symbol,
                           "side": "sell", "fill_price": limit, "qty": float(o["qty"]),
                           "kind": "limit_fill", "closed": closed})
    return events


async def run_paper_check(fetcher=None) -> list[dict]:
    """Фоновая проверка: закрыть позиции, чьи стоп/тейк задеты текущей ценой.

    `fetcher(symbol)` — awaitable, по умолчанию текущий тикер Binance.
    Сбой данных по паре пропускается. Возвращает список событий автозакрытия.
    """
    from agents.data_agent import DataAgentError

    fetcher = fetcher or _default_price
    positions = _all_open_with_levels()

    prices: dict[str, float] = {}
    events: list[dict] = []
    for pos in positions:
        symbol = pos["symbol"]
        if symbol not in prices:
            try:
                prices[symbol] = await fetcher(symbol)
            except DataAgentError:
                continue
        price = prices.get(symbol)
        if price is None:
            continue
        hit = _hit_level(pos, price)
        if hit is None:
            continue
        exit_price, reason = hit
        try:
            event = _close_row(pos["user_id"], pos["id"], exit_price, reason)
        except PaperError:
            continue  # уже закрыта в этом же цикле
        event["user_id"] = pos["user_id"]
        events.append(event)
    return events
