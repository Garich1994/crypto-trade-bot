"""Форматирование ответов бота на русском.

Только нейтральные формулировки. В конце ответа /analyze всегда дисклеймер.
"""
from __future__ import annotations

DISCLAIMER = "⚠️ Не финансовый совет. Это аналитика, решение — за тобой."
DISCLAIMER_BACKTEST = (
    "⚠️ Не финансовый совет. Прошлое не гарантирует будущего, решение — за тобой."
)

_SIDE_RU = {"long": "лонг", "short": "шорт"}


def _fmt_price(value: float) -> str:
    """Цена с разумным числом знаков (дороже — меньше знаков)."""
    if value >= 100:
        return f"{value:,.2f}".replace(",", " ")
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _fmt_level(value: float | None) -> str:
    return _fmt_price(value) if value is not None else "не найден в окне"


def format_analysis(result: dict) -> str:
    """Собрать текст ответа на /analyze из словаря TA Agent."""
    change = result.get("change_24h")
    if change is None:
        change_str = "н/д"
    else:
        sign = "+" if change >= 0 else ""
        change_str = f"{sign}{change:.2f}%"

    funding = result.get("funding_rate")
    if funding is None:
        funding_str = "недоступен"
    else:
        funding_str = f"{funding * 100:+.4f}%"

    lines = [
        f"📊 Анализ {result['symbol']} · таймфрейм {result['timeframe']}",
        "",
        f"💵 Цена: {_fmt_price(result['price'])}",
        f"📈 Изменение за 24ч: {change_str}",
        f"📐 Тренд: {result['trend']}",
        f"🎚 RSI(14): {result['rsi']:.1f} — {result['rsi_state']}",
        f"〽️ MACD: {result['macd_state']}",
        f"🎯 Уровни: поддержка {_fmt_level(result.get('support'))}, "
        f"сопротивление {_fmt_level(result.get('resistance'))}",
        f"💰 Funding rate: {funding_str}",
        f"🌪 Волатильность (ATR14): {result['atr_pct']:.2f}% — "
        f"{result['volatility_state']}",
        f"📦 Объём: {result['volume_state']}",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def format_backtest(result: dict) -> str:
    """Собрать текст ответа на /backtest из словаря Backtest Agent."""
    pf = result.get("profit_factor")
    pf_str = "∞ (без убыточных сделок)" if pf is None else f"{pf:.2f}"

    side_ru = _SIDE_RU.get(result["side"], result["side"])

    lines = [
        f"🕰 Бэктест {result['symbol']} · {side_ru} · таймфрейм {result['timeframe']}",
        "",
        f"🎯 Вход {_fmt_price(result['entry'])}, "
        f"стоп {_fmt_price(result['stop'])}, "
        f"тейк {_fmt_price(result['take'])}  (R:R ≈ {result['rr']:.2f})",
        f"🕯 Свечей в истории: {result['candles_analyzed']}",
        "",
        f"🔁 Сделок: {result['trades']} "
        f"(прибыльных {result['wins']}, убыточных {result['losses']})",
        f"✅ Win rate: {result['win_rate'] * 100:.1f}%",
        f"📏 Средний R: {result['avg_R']:+.2f}",
        f"🧮 Expectancy (ожидаемый R/сделку): {result['expectancy']:+.2f}",
        f"⚖️ Profit factor: {pf_str}",
        f"📉 Макс. просадка: {result['max_drawdown']:.2f} R",
        "",
        DISCLAIMER_BACKTEST,
    ]
    return "\n".join(lines)
