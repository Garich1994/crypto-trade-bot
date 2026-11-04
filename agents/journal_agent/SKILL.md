# Journal Agent (Trade Analysis)

## Назначение
Личный журнал сделок пользователя и честная статистика по закрытым сделкам.
Агент ничего не советует: он только хранит записи и считает факты.

## Честное ограничение
Статистика описывает **прошлое** (твои закрытые сделки). Она не предсказывает
будущее и не является оценкой вероятности успеха следующих сделок.

## Хранилище
SQLite, файл `data/journal.db` (папка `data/` в `.gitignore` — чужие сделки не
попадают в git). Таблица `trades`:
`id, user_id, symbol, side, entry, stop, take, qty, status, exit_price,
pnl_usdt, pnl_r, opened_at, closed_at`.

## Изоляция пользователей
Все операции фильтруются по `user_id`. Сделка одного пользователя недоступна
другому: чужая сделка для него просто «не найдена».

## Формулы PnL
```
направление   = +1 (long) / -1 (short)
pnl_usdt       = (exit_price - entry) * qty * направление
risk           = |entry - stop| * qty
pnl_r          = pnl_usdt / risk
```

## Интерфейс
- `add_trade(user_id, symbol, side, entry, stop, take, qty) -> id`  (статус open)
- `close_trade(user_id, trade_id, exit_price) -> {pnl_usdt, pnl_r, ...}`
- `stats(user_id) -> {total, wins, win_rate, avg_r, expectancy, total_pnl_usdt, best_r, worst_r}`
- `open_trades(user_id) -> list[dict]`
- `set_db_path(path)` — переопределить файл БД (используется в тестах).

Ошибки (нет сделки, чужая сделка, уже закрыта, неверные данные) — `JournalError`
с понятным текстом на русском.
