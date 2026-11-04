# Coordinator Agent (автономный дайджест)

## Назначение
Дирижёр: запускает цепочку готовых агентов и собирает единый дайджест по рынку.
Своей торговой логики не содержит — только вызывает функции других агентов и
сводит результат. Наблюдения, а не рекомендации.

## Пайплайн `build_brief`
```
build_brief(symbols=None, top=5, timeframe="1h") -> dict
```
1. `news` (3 свежие новости) — сверху дайджеста.
2. Если `symbols` не заданы — `scanner` находит ситуации по топ-парам; берём топ по силе.
3. На каждую пару: `ta.analyze` → сторона по EMA200 → `entry.score_entry` →
   `exit.plan_exit` → `skeptic`.
4. **Риск-гейт**: идея с FAIL по стопу или RSI (из skeptic) отсеивается в `filtered_out`.
5. Возврат — **структурированный dict** (не текст): `generated_at, market_summary,
   news, ideas[], filtered_out[]`. Каждая идея: `symbol, side, score, entry, stop,
   take1, take2, skeptic_passed/total, weak_points, change_24h, trend, rsi`.

Сбой по отдельной паре не валит весь бриф (пара пропускается).

## Автоподписка
Таблица `brief_subs(user_id, interval_h, active, last_sent_at)` в `data/journal.db`.
- `set_autobrief(user_id, active, interval_h=4)`;
- `get_autobrief(user_id)`;
- `due_subscriptions(now=None)` — кому пора;
- `mark_brief_sent(user_id)`.
Фоновая задача в `bot.main` (рядом со Сторожем) периодически шлёт дайджест
подписчикам каждые N часов. Работает только пока запущен бот.

## Честность
Шапка каждого автодайджеста: «🤖 Автодайджест. Наблюдения, не рекомендации.
Не финансовый совет.» Тон нейтральный — «наблюдается», без «покупай».

## Тестируемость
Имена агентов импортированы на уровень модуля `pipeline`, поэтому `build_brief`
прогоняется end-to-end на мок-функциях без сети.
