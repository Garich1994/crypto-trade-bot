"""Budget Manager Agent — калькулятор размера позиции по заданному риску.

Чистая арифметика, без обращения к API. Ничего не советует делать.
"""
from __future__ import annotations


class BudgetError(Exception):
    """Понятная наружу ошибка валидации входных данных."""


def calc_position(
    deposit_usdt: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: float = 1,
) -> dict:
    """Рассчитать размер позиции.

    Возвращает {risk_usdt, qty, position_usdt, leverage}.
    Бросает BudgetError при некорректных входных данных.
    """
    try:
        deposit_usdt = float(deposit_usdt)
        risk_pct = float(risk_pct)
        entry_price = float(entry_price)
        stop_price = float(stop_price)
        leverage = float(leverage)
    except (TypeError, ValueError):
        raise BudgetError("Все параметры должны быть числами.")

    if deposit_usdt <= 0:
        raise BudgetError("Депозит должен быть больше нуля.")
    if entry_price <= 0 or stop_price <= 0:
        raise BudgetError("Цены входа и стопа должны быть больше нуля.")
    if leverage <= 0:
        raise BudgetError("Плечо должно быть больше нуля.")
    if not (0 < risk_pct <= 100):
        raise BudgetError("Риск в процентах должен быть в диапазоне (0, 100].")
    if entry_price == stop_price:
        raise BudgetError("Стоп не может совпадать со входом.")

    risk_usdt = deposit_usdt * risk_pct / 100.0
    qty = risk_usdt / abs(entry_price - stop_price)
    position_usdt = qty * entry_price / leverage

    return {
        "deposit_usdt": deposit_usdt,
        "risk_pct": risk_pct,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "risk_usdt": risk_usdt,
        "qty": qty,
        "position_usdt": position_usdt,
        "leverage": leverage,
    }
