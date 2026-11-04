"""Самопроверка: прогон analyze на BTCUSDT и ETHUSDT (таймфрейм 1h).

Проверяет, что все индикаторы посчитались и сообщение сформировалось.
Зелёные галочки = всё ок. Запуск:  python selftest.py
"""
from __future__ import annotations

import asyncio
import math
import sys

# На Windows консоль по умолчанию cp1252 и не печатает кириллицу — переключаем в UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from agents.data_agent import DataAgentError
from agents.ta_agent import analyze
from bot.formatting import DISCLAIMER, format_analysis

OK = "\033[92m[OK]\033[0m"
FAIL = "\033[91m[X]\033[0m"

# Числовые поля, которые обязаны посчитаться (конечное число).
REQUIRED_NUMBERS = ("price", "rsi", "atr", "atr_pct")
# Текстовые поля состояния, которые обязаны быть непустыми.
REQUIRED_TEXTS = ("trend", "rsi_state", "macd_state", "volatility_state", "volume_state")
# Вложенные блоки индикаторов.
REQUIRED_NESTED = {
    "ema": ("ema20", "ema50", "ema200"),
    "bollinger": ("upper", "middle", "lower"),
}
# Запрещённые слова в пользовательском тексте (нейтральность).
FORBIDDEN_WORDS = ("покупай", "продавай", "сигнал")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _check_result(result: dict, errors: list[str]) -> None:
    tag = f"{result.get('symbol')}/{result.get('timeframe')}"

    for key in REQUIRED_NUMBERS:
        if not _is_number(result.get(key)):
            errors.append(f"{tag}: поле '{key}' не посчиталось ({result.get(key)!r})")

    for key in REQUIRED_TEXTS:
        if not str(result.get(key, "")).strip():
            errors.append(f"{tag}: поле '{key}' пустое")

    for block, subkeys in REQUIRED_NESTED.items():
        data = result.get(block, {})
        for sub in subkeys:
            if not _is_number(data.get(sub)):
                errors.append(f"{tag}: {block}.{sub} не посчиталось")

    # Уровни S/R могут быть None (не нашлись в окне) — это допустимо,
    # но ключи должны присутствовать.
    for key in ("support", "resistance", "funding_rate", "change_24h"):
        if key not in result:
            errors.append(f"{tag}: нет ключа '{key}'")


def _check_message(result: dict, errors: list[str]) -> str:
    tag = f"{result.get('symbol')}/{result.get('timeframe')}"
    message = format_analysis(result)

    if not message.strip():
        errors.append(f"{tag}: сообщение не сформировалось")
    if DISCLAIMER not in message:
        errors.append(f"{tag}: в сообщении нет дисклеймера")

    low = message.lower()
    for word in FORBIDDEN_WORDS:
        if word in low:
            errors.append(f"{tag}: в тексте запрещённое слово «{word}»")
    return message


async def _run() -> int:
    pairs = [("BTCUSDT", "1h"), ("ETHUSDT", "1h")]
    errors: list[str] = []

    for symbol, timeframe in pairs:
        print(f"\n— Проверяю {symbol} ({timeframe}) …")
        try:
            result = await analyze(symbol, timeframe)
        except DataAgentError as exc:
            errors.append(f"{symbol}/{timeframe}: данные недоступны — {exc}")
            print(f"  {FAIL} не удалось получить данные: {exc}")
            continue

        before = len(errors)
        _check_result(result, errors)
        message = _check_message(result, errors)

        if len(errors) == before:
            print(f"  {OK} индикаторы посчитаны, сообщение сформировано")
            print("  ----- превью ответа -----")
            for line in message.splitlines():
                print(f"  {line}")
            print("  --------------------------")

    print()
    if errors:
        print(f"{FAIL} Проверки НЕ пройдены ({len(errors)}):")
        for err in errors:
            print(f"   - {err}")
        return 1

    print(f"{OK} Все проверки зелёные.")
    return 0


def main() -> None:
    try:
        code = asyncio.run(_run())
    except KeyboardInterrupt:
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
