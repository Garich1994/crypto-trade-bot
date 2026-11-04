"""Scanner Agent — обзор топовых USDT-пар и подсветка ситуаций.

Находит совпадения с простыми паттернами, ничего не предсказывает и не советует.
"""
from __future__ import annotations

import time

import pandas as pd

from agents.data_agent import DataAgentError, get_all_tickers_24h, get_klines
from agents.ta_agent import indicators as ind

DEFAULT_TIMEFRAME = "1h"
DEFAULT_TOP_N = 30
_CANDLES = 200           # достаточно для RSI и ориентира по EMA200
_TIME_BUDGET = 85.0      # мягкий дедлайн, сек
_VOL_WINDOW = 20

# Исключаем стейблкоины и leveraged-токены из вселенной.
_STABLE_BASES = {"USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI", "USDD", "EUR"}
_LEVERAGED_MARKERS = ("UP", "DOWN", "BULL", "BEAR")


def _is_tradable_usdt(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if not base or base in _STABLE_BASES:
        return False
    if any(base.endswith(m) for m in _LEVERAGED_MARKERS):
        return False
    return True


def _select_universe(tickers: list[dict], top_n: int) -> list[dict]:
    usdt = [t for t in tickers if _is_tradable_usdt(str(t.get("symbol", "")))]

    def _volume(t: dict) -> float:
        try:
            return float(t.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            return 0.0

    usdt.sort(key=_volume, reverse=True)
    return usdt[:top_n]


def _detect_patterns(
    rsi_now: float,
    rsi_prev: float,
    price: float,
    ema200: float,
    change_24h: float | None,
    vol_ratio: float,
) -> list[str]:
    patterns: list[str] = []
    if rsi_now < 30:
        patterns.append("перепродан RSI<30")
    if rsi_now > 70:
        patterns.append("перекуплен RSI>70")
    if change_24h is not None and change_24h > 8:
        patterns.append("рост 24ч >8%")
    if change_24h is not None and change_24h < -8:
        patterns.append("падение 24ч >8%")
    if vol_ratio >= 2:
        patterns.append("всплеск объёма x2+")
    if price > ema200 and rsi_now > rsi_prev:
        patterns.append("выше EMA200, RSI растёт")
    return patterns


async def _scan_pair(ticker: dict, timeframe: str) -> dict | None:
    symbol = str(ticker.get("symbol", "")).upper()
    try:
        raw = await get_klines(symbol, timeframe, limit=_CANDLES)
    except DataAgentError:
        return None  # сбой по паре не ломает весь скан

    close = pd.to_numeric(pd.Series([c[4] for c in raw]), errors="coerce")
    volume = pd.to_numeric(pd.Series([c[5] for c in raw]), errors="coerce")
    if close.dropna().shape[0] < 30:
        return None

    try:
        price = float(ticker.get("lastPrice") or close.iloc[-1])
        change_24h = float(ticker.get("priceChangePercent"))
    except (TypeError, ValueError):
        price = float(close.iloc[-1])
        change_24h = None

    rsi_series = ind.rsi(close, 14)
    rsi_now = float(rsi_series.iloc[-1])
    rsi_prev = float(rsi_series.iloc[-2])
    ema200 = float(ind.ema(close, 200).iloc[-1])

    avg_vol = float(volume.tail(_VOL_WINDOW).mean())
    last_vol = float(volume.iloc[-1])
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0.0

    patterns = _detect_patterns(
        rsi_now, rsi_prev, price, ema200, change_24h, vol_ratio
    )
    if not patterns:
        return None

    return {
        "symbol": symbol,
        "price": price,
        "change_24h": change_24h,
        "rsi": rsi_now,
        "vol_ratio": vol_ratio,
        "patterns": patterns,
    }


async def scan(timeframe: str = DEFAULT_TIMEFRAME, top_n: int = DEFAULT_TOP_N) -> list[dict]:
    """Просканировать топ-N USDT-пар и вернуть список находок.

    Пустой список — нормальный результат (ярких ситуаций нет).
    Бросает DataAgentError, только если недоступен сам список тикеров.
    """
    tickers = await get_all_tickers_24h()
    universe = _select_universe(tickers, top_n)

    findings: list[dict] = []
    deadline = time.monotonic() + _TIME_BUDGET
    for ticker in universe:
        if time.monotonic() > deadline:
            break
        found = await _scan_pair(ticker, timeframe)
        if found is not None:
            findings.append(found)

    # Сначала пары с бОльшим числом паттернов, затем по силе суточного движения.
    findings.sort(
        key=lambda f: (len(f["patterns"]), abs(f["change_24h"] or 0.0)),
        reverse=True,
    )
    return findings
