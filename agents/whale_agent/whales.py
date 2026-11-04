"""Whale Tracker Agent — крупные сделки на споте Binance (aggTrades).

Необычная активность как факт прошлого, не сигнал. Ликвидации фьючерсов из
региона недоступны (451) — честная заглушка, данные не выдумываем.
"""
from __future__ import annotations

import time

from agents.data_agent import get_agg_trades

DEFAULT_WINDOW_MIN = 15
_BTC_THRESHOLD = 250_000.0
_OTHER_THRESHOLD = 100_000.0

LIQUIDATIONS_NOTE = (
    "📉 Ликвидации фьючерсов недоступны из региона (Binance 451). "
    "Доступен только спот. Данные не выдумываются."
)


def default_threshold(symbol: str) -> float:
    """Порог «кита» по паре: BTC — 250k, остальные — 100k USDT."""
    return _BTC_THRESHOLD if symbol.upper().startswith("BTC") else _OTHER_THRESHOLD


def _trade_side(trade: dict) -> str:
    # m == True -> покупатель мейкер -> агрессивная продажа; иначе покупка.
    return "sell" if trade.get("m") else "buy"


def analyze_whales(
    trades: list[dict],
    symbol: str,
    threshold: float | None = None,
    window_min: int = DEFAULT_WINDOW_MIN,
    now_ms: int | None = None,
) -> dict:
    """Разобрать список aggTrades. Чистая функция (тестируется на мок-данных)."""
    symbol = symbol.upper().strip()
    if threshold is None:
        threshold = default_threshold(symbol)
    threshold = float(threshold)

    if now_ms is None:
        times = [int(t["T"]) for t in trades if "T" in t]
        now_ms = max(times) if times else int(time.time() * 1000)
    cutoff = now_ms - window_min * 60_000

    whales: list[dict] = []
    span_times: list[int] = []
    for t in trades:
        ts = int(t.get("T", now_ms))
        if ts < cutoff:
            continue
        span_times.append(ts)
        notional = float(t["p"]) * float(t["q"])
        if notional >= threshold:
            whales.append(
                {
                    "price": float(t["p"]),
                    "qty": float(t["q"]),
                    "notional": notional,
                    "side": _trade_side(t),
                    "time": ts,
                }
            )

    buy_notional = sum(w["notional"] for w in whales if w["side"] == "buy")
    sell_notional = sum(w["notional"] for w in whales if w["side"] == "sell")

    if not whales:
        pressure = "нет китовых сделок"
    elif buy_notional > sell_notional * 1.5:
        pressure = "покупки"
    elif sell_notional > buy_notional * 1.5:
        pressure = "продажи"
    else:
        pressure = "смешанное"

    top = sorted(whales, key=lambda w: w["notional"], reverse=True)[:3]
    window_minutes = (
        (max(span_times) - min(span_times)) / 60_000 if len(span_times) >= 2 else 0.0
    )

    return {
        "symbol": symbol,
        "threshold": threshold,
        "window_minutes": window_minutes,
        "whale_count": len(whales),
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "pressure": pressure,
        "top": top,
    }


async def get_whales(
    symbol: str,
    threshold: float | None = None,
    window_min: int = DEFAULT_WINDOW_MIN,
) -> dict:
    """Скачать последние aggTrades и разобрать китовые сделки.

    Бросает DataAgentError при недоступности данных.
    """
    symbol = symbol.upper().strip()
    trades = await get_agg_trades(symbol, limit=1000)
    return analyze_whales(trades, symbol, threshold, window_min)
