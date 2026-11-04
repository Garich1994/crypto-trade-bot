# crypto-trade-bot

Telegram-бот для **аналитики** крипторынка. Не сигналы, не автоторговля — только
нейтральный технический анализ, решение всегда остаётся за человеком.

## Архитектура

Команда агентов, каждый агент — папка `agents/<имя>/` со `SKILL.md` (инструкция
агента) и Python-кодом.

- `agents/data_agent/` — сбор данных с публичного API Binance (без ключей), кэш в памяти.
- `agents/ta_agent/`   — технический анализ (EMA, RSI, MACD, ATR, Bollinger, S/R, объём).
- `bot/`               — Telegram-слой (aiogram 3.x, long polling).
- `core/`              — конфигурация (чтение `.env`).

## Запуск (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # и вписать BOT_TOKEN
python -m bot.main
```

## Команды бота

- `/start` — приветствие, список команд, дисклеймер.
- `/analyze BTCUSDT [таймфрейм]` — разбор монеты (таймфреймы: 15m, 1h, 4h, 1d; по умолчанию 1h).
- `/backtest BTCUSDT long вход стоп тейк [таймфрейм]` — исторический прогон уровней сделки
  (пример: `/backtest BTCUSDT long 80000 78000 84000 1h`). Это replay по прошлым свечам, не предсказание.

## Самопроверка

```
python selftest.py
```

⚠️ Не финансовый совет. Это аналитика, решение — за тобой.
