"""Форматирование ответов бота на русском.

Только нейтральные формулировки. В конце ответа /analyze всегда дисклеймер.
"""
from __future__ import annotations

from datetime import datetime, timezone

DISCLAIMER = "⚠️ Не финансовый совет. Это аналитика, решение — за тобой."
DISCLAIMER_BACKTEST = (
    "⚠️ Не финансовый совет. Прошлое не гарантирует будущего, решение — за тобой."
)
DISCLAIMER_JOURNAL = (
    "⚠️ Не финансовый совет. Статистика прошлого не гарантирует будущего."
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


def _fmt_change(change: float | None) -> str:
    if change is None:
        return "н/д"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.2f}%"


def format_scan(findings: list[dict], timeframe: str, top: int = 10) -> str:
    """Собрать текст ответа на /scan (топ находок сканера)."""
    header = f"🔭 Скан рынка · таймфрейм {timeframe}"
    if not findings:
        return (
            f"{header}\n\nСейчас по топ-парам ярких ситуаций не найдено.\n\n{DISCLAIMER}"
        )

    lines = [header, f"Найдено ситуаций: {len(findings)} (показаны топ-{min(top, len(findings))})", ""]
    for f in findings[:top]:
        lines.append(
            f"• {f['symbol']}: {_fmt_price(f['price'])} "
            f"({_fmt_change(f['change_24h'])})\n"
            f"   {', '.join(f['patterns'])}"
        )
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def format_entry(result: dict) -> str:
    """Собрать текст ответа на /entry (скоринг условий входа)."""
    side_ru = _SIDE_RU.get(result["side"], result["side"])
    lines = [
        f"🎯 Оценка условий {side_ru}-входа · {result['symbol']} · "
        f"таймфрейм {result['timeframe']}",
        "",
        f"Балл условий: {result['score']}/100",
        "",
        "Факторы:",
    ]
    for f in result["factors"]:
        pts = f["points"]
        sign = f"+{pts}" if pts > 0 else (f"{pts}" if pts < 0 else "0")
        lines.append(f"• {f['label']}  ({sign})")
    lines.extend(
        [
            "",
            result["summary"],
            "",
            f"🔍 Прогнать идею через скептика: "
            f"/skeptic {result['symbol']} {result['side']} вход стоп тейк",
            "",
            DISCLAIMER,
        ]
    )
    return "\n".join(lines)


def format_skeptic(result: dict) -> str:
    """Собрать текст ответа на /skeptic."""
    side_ru = _SIDE_RU.get(result["side"], result["side"])
    symbol = result.get("symbol", "")
    lines = [
        f"🔍 Скептик · {symbol} · {side_ru}",
        f"Вход {_fmt_price(result['entry'])}, стоп {_fmt_price(result['stop'])}, "
        f"тейк {_fmt_price(result['take'])}",
        "",
    ]
    for c in result["checks"]:
        mark = "✅" if c["status"] == "PASS" else "⚠️"
        lines.append(f"{mark} {c['name']}: {c['detail']}")
    lines.extend(["", result["summary"], "", DISCLAIMER])
    return "\n".join(lines)


def format_exit(result: dict) -> str:
    """Собрать текст ответа на /exit (уровни стопа и тейков)."""
    side_ru = _SIDE_RU.get(result["side"], result["side"])
    snapped = result.get("snapped") or []
    snap_note = (
        f"подтянуты к уровням S/R: {', '.join(snapped)}"
        if snapped
        else "уровни S/R рядом не нашлись — чистый ATR"
    )
    lines = [
        f"🚪 План выхода · {side_ru} · {result['symbol']} · таймфрейм {result['timeframe']}",
        "",
        f"🎯 Вход: {_fmt_price(result['entry'])}",
        f"🛑 Стоп: {_fmt_price(result['stop'])}",
        f"✅ Тейк 1: {_fmt_price(result['take1'])}  (R:R ≈ {result['rr1']:.2f})",
        f"✅ Тейк 2: {_fmt_price(result['take2'])}  (R:R ≈ {result['rr2']:.2f})",
        "",
        f"📐 Основание: ATR(14) = {_fmt_price(result['atr'])} × {result['atr_mult']}; "
        f"{snap_note}.",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def _fmt_qty(value: float) -> str:
    """Количество монет: достаточно знаков для мелких значений."""
    if value >= 1000:
        return f"{value:,.3f}".replace(",", " ")
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_size(result: dict) -> str:
    """Собрать текст ответа на /size (размер позиции)."""
    lev = result["leverage"]
    lev_str = f"{lev:.0f}x" if float(lev).is_integer() else f"{lev:g}x"
    risk_per_unit = abs(result["entry_price"] - result["stop_price"])

    lines = [
        "💼 Размер позиции",
        "",
        f"Депозит: {_fmt_price(result['deposit_usdt'])} USDT",
        f"Риск: {result['risk_pct']:g}% = {_fmt_price(result['risk_usdt'])} USDT",
        f"Вход {_fmt_price(result['entry_price'])}, стоп {_fmt_price(result['stop_price'])} "
        f"(риск на единицу {_fmt_price(risk_per_unit)})",
        f"Плечо: {lev_str}",
        "",
        f"📦 Размер позиции: {_fmt_qty(result['qty'])} монет "
        f"(~{_fmt_price(result['position_usdt'])} USDT маржи)",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def format_risk(result: dict) -> str:
    """Собрать текст ответа на /risk (проверка риск-лимитов)."""
    mark = "✅" if result["verdict"] == "В пределах лимитов." else "⚠️"
    lines = [
        "🛡 Проверка риск-лимитов",
        "",
        f"Депозит: {_fmt_price(result['deposit_usdt'])} USDT",
        f"Риск на сделку: {result['risk_pct']:g}% "
        f"({'в норме' if result['per_trade_ok'] else 'выше 2%'})",
        f"Открытых сделок: {result['open_trades']} → "
        f"суммарный риск с новой: {result['total_risk_pct']:g}%",
        f"Лимит суммарного риска: {result['max_total_risk_pct']:g}%",
        "",
        f"{mark} Вердикт: {result['verdict']}",
        f"💡 Совет: {result['advice']}",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


def format_trade_closed(result: dict) -> str:
    """Собрать текст ответа на /close."""
    side_ru = _SIDE_RU.get(result["side"], result["side"])
    return "\n".join(
        [
            f"✅ Сделка №{result['id']} закрыта",
            f"{result['symbol']} · {side_ru}",
            f"Вход {_fmt_price(result['entry'])} → выход {_fmt_price(result['exit_price'])}",
            f"PnL: {result['pnl_usdt']:+.2f} USDT ({result['pnl_r']:+.2f} R)",
        ]
    )


def format_journal(st: dict) -> str:
    """Собрать текст ответа на /journal."""
    if st["total"] == 0:
        return "📒 Журнал сделок\n\nПока нет закрытых сделок.\n\n" + DISCLAIMER_JOURNAL

    lines = [
        "📒 Журнал сделок",
        "",
        f"Закрыто сделок: {st['total']}",
        f"Прибыльных: {st['wins']} (win rate {st['win_rate'] * 100:.1f}%)",
        f"Средний R: {st['avg_r']:+.2f}",
        f"Expectancy: {st['expectancy']:+.2f} R",
        f"Суммарный PnL: {st['total_pnl_usdt']:+.2f} USDT",
        f"Лучший R: {st['best_r']:+.2f}, худший R: {st['worst_r']:+.2f}",
        "",
        DISCLAIMER_JOURNAL,
    ]
    return "\n".join(lines)


def format_open_trades(trades: list[dict]) -> str:
    """Собрать текст ответа на /opentrades."""
    if not trades:
        return "📂 Открытых сделок нет."
    lines = ["📂 Открытые сделки:", ""]
    for t in trades:
        side_ru = _SIDE_RU.get(t["side"], t["side"])
        lines.append(
            f"№{t['id']} · {t['symbol']} · {side_ru}: "
            f"вход {_fmt_price(t['entry'])}, стоп {_fmt_price(t['stop'])}, "
            f"тейк {_fmt_price(t['take'])}, объём {_fmt_qty(t['qty'])}"
        )
    return "\n".join(lines)


PAPER_TAG = "🧪 Виртуальный портфель. Не финансовый совет."
_WHALE_SIDE_RU = {"buy": "покупка", "sell": "продажа"}
_PRESSURE_RU = {
    "покупки": "покупки преобладают",
    "продажи": "продажи преобладают",
    "смешанное": "смешанное",
    "нет китовых сделок": "китовых сделок не найдено",
}


def _fmt_usdt_short(value: float) -> str:
    """Компактный формат крупных сумм USDT."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def format_whales(result: dict) -> str:
    """Собрать текст ответа на /whales."""
    thr = _fmt_usdt_short(result["threshold"])
    header = (
        f"🐋 Киты {result['symbol']} · порог ≥ {thr} USDT · "
        f"окно ~{result['window_minutes']:.0f} мин (по 1000 сделкам)"
    )
    if result["whale_count"] == 0:
        return (
            f"{header}\n\nКитовых сделок в окне не найдено.\n\n"
            "Китовые сделки — факт прошлого, не рекомендация."
        )

    lines = [
        header,
        "",
        f"Китовых сделок: {result['whale_count']}",
        f"Покупки: {_fmt_usdt_short(result['buy_notional'])} USDT · "
        f"Продажи: {_fmt_usdt_short(result['sell_notional'])} USDT",
        f"Давление: {_PRESSURE_RU.get(result['pressure'], result['pressure'])}",
        "",
        "Топ крупнейших:",
    ]
    for w in result["top"]:
        side_ru = _WHALE_SIDE_RU.get(w["side"], w["side"])
        lines.append(
            f"• {side_ru}: {_fmt_qty(w['qty'])} @ {_fmt_price(w['price'])} "
            f"(~{_fmt_usdt_short(w['notional'])} USDT)"
        )
    lines.extend(["", "Китовые сделки — факт прошлого, не рекомендация."])
    return "\n".join(lines)
_REASON_RU = {"stop": "задет стоп", "take": "задет тейк", "manual": "вручную"}


def format_paper_portfolio(data: dict) -> str:
    """Собрать текст ответа на /paper."""
    lines = [
        "🧪 Виртуальный портфель",
        "",
        f"Свободный баланс: {_fmt_price(data['balance'])} USDT",
    ]
    positions = data.get("positions") or []
    if not positions:
        lines.append("Открытых позиций нет.")
    else:
        lines.append("")
        lines.append("Открытые позиции:")
        for p in positions:
            side_ru = _SIDE_RU.get(p["side"], p["side"])
            extra = []
            if p.get("stop") is not None:
                extra.append(f"стоп {_fmt_price(p['stop'])}")
            if p.get("take") is not None:
                extra.append(f"тейк {_fmt_price(p['take'])}")
            tail = (" · " + ", ".join(extra)) if extra else ""
            lines.append(
                f"№{p['id']} · {p['symbol']} · {side_ru}: "
                f"{_fmt_qty(p['qty'])} @ {_fmt_price(p['entry_price'])}{tail}"
            )
    lines.extend(["", PAPER_TAG])
    return "\n".join(lines)


def format_paper_opened(pos: dict) -> str:
    """Собрать текст ответа на /paperbuy и /papersell."""
    side_ru = _SIDE_RU.get(pos["side"], pos["side"])
    extra = []
    if pos.get("stop") is not None:
        extra.append(f"стоп {_fmt_price(pos['stop'])}")
    if pos.get("take") is not None:
        extra.append(f"тейк {_fmt_price(pos['take'])}")
    tail = ("\n" + " · ".join(extra)) if extra else ""
    return (
        f"🧪 Открыта виртуальная позиция №{pos['id']}: {pos['symbol']} · {side_ru}\n"
        f"{_fmt_qty(pos['qty'])} @ {_fmt_price(pos['entry_price'])} "
        f"(номинал {_fmt_price(pos['notional'])} USDT){tail}\n"
        f"Свободный баланс: {_fmt_price(pos['balance_after'])} USDT\n"
        f"Закрыть: /paperclose {pos['id']}\n\n{PAPER_TAG}"
    )


def format_paper_closed(result: dict) -> str:
    """Собрать текст ответа на /paperclose и уведомления об автозакрытии."""
    side_ru = _SIDE_RU.get(result["side"], result["side"])
    reason = _REASON_RU.get(result.get("reason", "manual"), "")
    reason_note = f" ({reason})" if reason and result.get("reason") != "manual" else ""
    return (
        f"🧪 Закрыта виртуальная позиция №{result['id']}{reason_note}: "
        f"{result['symbol']} · {side_ru}\n"
        f"Вход {_fmt_price(result['entry_price'])} → выход {_fmt_price(result['exit_price'])}\n"
        f"PnL: {result['pnl_usdt']:+.2f} USDT ({result['pnl_pct']:+.2f}%)\n"
        f"Свободный баланс: {_fmt_price(result['balance_after'])} USDT\n\n{PAPER_TAG}"
    )


AUTOBRIEF_HEADER = "🤖 Автодайджест. Наблюдения, не рекомендации. Не финансовый совет."


def format_brief(brief: dict, auto: bool = False) -> str:
    """Собрать текст дайджеста из структурированного результата координатора."""
    try:
        when = datetime.fromisoformat(brief["generated_at"]).strftime("%H:%M UTC")
    except (ValueError, KeyError):
        when = ""

    lines: list[str] = []
    if auto:
        lines.append(AUTOBRIEF_HEADER)
        lines.append("")
    lines.append(f"📋 Бриф ({when}) · таймфрейм {brief.get('timeframe', '')}")
    lines.append(f"Рынок: {brief.get('market_summary', '')}")

    news = brief.get("news") or []
    if news:
        lines.append("")
        lines.append("📰 Новости:")
        for it in news[:3]:
            lines.append(f"• {it['title']} — {it['source']}")

    ideas = brief.get("ideas") or []
    lines.append("")
    if ideas:
        lines.append("💡 Топ идеи (наблюдения):")
        for idea in ideas:
            side_ru = _SIDE_RU.get(idea["side"], idea["side"])
            line = (
                f"• {idea['symbol']} ({side_ru}): скоринг {idea['score']}/100; "
                f"вход {_fmt_price(idea['entry'])}, стоп {_fmt_price(idea['stop'])}, "
                f"тейк {_fmt_price(idea['take1'])}; скептик "
                f"{idea['skeptic_passed']}/{idea['skeptic_total']}"
            )
            lines.append(line)
            if idea.get("weak_points"):
                lines.append(f"   слабые места: {'; '.join(idea['weak_points'])}")
    else:
        lines.append("💡 Идей по текущим правилам не набралось.")

    filtered = brief.get("filtered_out") or []
    if filtered:
        lines.append("")
        lines.append("⚠️ Отсеяно риск-гейтом:")
        for f in filtered:
            lines.append(f"• {f['symbol']}: {f['reason']}")

    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def format_best(data: dict) -> str:
    """Собрать текст /best — топ-идеи по правилам (наблюдения, не рекомендации)."""
    try:
        when = datetime.fromisoformat(data["updated_at"]).strftime("%H:%M UTC")
    except (ValueError, KeyError, TypeError):
        when = ""
    ideas = data.get("ideas") or []
    lines = [f"💡 Топ-идеи ({when}) — {len(ideas)} сетапов",
             "Лучшие сетапы по правилам (скоринг ≥70, скептик не против, "
             "риск-гейт PASS). Наблюдения, не рекомендации.", ""]
    if not ideas:
        lines.append("Сильных сетапов по правилам сейчас нет.")
        lines.extend(["", DISCLAIMER])
        return "\n".join(lines)
    for i, idea in enumerate(ideas, 1):
        side_ru = _SIDE_RU.get(idea["side"], idea["side"])
        sk = idea.get("skeptic") or {}
        lines.append(f"{i}. {idea['pair']} · {side_ru} — сила {idea['score']}/100")
        lines.append(
            f"   вход {_fmt_price(idea['entry'])}, стоп {_fmt_price(idea['stop'])}, "
            f"тейк1 {_fmt_price(idea['take1'])} (R:R {idea['rr1']:.2f}), "
            f"тейк2 {_fmt_price(idea['take2'])}")
        lines.append(
            f"   скептик {sk.get('passed', '—')}/{sk.get('total', '—')} · "
            f"риск-гейт {idea.get('risk_gate', '—')}"
            + (f" · {idea['pattern']}" if idea.get("pattern") else ""))
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def format_push(idea: dict) -> str:
    """Собрать текст автопуша наблюдения."""
    side_ru = _SIDE_RU.get(idea["side"], idea["side"])
    score = idea.get("setup_score")
    if score is None:
        score = idea.get("score", "—")
    factors = idea.get("setup_factors") or []
    top2 = sorted(factors, key=lambda f: f.get("points", 0), reverse=True)[:2]
    top_names = ", ".join(f["name"] for f in top2) if top2 else "—"
    return (
        f"🔔 {idea['symbol']} · {side_ru}\n"
        f"Сила сетапа {score}/100 — {top_names}\n"
        f"Вход {_fmt_price(idea['entry'])}, стоп {_fmt_price(idea['stop'])}, "
        f"тейк {_fmt_price(idea['take1'])}\n"
        f"Скептик: пройдено {idea['skeptic_passed']}/{idea['skeptic_total']}\n"
        "Наблюдение, не рекомендация. Не финансовый совет."
    )


def format_paper_limit_fill(event: dict) -> str:
    """Собрать уведомление об исполнении лимитного ордера (paper)."""
    side = "покупка" if event["side"] == "buy" else "продажа"
    base = (
        f"🧪 Лимитный ордер №{event['orderId']} исполнен: {side} "
        f"{event['symbol']} · {_fmt_qty(event['qty'])} по {_fmt_price(event['fill_price'])}"
    )
    closed = event.get("closed")
    if closed:
        base += f"\nPnL: {closed['pnl_usdt']:+.2f} USDT ({closed['pnl_pct']:+.2f}%)"
    return base + "\n\n" + PAPER_TAG


def _cond_phrase(condition_type: str, threshold: float) -> str:
    """Человекочитаемое описание условия подписки."""
    if condition_type == "price_above":
        return f"цена выше {_fmt_price(threshold)}"
    if condition_type == "price_below":
        return f"цена ниже {_fmt_price(threshold)}"
    if condition_type == "rsi_above":
        return f"RSI выше {threshold:g}"
    if condition_type == "rsi_below":
        return f"RSI ниже {threshold:g}"
    if condition_type == "change24h_abs":
        return f"|изменение за 24ч| ≥ {threshold:g}%"
    if condition_type == "volume_spike":
        return f"объём ≥ x{threshold:g} от среднего"
    return f"{condition_type} {threshold:g}"


def _fmt_actual(condition_type: str, value) -> str:
    if value is None:
        return "н/д"
    if condition_type.startswith("price"):
        return _fmt_price(value)
    if condition_type.startswith("rsi"):
        return f"{value:.1f}"
    if condition_type == "change24h_abs":
        return f"{value:.2f}%"
    if condition_type == "volume_spike":
        return f"x{value:.2f}"
    return str(value)


def format_alert_added(alert: dict) -> str:
    """Собрать текст ответа на /watch."""
    phrase = _cond_phrase(alert["condition_type"], alert["threshold"])
    return (
        f"🔔 Подписка №{alert['id']} создана: {alert['symbol']} — {phrase}.\n"
        f"Одноразовая: сработает один раз. Отмена: /unwatch {alert['id']}"
    )


def format_watches(alerts: list[dict]) -> str:
    """Собрать текст ответа на /watches."""
    if not alerts:
        return "📭 Активных подписок нет. Создать: /watch BTCUSDT price_above 81000"
    lines = ["📬 Активные подписки:", ""]
    for a in alerts:
        phrase = _cond_phrase(a["condition_type"], a["threshold"])
        lines.append(f"№{a['id']} · {a['symbol']} — {phrase}")
    return "\n".join(lines)


def format_alert_fired(event: dict) -> str:
    """Собрать текст уведомления о срабатывании условия."""
    phrase = _cond_phrase(event["condition_type"], event["threshold"])
    actual = _fmt_actual(event["condition_type"], event.get("actual"))
    return (
        f"🔔 Условие сработало: {event['symbol']} {phrase} "
        f"(факт {actual}). Это уведомление, не рекомендация."
    )


def _ago(dt: datetime) -> str:
    """Человекочитаемое «сколько назад» для времени в UTC."""
    delta = datetime.now(timezone.utc) - dt
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    return f"{hours} ч назад"


def format_news(items: list[dict]) -> str:
    """Собрать текст ответа на /news."""
    if not items:
        return (
            "📰 Свежих новостей за последние 24 часа не нашлось "
            "(или источники сейчас недоступны)."
        )
    lines = ["📰 Свежие крипто-новости:", ""]
    for it in items:
        lines.append(
            f"• {it['title']}\n"
            f"   {it['source']} · {_ago(it['time'])}\n"
            f"   {it['url']}"
        )
    return "\n".join(lines)
