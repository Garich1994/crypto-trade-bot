"""Alerts Agent (Сторож) — фоновые уведомления о срабатывании условий.

Подписки одноразовые: сработав, становятся 'fired'. Уведомление — это факт о
цене/индикаторе, а не рекомендация. Данные берутся у Data Agent (кэш TTL 60с),
живые котировки не гарантируются.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from agents.data_agent import DataAgentError, get_klines, get_ticker_24h
from agents.ta_agent import indicators as ind

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB = _PROJECT_ROOT / "data" / "journal.db"
_db_path: Path = _DEFAULT_DB

#: Поддерживаемые типы условий.
CONDITION_TYPES = (
    "price_above",
    "price_below",
    "rsi_above",
    "rsi_below",
    "change24h_abs",
    "volume_spike",
)
_NEEDS_KLINES = {"rsi_above", "rsi_below", "volume_spike"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    symbol         TEXT    NOT NULL,
    condition_type TEXT    NOT NULL,
    threshold      REAL    NOT NULL,
    direction      TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'active',
    created_at     TEXT    NOT NULL,
    fired_at       TEXT
);
"""

_DIRECTION = {
    "price_above": "above",
    "price_below": "below",
    "rsi_above": "above",
    "rsi_below": "below",
    "change24h_abs": "abs",
    "volume_spike": "spike",
}


class AlertError(Exception):
    """Понятная наружу ошибка (валидация, нет подписки и т.п.)."""


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


def _validate(condition_type: str, threshold: float) -> float:
    if condition_type not in CONDITION_TYPES:
        raise AlertError(
            "Неизвестный тип условия. Доступны: " + ", ".join(CONDITION_TYPES) + "."
        )
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise AlertError("Порог должен быть числом.")

    if condition_type in ("price_above", "price_below") and threshold <= 0:
        raise AlertError("Цена-порог должна быть больше нуля.")
    if condition_type in ("rsi_above", "rsi_below") and not (0 <= threshold <= 100):
        raise AlertError("Порог RSI должен быть в диапазоне [0, 100].")
    if condition_type in ("change24h_abs", "volume_spike") and threshold <= 0:
        raise AlertError("Порог должен быть больше нуля.")
    return threshold


def add_alert(user_id: int, symbol: str, condition_type: str, threshold: float) -> dict:
    """Создать активную подписку, вернуть её данные (id, symbol, type, threshold)."""
    condition_type = str(condition_type).strip().lower()
    threshold = _validate(condition_type, threshold)
    symbol = symbol.upper().strip()

    with _connect() as con:
        cur = con.execute(
            "INSERT INTO alerts "
            "(user_id, symbol, condition_type, threshold, direction, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (int(user_id), symbol, condition_type, threshold,
             _DIRECTION[condition_type], _now()),
        )
        alert_id = int(cur.lastrowid)

    return {
        "id": alert_id,
        "symbol": symbol,
        "condition_type": condition_type,
        "threshold": threshold,
    }


def list_alerts(user_id: int) -> list[dict]:
    """Активные подписки пользователя."""
    with _connect() as con:
        rows = con.execute(
            "SELECT id, symbol, condition_type, threshold, created_at "
            "FROM alerts WHERE user_id = ? AND status = 'active' ORDER BY id",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_alert(user_id: int, alert_id: int) -> bool:
    """Отменить активную подписку пользователя. Чужая/несуществующая — ошибка."""
    with _connect() as con:
        cur = con.execute(
            "DELETE FROM alerts WHERE id = ? AND user_id = ? AND status = 'active'",
            (int(alert_id), int(user_id)),
        )
        if cur.rowcount == 0:
            raise AlertError(
                f"Подписка №{alert_id} не найдена (или принадлежит другому пользователю)."
            )
    return True


def evaluate_condition(
    condition_type: str, threshold: float, metrics: dict
) -> tuple[bool, float | None]:
    """Проверить условие по метрикам. Возвращает (сработало, фактическое значение)."""
    if condition_type == "price_above":
        a = metrics.get("price")
        return (a is not None and a > threshold), a
    if condition_type == "price_below":
        a = metrics.get("price")
        return (a is not None and a < threshold), a
    if condition_type == "rsi_above":
        a = metrics.get("rsi")
        return (a is not None and a > threshold), a
    if condition_type == "rsi_below":
        a = metrics.get("rsi")
        return (a is not None and a < threshold), a
    if condition_type == "change24h_abs":
        ch = metrics.get("change24h")
        a = abs(ch) if ch is not None else None
        return (a is not None and a >= threshold), a
    if condition_type == "volume_spike":
        a = metrics.get("volume_ratio")
        return (a is not None and a >= threshold), a
    return False, None


def _mark_fired(alert_id: int, actual: float | None) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE alerts SET status = 'fired', fired_at = ? WHERE id = ?",
            (_now(), int(alert_id)),
        )


def _all_active_alerts() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT id, user_id, symbol, condition_type, threshold "
            "FROM alerts WHERE status = 'active' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


async def _default_fetch_metrics(symbol: str, need_klines: bool) -> dict:
    """Собрать метрики по паре через Data Agent (кэш TTL 60с)."""
    ticker = await get_ticker_24h(symbol)
    metrics: dict = {}
    try:
        metrics["price"] = float(ticker.get("lastPrice"))
    except (TypeError, ValueError):
        metrics["price"] = None
    try:
        metrics["change24h"] = float(ticker.get("priceChangePercent"))
    except (TypeError, ValueError):
        metrics["change24h"] = None

    if need_klines:
        raw = await get_klines(symbol, "1h", limit=200)
        close = pd.to_numeric(pd.Series([c[4] for c in raw]), errors="coerce")
        volume = pd.to_numeric(pd.Series([c[5] for c in raw]), errors="coerce")
        metrics["rsi"] = float(ind.rsi(close, 14).iloc[-1])
        avg_vol = float(volume.tail(20).mean())
        metrics["volume_ratio"] = (
            float(volume.iloc[-1]) / avg_vol if avg_vol > 0 else 0.0
        )
    return metrics


async def run_check(fetcher=None) -> list[dict]:
    """Проверить все активные подписки и погасить сработавшие.

    `fetcher(symbol, need_klines)` — awaitable, возвращает метрики (по умолчанию
    реальные данные). Возвращает список событий сработавших условий.
    Сбой данных по паре пропускается (не выдумываем).
    """
    fetcher = fetcher or _default_fetch_metrics
    alerts = _all_active_alerts()

    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for a in alerts:
        by_symbol[a["symbol"]].append(a)

    events: list[dict] = []
    for symbol, group in by_symbol.items():
        need_klines = any(a["condition_type"] in _NEEDS_KLINES for a in group)
        try:
            metrics = await fetcher(symbol, need_klines)
        except DataAgentError:
            continue  # честно: нет данных — не трогаем подписку

        for a in group:
            fired, actual = evaluate_condition(
                a["condition_type"], a["threshold"], metrics
            )
            if fired:
                _mark_fired(a["id"], actual)
                events.append(
                    {
                        "user_id": a["user_id"],
                        "alert_id": a["id"],
                        "symbol": a["symbol"],
                        "condition_type": a["condition_type"],
                        "threshold": a["threshold"],
                        "actual": actual,
                    }
                )
    return events
