"""TA Agent — сборка анализа пары в нейтральный словарь фактов.

Берёт данные у Data Agent, считает индикаторы (indicators.py) и формирует dict.
Нигде не используются слова «покупай/продавай/сигнал» — только нейтральные
описания состояния рынка.
"""
from __future__ import annotations

import pandas as pd

from agents.data_agent import (
    DataAgentError,
    get_funding_rate,
    get_klines,
    get_ticker_24h,
)
from agents.data_agent.client import ALLOWED_INTERVALS
from agents.ta_agent import indicators as ind

DEFAULT_TIMEFRAME = "1h"
_KLINES_LIMIT = 300  # хватает для EMA 200 с запасом


def _klines_to_df(raw: list[list]) -> pd.DataFrame:
    """Сырые свечи Binance -> DataFrame с числовыми колонками."""
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _trend_by_ema(price: float, e20: float, e50: float, e200: float) -> str:
    """Нейтральное описание тренда по взаимному расположению EMA."""
    if price > e20 > e50 > e200:
        return "восходящий (цена выше EMA 20 > 50 > 200)"
    if price < e20 < e50 < e200:
        return "нисходящий (цена ниже EMA 20 < 50 < 200)"
    if price > e200:
        return "смешанный, выше долгосрочной EMA 200"
    return "смешанный, ниже долгосрочной EMA 200"


def _rsi_state(value: float) -> str:
    if value >= 70:
        return "зона перекупленности"
    if value <= 30:
        return "зона перепроданности"
    return "нейтрально"


def _macd_state(macd_line: float, avg_line: float, hist: float) -> str:
    """Нейтральное описание MACD без слова «сигнал»."""
    position = "выше" if macd_line >= avg_line else "ниже"
    hist_word = "положительная" if hist >= 0 else "отрицательная"
    zero_word = "выше нуля" if macd_line >= 0 else "ниже нуля"
    return (
        f"линия MACD {position} усреднённой линии, "
        f"гистограмма {hist_word}, MACD {zero_word}"
    )


def _volatility_state(atr_pct: float) -> str:
    if atr_pct >= 3.0:
        return "высокая"
    if atr_pct >= 1.0:
        return "умеренная"
    return "низкая"


def _volume_state(last_volume: float, avg_volume: float) -> str:
    if avg_volume <= 0:
        return "нет данных"
    ratio = last_volume / avg_volume
    if ratio >= 1.5:
        return f"повышенный (x{ratio:.1f} к среднему)"
    if ratio <= 0.5:
        return f"пониженный (x{ratio:.1f} к среднему)"
    return f"средний (x{ratio:.1f} к среднему)"


async def analyze(symbol: str, timeframe: str = DEFAULT_TIMEFRAME) -> dict:
    """Собрать нейтральный технический анализ пары.

    Бросает DataAgentError при недоступности API или неизвестном тикере.
    """
    symbol = symbol.upper().strip()
    timeframe = timeframe.lower().strip()
    if timeframe not in ALLOWED_INTERVALS:
        raise DataAgentError(
            f"Неподдерживаемый таймфрейм: {timeframe}. "
            f"Доступны: {', '.join(ALLOWED_INTERVALS)}."
        )

    raw_klines = await get_klines(symbol, timeframe, limit=_KLINES_LIMIT)
    ticker = await get_ticker_24h(symbol)
    funding = await get_funding_rate(symbol)

    df = _klines_to_df(raw_klines)
    close = df["close"]
    if close.dropna().shape[0] < 30:
        raise DataAgentError("Недостаточно свечей для анализа.")

    price = float(ticker.get("lastPrice") or close.iloc[-1])
    try:
        change_24h = float(ticker.get("priceChangePercent"))
    except (TypeError, ValueError):
        change_24h = None

    e20 = ind.ema(close, 20)
    e50 = ind.ema(close, 50)
    e200 = ind.ema(close, 200)
    rsi_series = ind.rsi(close, 14)
    macd_line, macd_avg, macd_hist = ind.macd(close, 12, 26, 9)
    atr_series = ind.atr(df["high"], df["low"], close, 14)
    bb_upper, bb_mid, bb_lower = ind.bollinger(close, 20, 2.0)

    swing_highs, swing_lows = ind.swing_levels(df["high"], df["low"], window=5)
    # Уровни S/R считаем от последнего закрытия — единый источник на всех экранах
    # (Аналитика и детали Skeptic дают одинаковые значения на одном таймфрейме).
    support, resistance = ind.nearest_support_resistance(
        swing_highs, swing_lows, float(close.iloc[-1])
    )

    atr_val = float(atr_series.iloc[-1])
    atr_pct = (atr_val / price * 100) if price else 0.0

    last_volume = float(df["volume"].iloc[-1])
    avg_volume = float(df["volume"].tail(20).mean())

    rsi_val = float(rsi_series.iloc[-1])

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price": price,
        "change_24h": change_24h,
        "trend": _trend_by_ema(
            price, float(e20.iloc[-1]), float(e50.iloc[-1]), float(e200.iloc[-1])
        ),
        "ema": {
            "ema20": float(e20.iloc[-1]),
            "ema50": float(e50.iloc[-1]),
            "ema200": float(e200.iloc[-1]),
        },
        "rsi": rsi_val,
        "rsi_state": _rsi_state(rsi_val),
        "macd_state": _macd_state(
            float(macd_line.iloc[-1]),
            float(macd_avg.iloc[-1]),
            float(macd_hist.iloc[-1]),
        ),
        "atr": atr_val,
        "atr_pct": atr_pct,
        "volatility_state": _volatility_state(atr_pct),
        "support": support,
        "resistance": resistance,
        "bollinger": {
            "upper": float(bb_upper.iloc[-1]),
            "middle": float(bb_mid.iloc[-1]),
            "lower": float(bb_lower.iloc[-1]),
        },
        "volume_state": _volume_state(last_volume, avg_volume),
        "funding_rate": funding,
    }
