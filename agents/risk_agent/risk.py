"""Risk Manager Agent — проверка риск-лимитов (предупреждает, не запрещает).

Чистая арифметика, без обращения к API. Решение всегда остаётся за человеком.
"""
from __future__ import annotations

_PER_TRADE_LIMIT = 2.0  # классический лимит риска на сделку, %


class RiskError(Exception):
    """Понятная наружу ошибка валидации входных данных."""


def check_risk(
    deposit_usdt: float,
    risk_pct: float,
    open_trades: int = 0,
    max_total_risk_pct: float = 6,
) -> dict:
    """Проверить параметры риска.

    Возвращает {per_trade_ok, total_risk_pct, verdict, advice}.
    Бросает RiskError при некорректных входных данных.
    """
    try:
        deposit_usdt = float(deposit_usdt)
        risk_pct = float(risk_pct)
        open_trades = int(open_trades)
        max_total_risk_pct = float(max_total_risk_pct)
    except (TypeError, ValueError):
        raise RiskError("Параметры должны быть числами.")

    if deposit_usdt <= 0:
        raise RiskError("Депозит должен быть больше нуля.")
    if not (0 < risk_pct <= 100):
        raise RiskError("Риск в процентах должен быть в диапазоне (0, 100].")
    if open_trades < 0:
        raise RiskError("Число открытых сделок не может быть отрицательным.")
    if max_total_risk_pct <= 0:
        raise RiskError("Лимит суммарного риска должен быть больше нуля.")

    per_trade_ok = risk_pct <= _PER_TRADE_LIMIT
    total_risk_pct = risk_pct * (open_trades + 1)
    total_ok = total_risk_pct <= max_total_risk_pct

    if not per_trade_ok and not total_ok:
        verdict = "Риск на сделку и суммарный риск выше лимитов."
        advice = (
            f"Снизь риск на сделку до ≤{_PER_TRADE_LIMIT:.0f}% "
            "и/или сократи число одновременных позиций."
        )
    elif not per_trade_ok:
        verdict = f"Риск на сделку выше классического лимита ({_PER_TRADE_LIMIT:.0f}%)."
        advice = f"Рассмотри риск ≤{_PER_TRADE_LIMIT:.0f}% на сделку."
    elif not total_ok:
        verdict = "Превышен лимит суммарного риска."
        advice = (
            f"Суммарный риск {total_risk_pct:.1f}% выше лимита "
            f"{max_total_risk_pct:.0f}% — сократи число позиций или риск на каждую."
        )
    else:
        verdict = "В пределах лимитов."
        advice = "Параметры риска выглядят умеренными."

    return {
        "deposit_usdt": deposit_usdt,
        "risk_pct": risk_pct,
        "open_trades": open_trades,
        "max_total_risk_pct": max_total_risk_pct,
        "per_trade_ok": per_trade_ok,
        "total_risk_pct": total_risk_pct,
        "total_ok": total_ok,
        "verdict": verdict,
        "advice": advice,
    }
