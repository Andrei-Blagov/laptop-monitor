from __future__ import annotations

"""Telegram formatting for buy opportunity + Thailand comparison (≤3 messages)."""

import re
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
    banana_via_lazada = False
    labels = {
        "jib": "JIB",
        "advice": "Advice",
        "banana": "BaNANA",
        "lazada": "Lazada",
        "speedcom": "SpeedCom",
        "invadeit": "InvadeIT",
        "itcity": "IT City",
    }
    for sr in store_results:
        label = labels.get(sr.store, sr.store)
        if getattr(sr, "policy_disabled", False) or getattr(sr, "circuit_breaker_status", None) == "open":
            lines.append(f"{label} — временно отключён")
            continue
        if sr.error_code == "RATE_LIMITED" or sr.collection_mode == "rate_limited":
            lines.append(f"{label} — временно не проверялся из-за лимита запросов")
            continue
        if sr.error_code == "NO_RESULTS":
            lines.append(f"{label} — предложений нет")
            continue
        if sr.ok:
            lines.append(f"{label} ✅")
        else:
            lines.append(f"{label} — временно недоступен")
        if sr.store == "lazada" and sr.ok:
            # Detect BaNANA IT presence from offers if available on result
            for o in list(sr.offers or []) + list(sr.unverified_candidates or []):
                from thailand.seller_trust import is_banana_it_seller

                if getattr(o, "retailer_brand", None) == "BaNANA" or is_banana_it_seller(
                    getattr(o, "seller_name", None)
                ):
                    banana_via_lazada = True
                    break
    if banana_via_lazada:
        lines.append("BaNANA IT через Lazada ✅")

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


def _family_series_label(target: dict[str, Any], family_key: str) -> str:
    raw = re.sub(
        r"^(ноутбук|notebook|laptop)\s+",
        "",
        str(target.get("cluster_name") or ""),
        flags=re.I,
    ).strip()
    if family_key:
        idx = raw.upper().find(family_key.upper())
        if idx >= 0:
            return raw[: idx + len(family_key)].strip(" -")
    brand = str(target.get("brand") or "").strip()
    return " ".join(part for part in (brand, family_key) if part) or raw or "Модель"


def format_target_model_message(
    target: dict[str, Any],
    search: dict[str, Any],
) -> str:
    """First Thailand message: this Russian model, then the market TOP is separate."""
    lines = ["🇹🇭 <b>ЭТА МОДЕЛЬ В ТАИЛАНДЕ</b>", ""]
    title = target.get("cluster_name") or target.get("canonical_model_code") or "Модель"
    lines.append(str(title))
    bits = [str(target.get("gpu") or ""), str(target.get("cpu") or "")]
    spec = " / ".join(b for b in bits if b)
    if spec:
        lines.append(spec)
    lines.append("")
    lines.append("Россия:")
    price = target.get("price")
    store = target.get("store") or ""
    lines.append(f"{_rub(price) if price is not None else 'н/д'} ₽ — {store}")
    lines.append("")
    lines.append("Таиланд:")
    level = search.get("match_level") or "NOT_FOUND"
    exact = list(search.get("exact_matches") or [])
    family = list(search.get("same_family_matches") or [])
    equivalent = list(search.get("equivalent_matches") or [])
    if level == "NOT_FOUND":
        lines.append("Точная модификация в Таиланде не найдена.")
    elif exact:
        primary = exact[0]
        lines.append("EXACT")
        label = {
            "jib": "JIB",
            "advice": "Advice",
            "speedcom": "SpeedCom",
            "invadeit": "InvadeIT",
            "itcity": "IT City",
        }.get(str(primary.get("store")), str(primary.get("store")))
        lines.append(label)
        if primary.get("price_thb") is not None:
            lines.append(f"{_thb(primary.get('price_thb'))} ฿")
        rub = primary.get("price_rub")
        if rub is not None:
            lines.append(f"≈ {_rub(rub)} ₽")
        if primary.get("out_of_stock"):
            lines.append("Найдена, сейчас нет в наличии.")
        elif primary.get("over_cap"):
            lines.append("Точная модель найдена, но цена выше вашего лимита 300 000 ₽.")
        else:
            lines.append("в наличии")
        ru = target.get("price")
        if rub is not None and ru is not None and not primary.get("out_of_stock"):
            delta = int(ru) - int(rub)
            lines.append("")
            lines.append("Разница:")
            if delta > 0:
                lines.append(f"Таиланд дешевле на {_rub(delta)} ₽")
            elif delta < 0:
                lines.append(f"Россия дешевле на {_rub(abs(delta))} ₽")
            else:
                lines.append("Цены совпадают")
        others = exact[1:3]
        if others:
            lines.append("")
            lines.append("Другие магазины:")
            for alt in others:
                alt_label = {
                    "jib": "JIB",
                    "speedcom": "SpeedCom",
                    "invadeit": "InvadeIT",
                    "itcity": "IT City",
                    "advice": "Advice",
                }.get(str(alt.get("store")), str(alt.get("store")))
                lines.append(
                    f"{alt_label} — {_thb(alt.get('price_thb'))} ฿ (≈ {_rub(alt.get('price_rub'))} ₽)"
                )
    if not exact and family:
        row = family[0]
        family_key = str(row.get("family_key") or search.get("family_key") or "")
        ru_code = str(row.get("matched_identifier") or "")
        th_code = str(row.get("matched_offer_identifier") or row.get("manufacturer_part_number") or "")
        lines.append("Та же серия / семейство:")
        lines.append(_family_series_label(target, family_key))
        lines.append("")
        lines.append("Россия:")
        if ru_code:
            lines.append(ru_code)
        lines.append("Таиланд:")
        if th_code:
            lines.append(th_code)
        label = {
            "jib": "JIB",
            "advice": "Advice",
            "speedcom": "SpeedCom",
            "invadeit": "InvadeIT",
            "itcity": "IT City",
        }.get(str(row.get("store")), str(row.get("store") or ""))
        if row.get("price_thb") is not None:
            rub = row.get("price_rub")
            rub_bit = f" (≈ {_rub(rub)} ₽)" if rub is not None else ""
            lines.append(f"{label} — {_thb(row.get('price_thb'))} ฿{rub_bit}")
        if row.get("out_of_stock"):
            lines.append("Найдена, сейчас нет в наличии.")
        lines.append("Модификация и региональный индекс отличаются.")
        diffs = list(row.get("spec_differences") or [])
        if diffs:
            lines.append("")
            lines.append("Отличия конфигурации:")
            lines.extend(str(item) for item in diffs)
        others = family[1:3]
        if others:
            lines.append("")
            lines.append("Другие магазины:")
            for alt in others:
                alt_label = {
                    "jib": "JIB",
                    "speedcom": "SpeedCom",
                    "invadeit": "InvadeIT",
                    "itcity": "IT City",
                    "advice": "Advice",
                }.get(str(alt.get("store")), str(alt.get("store")))
                stock = ", нет в наличии" if alt.get("out_of_stock") else ""
                lines.append(
                    f"{alt_label} — {_thb(alt.get('price_thb'))} ฿ "
                    f"(≈ {_rub(alt.get('price_rub'))} ₽){stock}"
                )
    if not exact and not family and equivalent:
        lines.append("Точной модели нет. Ближайшая эквивалентная конфигурация:")
        lines.append(str(equivalent[0].get("name") or ""))
    for skipped in search.get("stores_skipped") or []:
        if skipped.get("reason") in {"RATE_LIMITED", "rate_limited"}:
            name = {
                "speedcom": "SpeedCom",
                "jib": "JIB",
                "invadeit": "InvadeIT",
                "itcity": "IT City",
                "advice": "Advice",
            }.get(str(skipped.get("store")), str(skipped.get("store")))
            lines.append("")
            lines.append(f"{name} временно не проверялся из-за лимита запросов.")
    lines.append("")
    lines.append(FOOTER_LOCAL)
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
            "🇹🇭 <b>ЛУЧШИЕ ЦЕНЫ В ТАИЛАНДЕ ДО 300 000 ₽</b>\n\n"
            "Не удалось применить лимит 300 000 ₽:\n"
            "актуальный курс THB/RUB недоступен.\n\n"
            f"{FOOTER_LOCAL}"
        )[:TELEGRAM_SOFT]
    if not top and not unverified_count:
        return ""
    lines = ["🇹🇭 <b>ЛУЧШИЕ ЦЕНЫ В ТАИЛАНДЕ ДО 300 000 ₽</b>", ""]
    if cap:
        lines.append("Лимит:")
        lines.append(f"≤{_rub(int(cap.get('max_tracked_price_rub') or max_tracked_price_rub()))} ₽")
        lines.append("")
        lines.append(
            f"Исключено дороже 300 000 ₽: {int(cap.get('over_cap_count') or 0)}"
        )
        lines.append("")
    for i, row in enumerate(top[:limit], start=1):
        lines.append(f"{i}. {row.get('name') or '?'}")
        if row.get("gpu"):
            lines.append(str(row["gpu"]))
        if row.get("cpu"):
            lines.append(str(row["cpu"]))
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
            lines.append(f"Value score: {row['international_score']:g}")
        if row.get("international_confidence") is not None:
            lines.append(f"Confidence: {row['international_confidence']}%")
        if row.get("url"):
            lines.append(str(row["url"]))
        alts = row.get("alt_channels") or []
        if alts:
            lines.append("Другие магазины:")
            for alt in alts[:2]:
                alt_label = alt.get("store_label") or store_display_label(alt)
                lines.append(
                    f"• {alt_label}: {_thb(alt.get('price_thb'))} ฿ "
                    f"(≈ {_rub(alt.get('price_rub'))} ₽)"
                )
        lines.append("")
    if unverified_count > 0:
        lines.append(
            f"⚠️ Ещё найдено {unverified_count} непроверенных предложений "
            "(конфигурация или наличие требуют подтверждения)."
        )
        lines.append("")
    lines.append(FOOTER_LOCAL)
    lines.append(FOOTER_FX)
    lines.append(FOOTER_CAP)
    text = "\n".join(lines).rstrip()
    return text[:TELEGRAM_SOFT]
