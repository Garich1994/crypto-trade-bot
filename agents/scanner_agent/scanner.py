"""Scanner Agent — обзор топовых USDT-пар и подсветка ситуаций.

Находит совпадения с простыми паттернами, ничего не предсказывает и не советует.
"""
from __future__ import annotations

import asyncio
import os
import time

import pandas as pd

from agents.data_agent import DataAgentError, get_all_tickers_24h, get_klines
from agents.ta_agent import indicators as ind

DEFAULT_TIMEFRAME = "1h"
DEFAULT_TOP_N = 30
_CANDLES = 200           # достаточно для RSI и ориентира по EMA200
_TIME_BUDGET = 85.0      # мягкий дедлайн, сек (топ-N, последовательный скан)
_VOL_WINDOW = 20
_MAX_STALE_MS = 3 * 24 * 3600 * 1000  # пара «мертва», если последний бар старше 3 суток

# Скан всей вселенной: сколько пар максимум (по объёму) и параллельность.
UNIVERSE_CAP = int(os.environ.get("SCAN_UNIVERSE_CAP", "400"))
UNIVERSE_CONCURRENCY = int(os.environ.get("SCAN_CONCURRENCY", "24"))
UNIVERSE_TIME_BUDGET = float(os.environ.get("SCAN_UNIVERSE_BUDGET", "170"))  # ≤3 мин

# Исключаем стейблкоины и leveraged-токены из вселенной.
_STABLE_BASES = {
    "USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI", "USDD", "EUR",
    "USD1", "USDE", "AEUR", "XUSD", "EURI", "GUSD", "USTC", "PYUSD",
}
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


def _detect_structure(close, rsi_series) -> str | None:
    """Распознать паттерн методологии на свечах. Возвращает имя или None."""
    c = close.to_numpy()
    r = rsi_series.to_numpy()
    n = len(c)
    # 20-периодный пробой по закрытиям
    if n > 21 and c[-1] > c[-21:-1].max():
        return "20-day breakout"
    # bull flag: рост >10% за 10 свеч до последних 5, затем 5 закрытий в коридоре 5%
    if n >= 15:
        before, mid = c[-15], c[-5]
        if before > 0 and (mid - before) / before > 0.10:
            seg = c[-5:]
            if seg.min() > 0 and (seg.max() - seg.min()) / seg.min() < 0.05:
                return "bull flag"
    # reclaim MA20: предыдущее закрытие ниже SMA20, последнее — выше
    if n >= 21:
        sma_prev = c[-21:-1].mean()
        sma_now = c[-20:].mean()
        if c[-2] < sma_prev and c[-1] > sma_now:
            return "MA20 reclaim"
    # oversold bounce: RSI был < 30, текущая свеча закрылась ростом
    if len(r) >= 2 and r[-2] < 30 and c[-1] > c[-2]:
        return "oversold bounce"
    return None


def _score_trend(side, price, e20, e50, e200) -> int:
    if side == "long":
        if e20 > e50 > e200:
            return 30
        if e20 > e50:
            return 20
        if price > e200:
            return 10
        return 0
    if e20 < e50 < e200:
        return 30
    if e20 < e50:
        return 20
    if price < e200:
        return 10
    return 0


def _score_volume(ratio) -> int:
    if ratio >= 1.5:
        return 25
    if ratio >= 1.2:
        return 15
    if ratio >= 1.0:
        return 8
    return 0


def _score_rsi(side, rsi) -> int:
    eff = rsi if side == "long" else 100 - rsi
    if 45 <= eff <= 65:
        return 20
    if (35 <= eff < 45) or (65 < eff <= 75):
        return 10
    return 0


def _score_level(side, price, support, resistance, atr) -> int:
    if atr <= 0:
        return 0
    if side == "long":
        dist = (resistance - price) / atr if resistance is not None else 999.0
    else:
        dist = (price - support) / atr if support is not None else 999.0
    if dist >= 2:
        return 15
    if dist >= 1:
        return 8
    return 0


def score_setup(m: dict) -> tuple[int, list[dict]]:
    """Качество сетапа по правилам (НЕ вероятность успеха). score = сумма factors."""
    factors = [
        {"name": "Тренд", "points": _score_trend(
            m["side"], m["price"], m["ema20"], m["ema50"], m["ema200"]), "max": 30},
        {"name": "Объём", "points": _score_volume(m["vol_ratio"]), "max": 25},
        {"name": "RSI-зона", "points": _score_rsi(m["side"], m["rsi"]), "max": 20},
        {"name": "Место до уровня", "points": _score_level(
            m["side"], m["price"], m["support"], m["resistance"], m["atr"]), "max": 15},
        {"name": "Структура", "points": 10 if m.get("structure") else 0, "max": 10},
    ]
    score = sum(f["points"] for f in factors)
    return score, factors


async def _scan_pair(
    ticker: dict, timeframe: str, require_pattern: bool = True
) -> dict | None:
    symbol = str(ticker.get("symbol", "")).upper()
    try:
        raw = await get_klines(symbol, timeframe, limit=_CANDLES)
    except DataAgentError:
        return None  # сбой по паре не ломает весь скан

    # Отбрасываем «мёртвые» пары: в 24ч-тикере ещё числятся, но свечи заморожены
    # (делистинг/остановка торгов) — показывать их как живые было бы нечестно.
    try:
        if (time.time() * 1000 - int(raw[-1][0])) > _MAX_STALE_MS:
            return None
    except (TypeError, ValueError, IndexError):
        pass

    close = pd.to_numeric(pd.Series([c[4] for c in raw]), errors="coerce")
    high = pd.to_numeric(pd.Series([c[2] for c in raw]), errors="coerce")
    low = pd.to_numeric(pd.Series([c[3] for c in raw]), errors="coerce")
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
    ema20 = float(ind.ema(close, 20).iloc[-1])
    ema50 = float(ind.ema(close, 50).iloc[-1])
    ema200 = float(ind.ema(close, 200).iloc[-1])
    atr = float(ind.atr(high, low, close, 14).iloc[-1])
    macd_line, macd_avg, macd_hist = ind.macd(close, 12, 26, 9)
    macd_hist = float(macd_hist.iloc[-1])

    avg_vol = float(volume.tail(_VOL_WINDOW).mean())
    last_vol = float(volume.iloc[-1])
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0.0

    patterns = _detect_patterns(
        rsi_now, rsi_prev, price, ema200, change_24h, vol_ratio
    )
    if require_pattern and not patterns:
        return None

    swing_highs, swing_lows = ind.swing_levels(high, low, window=5)
    support, resistance = ind.nearest_support_resistance(swing_highs, swing_lows, price)
    side = "long" if price >= ema200 else "short"
    structure = _detect_structure(close, rsi_series)

    score, factors = score_setup({
        "side": side, "price": price, "ema20": ema20, "ema50": ema50,
        "ema200": ema200, "vol_ratio": vol_ratio, "rsi": rsi_now, "atr": atr,
        "support": support, "resistance": resistance, "structure": structure,
    })

    spark = [round(float(v), 8) for v in close.dropna().tail(24).tolist()]

    return {
        "symbol": symbol,
        "price": price,
        "change_24h": change_24h,
        "rsi": rsi_now,
        "vol_ratio": vol_ratio,
        "patterns": patterns,
        "side": side,
        "structure": structure,
        "breakout": bool(structure and "breakout" in structure.lower()),
        "score": score,
        "factors": factors,
        "support": support,
        "resistance": resistance,
        "ema": {"ema20": ema20, "ema50": ema50, "ema200": ema200},
        "atr": atr,
        "macd": macd_hist,
        "spark": spark,
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

    # Сначала по силе сетапа, затем по числу паттернов.
    findings.sort(
        key=lambda f: (f.get("score", 0), len(f["patterns"])),
        reverse=True,
    )
    return findings


def _select_all_universe(tickers: list[dict], cap: int) -> list[dict]:
    """Все торгуемые USDT-пары (без стейблов и leveraged), срез по объёму до cap."""
    usdt = [t for t in tickers if _is_tradable_usdt(str(t.get("symbol", "")))]

    def _volume(t: dict) -> float:
        try:
            return float(t.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            return 0.0

    usdt.sort(key=_volume, reverse=True)
    return usdt[:cap] if cap > 0 else usdt


async def scan_all(
    timeframe: str = DEFAULT_TIMEFRAME,
    cap: int = UNIVERSE_CAP,
    concurrency: int = UNIVERSE_CONCURRENCY,
) -> list[dict]:
    """Просканировать всю вселенную USDT-пар (параллельно) и вернуть ВСЕ находки.

    В отличие от scan() возвращает каждую пару со скорингом сетапа (не только
    совпавшие с паттерном) — таблица на сайте сортируется по силе сетапа сама.
    Полный проход укладывается в мягкий дедлайн (≤3 минут); пары, не успевшие
    или сбойные, просто выпадают. Пустой список — честный результат.
    """
    tickers = await get_all_tickers_24h()
    universe = _select_all_universe(tickers, cap)

    sem = asyncio.Semaphore(max(1, concurrency))
    deadline = time.monotonic() + UNIVERSE_TIME_BUDGET

    async def _one(ticker: dict) -> dict | None:
        if time.monotonic() > deadline:
            return None
        async with sem:
            if time.monotonic() > deadline:
                return None
            try:
                return await _scan_pair(ticker, timeframe, require_pattern=False)
            except DataAgentError:
                return None
            except Exception:  # noqa: BLE001 — сбой по паре не валит весь скан
                return None

    results = await asyncio.gather(*(_one(t) for t in universe))
    findings = [r for r in results if r is not None]

    # По силе сетапа (desc), затем по числу паттернов — слабые идут вниз сами.
    findings.sort(
        key=lambda f: (f.get("score", 0), len(f.get("patterns", []))),
        reverse=True,
    )
    return findings
