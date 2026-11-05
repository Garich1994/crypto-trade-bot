"""Алиас запуска самопроверки: `python -m bot.selftest`.

Все проверки находятся в корневом `selftest.py`; этот модуль просто запускает их.
"""
from __future__ import annotations

import selftest

if __name__ == "__main__":
    selftest.main()
