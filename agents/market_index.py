"""Market Index — индекс режима рынка (0–100) по топ-30 USDT-парам.

Это режим рынка, а не прогноз. Формула прозрачна:
- Широта (0–40): доля пар с ценой выше EMA50 × 40
- Импульс (0–30): clamp((средний RSI(14) − 20) / 60 × 30, 0, 30)
- Объём (0–30): clamp((средний объём 24ч / средний за 20 дней − 0.5) / 1.5 × 30, 0, 30)
Итог = сумма (0–100). Данные — дневные свечи Binance, без выдуманных чисел.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from agents.data_agent import DataAgentError, get_all_tickers_24h, get_klines
from agents.ta_agent import indicators as ind

DEFAULT_TOP_N = 30
_STABLE_BASES = {"USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI", "USDD", "EUR"}
_LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _label(value: float) -> str:
    if value < 25:
        return "Страх"
    if value < 45:
        return "Осторожность"
    if value < 55:
        return "Нейтрально"
    if value < 75:
        return "Уверенность"
    return "Жадность"


def _is_tradable_usdt(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if not base or base in _STABLE_BASES:
        return False
    return not any(base.endswith(m) for m in _LEVERAGED)


def _select_universe(tickers: list[dict], top_n: int) -> list[dict]:
    usdt = [t for t in tickers if _is_tradable_usdt(str(t.get("symbol", "")))]

    def _vol(t: dict) -> float:
        try:
            return float(t.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            return 0.0

    usdt.sort(key=_vol, reverse=True)
    return usdt[:top_n]


def compute(metrics: list[dict]) -> dict:
    """Чистый расчёт индекса по списку метрик пар (тестируется без сети)."""
    n = len(metrics)
    if n == 0:
        raise DataAgentError("Нет данных по парам для расчёта индекса.")

    breadth = sum(1 for m in metrics if m["price"] > m["ema50"]) / n * 40.0

    avg_rsi = sum(m["rsi"] for m in metrics) / n
    momentum = _clamp((avg_rsi - 20) / 60 * 30, 0, 30)

    ratios = [m["vol_24h"] / m["avg_vol_20"] for m in metrics if m["avg_vol_20"] > 0]
    avg_ratio = (sum(ratios) / len(ratios)) if ratios else 0.0
    volume = _clamp((avg_ratio - 0.5) / 1.5 * 30, 0, 30)

    value = round(breadth + momentum + volume)
    return {
        "value": int(value),
        "label": _label(value),
        "components": {
            "breadth": round(breadth, 1),
            "momentum": round(momentum, 1),
            "volume": round(volume, 1),
        },
    }


async def _pair_metrics(symbol: str) -> dict:
    raw = await get_klines(symbol, "1d", limit=60)
    close = pd.to_numeric(pd.Series([c[4] for c in raw]), errors="coerce")
    volume = pd.to_numeric(pd.Series([c[5] for c in raw]), errors="coerce")
    if close.dropna().shape[0] < 20:
        raise DataAgentError("Недостаточно дневных свечей.")
    return {
        "price": float(close.iloc[-1]),
        "ema50": float(ind.ema(close, 50).iloc[-1]),
        "rsi": float(ind.rsi(close, 14).iloc[-1]),
        "vol_24h": float(volume.iloc[-1]),
        "avg_vol_20": float(volume.iloc[-21:-1].mean()),
    }


async def market_index(top_n: int = DEFAULT_TOP_N) -> dict:
    """Собрать индекс режима рынка по топ-N USDT-парам.

    Возвращает {value, label, components:{breadth, momentum, volume}, updated_at}.
    Бросает DataAgentError, если недоступен список тикеров.
    """
    tickers = await get_all_tickers_24h()
    universe = _select_universe(tickers, top_n)

    metrics: list[dict] = []
    for ticker in universe:
        try:
            metrics.append(await _pair_metrics(str(ticker["symbol"])))
        except DataAgentError:
            continue  # пара пропускается, числа не выдумываются

    result = compute(metrics)
    result["updated_at"] = datetime.now(timezone.utc).isoformat()
    result["pairs"] = len(metrics)
    return result
