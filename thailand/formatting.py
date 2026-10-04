from __future__ import annotations

"""Telegram formatting for buy opportunity + Thailand comparison (≤3 messages)."""

from typing import Any, Sequence

from buy_opportunity import BuySignal
from thailand.comparison import verdict_label_ru
from thailand.eligibility import max_tracked_price_rub
from thailand.models import CountryComparison, CrossCountryMatch, FxRate, StoreScanResult
from thailand.seller_trust import store_display_label

FOOTER_LOCAL = (
    "Локальная покупка в Таиланде. "
    "Поездка, международная доставка и таможня не учитываются."
)
FOOTER_FX = (
    "Курс валют меняется; RUB equivalent рассчитан "
    "по указанному курсу на момент проверки."
)
FOOTER_CAP = (
    "Показаны только предложения до 300 000 ₽ "
    "по курсу на момент проверки."
)

TELEGRAM_SOFT = 3500


def _rub(n: int | None) -> str:
    if n is None:
        return "н/д"
    return f"{n:,}".replace(",", " ")


def _thb(n: int | None) -> str:
    if n is None:
        return "н/д"
    return f"{n:,}".replace(",", " ")


def _cfg(ram: int | None, ssd: int | None, screen: float | None) -> str:
    parts: list[str] = []
    if ram:
        parts.append(f"{ram}GB")
    if ssd:
        if ssd >= 1000 and ssd % 1024 == 0:
            parts.append(f"{ssd // 1024}TB")
        elif ssd >= 1000:
            parts.append("1TB" if ssd < 1500 else f"{ssd}GB")
        else:
            parts.append(f"{ssd}GB")
    if screen:
        parts.append(f'{screen:g}"')
    return " / ".join(parts) if parts else "конфигурация н/д"


def format_buy_opportunity_message(
    signal: BuySignal,
    *,
    thailand_queued: bool = False,
    thailand_enqueue_failed: bool = False,
) -> str:
    lines = [
        "🔥 <b>СЕЙЧАС ВЫГОДНЫЙ МОМЕНТ ДЛЯ ПОКУПКИ</b>",
        "",
        "<b>Россия</b>",
        "",
        f"1. {signal.cluster_name}",
    ]
    if signal.gpu:
        lines.append(str(signal.gpu))
    if signal.cpu:
        lines.append(str(signal.cpu))
    lines.append(_cfg(signal.ram_gb, signal.ssd_gb, signal.screen_inch))
    lines.append("")
    lines.append(f"Цена:\n{_rub(signal.price)} ₽ — {signal.store.upper()}")
    lines.append("")
    lines.append(f"Deal score:\n{signal.score:g}/100")
    lines.append("")
    lines.append(f"Confidence:\n{signal.confidence}%")
    if signal.historical_min:
        lines.append("")
        lines.append(f"Исторический минимум:\n{_rub(signal.historical_min)} ₽")
        if signal.over_hist_pct is not None:
            lines.append("")
            lines.append(f"Сейчас:\n+{signal.over_hist_pct * 100:.1f}% от минимума")
    if signal.target_price:
        lines.append("")
        lines.append(f"Target:\n{_rub(signal.target_price)} ₽")
    if signal.level == "STRONG_BUY":
        lines.append("")
        lines.append("Уровень: <b>STRONG_BUY</b>")
    lines.append("")
    lines.append("Почему сигнал:")
    for r in signal.reasons[:8]:
        lines.append(f"• {r}")
    if signal.url:
        lines.append("")
        lines.append(signal.url)
    if thailand_queued:
        lines.append("")
        lines.append(
            "Сравнение с Таиландом запущено и придёт отдельным сообщением."
        )
    elif thailand_enqueue_failed:
        lines.append("")
        lines.append("Сравнение с Таиландом сейчас запустить не удалось.")
    text = "\n".join(lines)
    return text[:TELEGRAM_SOFT]


def format_thailand_comparison_message(
    *,
    store_results: Sequence[StoreScanResult],
    fx: FxRate | None,
    match: CrossCountryMatch | None,
    comparison: CountryComparison | None,
    russian_signal: BuySignal | None = None,
    match_over_cap_note: str | None = None,
) -> str:
    lines = ["🇹🇭 <b>ТАИЛАНД — РАЗОВАЯ ПРОВЕРКА</b>", "", "Проверены:"]
    for sr in store_results:
        mark = "✅" if sr.ok else "❌"
        label = {
            "jib": "JIB",
            "advice": "Advice",
            "banana": "BaNANA",
            "lazada": "Lazada",
        }.get(sr.store, sr.store)
        extra = f" ({sr.error})" if (not sr.ok and sr.error) else ""
        lines.append(f"{label} {mark}{extra}")

    ok_count = sum(1 for s in store_results if s.ok)
    if ok_count == 0:
        lines = [
            "🇹🇭 Тайский рынок сейчас проверить не удалось.",
            "",
            FOOTER_LOCAL,
        ]
        return "\n".join(lines)[:TELEGRAM_SOFT]
    if ok_count < len(store_results):
        lines.append("")
        lines.append("Thailand data: <b>PARTIAL</b>")

    lines.append("")
    if fx and fx.rub_per_thb > 0 and not fx.error:
        stale = " (stale)" if fx.stale else ""
        lines.append("Курс:")
        lines.append(f"1 ฿ = {fx.rub_per_thb:.4f} ₽{stale}")
        lines.append(fx.source)
        if fx.published_date:
            lines.append(f"дата {fx.published_date}")
        if fx.stale and fx.rate_age_hours is not None:
            lines.append(f"age {fx.rate_age_hours:.0f}h stale=true")
    else:
        lines.append("⚠️ Актуальный курс THB/RUB получить не удалось.")
        lines.append(
            "Не удалось применить лимит 300 000 ₽: "
            "актуальный курс THB/RUB недоступен."
        )

    lines.append("")
    lines.append(f"Лимит:\n≤{_rub(max_tracked_price_rub())} ₽")

    if match and match.level in {"EXACT", "EQUIVALENT", "SAME_FAMILY"}:
        lines.append("")
        lines.append(f"<b>{match.level}</b>")
        lines.append("")
        lines.append("Россия:")
        name = russian_signal.cluster_name if russian_signal else match.russian_name
        lines.append(name)
        lines.append(f"{_rub(match.russian_price_rub)} ₽")
        lines.append("")
        lines.append("Таиланд:")
        lines.append(match.thai_offer.name)
        lines.append(f"{_thb(match.thai_price_thb)} ฿")
        rub_eq = _rub(match.thai_price_rub) if match.thai_price_rub is not None else "н/д"
        lines.append(f"≈ {rub_eq} ₽")
        lines.append(store_display_label(match.thai_offer))
        if match.thai_offer.availability_status == "store_pickup_only":
            lines.append("ограничение: store pickup only")
        if match.delta_rub is not None and match.delta_percent is not None:
            lines.append("")
            lines.append("Разница:")
            if match.delta_rub < 0:
                lines.append(
                    f"Таиланд дешевле: {match.delta_rub:,} ₽ / "
                    f"{match.delta_percent * 100:.1f}%".replace(",", " ")
                )
            else:
                lines.append(
                    f"Россия дешевле: {match.delta_rub:,} ₽ / "
                    f"{match.delta_percent * 100:.1f}%".replace(",", " ")
                )
        if match.differences:
            lines.append("")
            lines.append("Конфигурация:")
            for d in match.differences[:6]:
                lines.append(f"• {d}")
        if comparison:
            lines.append("")
            lines.append("Итог:")
            lines.append(verdict_label_ru(comparison.verdict))
            for r in comparison.reasons[:3]:
                lines.append(r)
    else:
        lines.append("")
        lines.append("EXACT / EQUIVALENT: не найдено")
        if match_over_cap_note:
            lines.append(match_over_cap_note)

    lines.append("")
    lines.append(FOOTER_LOCAL)
    lines.append(FOOTER_FX)
    lines.append(FOOTER_CAP)
    return "\n".join(lines)[:TELEGRAM_SOFT]


def format_thailand_alternatives_message(
    top: Sequence[dict[str, Any]],
    *,
    limit: int = 5,
    unverified_count: int = 0,
    price_cap: dict[str, Any] | None = None,
    fx_usable: bool | None = None,
) -> str:
    cap = price_cap or {}
    if fx_usable is False or (
        fx_usable is None and cap.get("fx_usable") is False
    ):
        return (
            "🇹🇭 <b>ЛУЧШИЕ ПРЕДЛОЖЕНИЯ В ТАИЛАНДЕ ДО 300 000 ₽</b>\n\n"
            "Не удалось применить лимит 300 000 ₽:\n"
            "актуальный курс THB/RUB недоступен.\n\n"
            f"{FOOTER_LOCAL}"
        )[:TELEGRAM_SOFT]
    if not top and not unverified_count:
        return ""
    lines = ["🇹🇭 <b>ЛУЧШИЕ ПРЕДЛОЖЕНИЯ В ТАИЛАНДЕ ДО 300 000 ₽</b>", ""]
    if cap:
        lines.append("Лимит:")
        lines.append(f"≤{_rub(int(cap.get('max_tracked_price_rub') or max_tracked_price_rub()))} ₽")
        lines.append("")
        lines.append("Найдено:")
        lines.append(f"verified: {int(cap.get('verified_count') or 0)}")
        lines.append(f"eligible до лимита: {int(cap.get('eligible_count') or 0)}")
        lines.append(f"дороже лимита: {int(cap.get('over_cap_count') or 0)}")
        lines.append("")
    urls: list[str] = []
    for i, row in enumerate(top[:limit], start=1):
        lines.append(f"{i}. {row.get('name') or '?'}")
        if row.get("gpu"):
            lines.append(str(row["gpu"]))
        lines.append(
            _cfg(row.get("ram_gb"), row.get("ssd_gb"), row.get("screen_size_inch"))
        )
        lines.append(f"{_thb(row.get('price_thb'))} ฿")
        rub = row.get("price_rub")
        lines.append(f"≈ {_rub(rub) if rub is not None else 'н/д'} ₽")
        lines.append(str(row.get("store_label") or store_display_label(row)))
        if row.get("availability_status") == "store_pickup_only":
            lines.append("pickup only")
        if row.get("international_score") is not None:
            lines.append(f"International score: {row['international_score']:g}")
        if row.get("international_confidence") is not None:
            lines.append(f"Confidence: {row['international_confidence']}%")
        if row.get("url") and len(urls) < 3:
            urls.append(str(row["url"]))
        lines.append("")
    if unverified_count > 0:
        lines.append(
            f"⚠️ Ещё найдено {unverified_count} непроверенных предложений "
            "(конфигурация или наличие требуют подтверждения)."
        )
        lines.append("")
    for u in urls:
        lines.append(u)
    lines.append("")
    lines.append(FOOTER_LOCAL)
    lines.append(FOOTER_FX)
    lines.append(FOOTER_CAP)
    text = "\n".join(lines).rstrip()
    return text[:TELEGRAM_SOFT]
