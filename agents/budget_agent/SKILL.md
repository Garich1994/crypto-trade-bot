# Budget Manager Agent

## Назначение
Расчёт размера позиции исходя из заданного риска на сделку. Это калькулятор, а не
рекомендация: агент считает, сколько единиц актива соответствует твоему риску, и
ничего не советует делать.

## Формула
```
risk_usdt     = deposit_usdt * risk_pct / 100
qty           = risk_usdt / |entry_price - stop_price|
position_usdt = qty * entry_price / leverage
```
- `risk_usdt` — сколько денег теряется при срабатывании стопа.
- `qty` — размер позиции в единицах базовой монеты.
- `position_usdt` — требуемая маржа с учётом плеча (номинал `qty*entry` делится на `leverage`).

## Валидация
- `deposit_usdt`, `entry_price`, `stop_price`, `leverage` — числа > 0.
- `risk_pct` ∈ (0, 100].
- `stop_price != entry_price` (иначе деление на ноль).
Иначе — понятная ошибка на русском (`BudgetError`).

## Интерфейс
```
calc_position(deposit_usdt, risk_pct, entry_price, stop_price, leverage=1)
    -> {"risk_usdt", "qty", "position_usdt", "leverage"}
```
Синхронная (чистая арифметика, без обращения к API).
