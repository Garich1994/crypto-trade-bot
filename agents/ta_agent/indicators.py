"""Технические индикаторы на pandas/numpy.

Чистые функции над ценовым рядом. Ничего не интерпретируют в торговом ключе —
только считают числа.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Экспоненциальная скользящая средняя."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI по сглаживанию Уайлдера (RMA)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Если потерь не было вовсе — RSI = 100.
    out = out.where(avg_loss != 0.0, 100.0)
    return out


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD. Возвращает (линия MACD, усреднённая линия, гистограмма)."""
    macd_line = ema(series, fast) - ema(series, slow)
    avg_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - avg_line
    return macd_line, avg_line, histogram


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range (сглаживание Уайлдера)."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Полосы Боллинджера. Возвращает (верхняя, средняя, нижняя)."""
    middle = series.rolling(window=period).mean()
    std = series.rolling(window=period).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def swing_levels(
    high: pd.Series,
    low: pd.Series,
    window: int = 5,
) -> tuple[list[float], list[float]]:
    """Уровни по свингам: локальные максимумы (сопротивления) и минимумы (поддержки).

    Точка считается свингом, если она является экстремумом в окне ±window.
    """
    n = len(high)
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    if n < 2 * window + 1:
        return swing_highs, swing_lows

    h = high.to_numpy()
    low_arr = low.to_numpy()
    for i in range(window, n - window):
        left = i - window
        right = i + window + 1
        if h[i] == h[left:right].max():
            swing_highs.append(float(h[i]))
        if low_arr[i] == low_arr[left:right].min():
            swing_lows.append(float(low_arr[i]))
    return swing_highs, swing_lows


def nearest_support_resistance(
    swing_highs: list[float],
    swing_lows: list[float],
    price: float,
) -> tuple[float | None, float | None]:
    """Ближайшие поддержка (ниже цены) и сопротивление (выше цены)."""
    resistances = [h for h in swing_highs if h > price]
    supports = [low for low in swing_lows if low < price]
    resistance = min(resistances) if resistances else None
    support = max(supports) if supports else None
    return support, resistance
