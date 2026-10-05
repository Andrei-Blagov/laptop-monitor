"""Deterministic "what to buy now" recommendation. No network and no LLM."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

import config
from buy_opportunity import evaluate_buy_rules
from deal_ranking import RankedDeal

BUY_NOW_RUSSIA = "BUY_NOW_RUSSIA"
THAILAND_BETTER = "THAILAND_BETTER"
WAIT = "WAIT"
GOOD_PRICE_NOT_URGENT = "GOOD_PRICE_NOT_URGENT"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

AT_HISTORICAL_LOW = "AT_HISTORICAL_LOW"
NEAR_HISTORICAL_LOW = "NEAR_HISTORICAL_LOW"
GOOD = "GOOD"
NORMAL = "NORMAL"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

EXACT = "EXACT"
SAME_FAMILY = "SAME_FAMILY"
EQUIVALENT = "EQUIVALENT"
NOT_FOUND = "NOT_FOUND"


@dataclass
class PriceGap:
    difference_rub: int | None = None
    difference_pct: float | None = None
    band: str | None = None  # comparable | slight | substantial
    thailand_cheaper: bool | None = None


@dataclass
class Recommendation:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    ru_price_quality: str = INSUFFICIENT_HISTORY
    history_days: float | None = None
    history_mature: bool = False
    history_min: int | None = None
    history_delta_pct: float | None = None
    buy_level: str | None = None
    thailand_match_level: str | None = None
    thailand_price: int | None = None
    thailand_store: str | None = None
    thailand_available: bool | None = None
    thailand_difference_rub: int | None = None
    thailand_difference_pct: float | None = None
    thailand_coverage_complete: bool = True
    country_comparison_trusted: bool = False
    spec_differences: list[str] = field(default_factory=list)
    equivalent_price: int | None = None
    equivalent_store: str | None = None
    coverage_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "ru_price_quality": self.ru_price_quality,
            "history_days": self.history_days,
            "history_mature": self.history_mature,
            "history_min": self.history_min,
            "history_delta_pct": self.history_delta_pct,
            "buy_level": self.buy_level,
            "thailand_match_level": self.thailand_match_level,
            "thailand_price": self.thailand_price,
            "thailand_store": self.thailand_store,
            "thailand_available": self.thailand_available,
            "thailand_difference_rub": self.thailand_difference_rub,
            "thailand_difference_pct": self.thailand_difference_pct,
            "thailand_coverage_complete": self.thailand_coverage_complete,
            "country_comparison_trusted": self.country_comparison_trusted,
            "spec_differences": list(self.spec_differences),
            "equivalent_price": self.equivalent_price,
            "equivalent_store": self.equivalent_store,
            "coverage_notes": list(self.coverage_notes),
        }


def _rub(value: int | None) -> str:
    if value is None:
        return "н/д"
    return f"{int(value):,}".replace(",", " ")


def _pct(value: float | None) -> str:
    if value is None:
        return "н/д"
    return f"{abs(value):.1f}".replace(".", ",")


_STORE = {
    "jib": "JIB",
    "advice": "Advice",
    "speedcom": "SpeedCom",
    "invadeit": "InvadeIT",
    "itcity": "IT City",
    "citilink": "Citilink",
    "regard": "Regard",
    "andpro": "ANDPRO",
    "kns": "KNS",
}


def _store_label(store: str | None) -> str:
    return _STORE.get(str(store or "").lower(), str(store or ""))


def format_purchase_recommendation(
    deal: RankedDeal,
    rec: Recommendation,
    *,
    alternatives: Sequence[dict[str, Any]] | None = None,
) -> str:
    """One user-facing recommendation. Internal enums stay out of the text."""
    lines = ["🏆 <b>ЧТО ПОКУПАТЬ СЕЙЧАС</b>", ""]
    lines.append(str(deal.cluster_name or "Модель"))
    bits = [b for b in (deal.gpu, deal.cpu) if b]
    mem = []
    if deal.ram_gb:
        mem.append(f"{int(deal.ram_gb)} GB")
    if deal.ssd_gb:
        mem.append(f"{int(deal.ssd_gb)} GB" if int(deal.ssd_gb) < 1000 else f"{int(deal.ssd_gb) // 1024} TB")
    spec = " / ".join(bits + mem)
    if spec:
        lines.append(spec)
    lines.append("")
    lines.append("🇷🇺 Россия")
    lines.append(f"{_rub(deal.price)} ₽ — {_store_label(deal.store)}")
    if rec.history_min is not None:
        lines.append(f"Исторический минимум: {_rub(rec.history_min)} ₽")
    if rec.history_delta_pct is not None:
        if rec.history_delta_pct <= 0.05:
            lines.append("Сейчас около исторического минимума.")
        else:
            lines.append(f"Сейчас +{_pct(rec.history_delta_pct)}% от минимума.")
    if not rec.history_mature:
        lines.append("Истории цены меньше 14 дней: это не вывод, что сейчас исторически лучшее время.")
    lines.append("")
    lines.append("🇹🇭 Таиланд")
    level = rec.thailand_match_level
    if level == EXACT and rec.thailand_price is not None:
        lines.append("та же модель")
    elif level == SAME_FAMILY:
        lines.append("та же серия / региональная модификация")
    elif level == EQUIVALENT:
        lines.append("это не та же модель, а сопоставимая конфигурация")
    else:
        lines.append("Прямого сравнения с Таиландом нет.")
    if rec.thailand_available is False:
        lines.append("Найдена, сейчас нет в наличии.")
        if rec.thailand_price is not None:
            lines.append(f"{_rub(rec.thailand_price)} ₽ — {_store_label(rec.thailand_store)} (не в наличии)")
    elif rec.country_comparison_trusted and rec.thailand_price is not None:
        lines.append(f"{_rub(rec.thailand_price)} ₽ — {_store_label(rec.thailand_store)}")
        rub = rec.thailand_difference_rub
        pct = rec.thailand_difference_pct
        if rub is not None and pct is not None and rub > 0:
            lines.append(f"Таиланд дешевле на {_rub(rub)} ₽ / {_pct(pct)}% от российской цены.")
        elif rub is not None and pct is not None and rub < 0 and rec.thailand_coverage_complete:
            lines.append(f"Россия дешевле на {_rub(abs(rub))} ₽ / {_pct(pct)}% от российской цены.")
        elif rub is not None and abs(rub) > 0:
            lines.append("Проверенная цена близка к российской.")
        else:
            lines.append("Цены примерно сопоставимы.")
    elif rec.equivalent_price is not None and deal.price:
        gap = country_price_gap(deal.price, rec.equivalent_price)
        if gap.thailand_cheaper and gap.difference_rub is not None:
            lines.append(
                "Сопоставимая конфигурация в Таиланде дешевле на "
                f"{_rub(gap.difference_rub)} ₽ / {_pct(gap.difference_pct)}%."
            )
            lines.append(f"{_rub(rec.equivalent_price)} ₽ — {_store_label(rec.equivalent_store)}")
    if rec.spec_differences:
        lines.append("Конфигурация отличается, поэтому цену нельзя сравнивать напрямую:")
        lines.extend(rec.spec_differences)
    if not rec.thailand_coverage_complete:
        lines.append("Часть магазинов Таиланда сейчас не проверена, сравнение неполное.")
    lines.append("")
    lines.append("📊 Почему")
    why = _why_lines(rec)
    lines.extend(f"• {item}" for item in why[:4])
    lines.append("")
    lines.append("✅ Вывод")
    lines.append(_conclusion(deal, rec))
    alts = [row for row in (alternatives or []) if row.get("name")][:2]
    if alts:
        lines.append("")
        lines.append("Альтернативы в Таиланде:")
        for i, row in enumerate(alts, start=1):
            lines.append(
                f"{i}. {row.get('name')} — {_rub(row.get('price_rub'))} ₽, {_store_label(row.get('store'))}"
            )
    return "\n".join(lines)[:3500]


def _why_lines(rec: Recommendation) -> list[str]:
    out: list[str] = []
    if "strong_buy" in rec.reasons:
        out.append("По правилам покупки в России это сильный сигнал.")
    elif "buy_signal" in rec.reasons:
        out.append("По правилам покупки в России это сигнал BUY.")
    if rec.ru_price_quality == AT_HISTORICAL_LOW:
        out.append("Российская цена у своего минимума.")
    elif rec.ru_price_quality == NEAR_HISTORICAL_LOW:
        out.append("Российская цена рядом с минимумом.")
    elif rec.ru_price_quality == NORMAL:
        out.append("Российская цена заметно выше своего минимума.")
    if "thai_not_found" in rec.reasons:
        out.append("Прямого предложения в Таиланде нет. Это не довод в пользу российской цены.")
    if "thai_out_of_stock" in rec.reasons:
        out.append("Тайская цена есть, но купить её сейчас нельзя.")
    if "thai_specs_differ" in rec.reasons:
        out.append("Серия похожа, но конфигурация другая.")
    if "thai_equivalent_alternative" in rec.reasons:
        out.append("В Таиланде есть другая модель с похожей конфигурацией.")
    if any(item.startswith("thai_exact_cheaper") or item.startswith("thai_same_family_cheaper") for item in rec.reasons):
        out.append("Проверенное предложение в Таиланде заметно дешевле.")
    if "country_comparable" in rec.reasons:
        out.append("Проверенные цены России и Таиланда близки.")
    if "thai_coverage_incomplete" in rec.reasons:
        out.append("Не все тайские магазины ответили.")
    if "history_immature" in rec.reasons:
        out.append("История цены ещё короткая.")
    return out or ["Собраны текущая цена, история и доступное сравнение рынков."]


def _conclusion(deal: RankedDeal, rec: Recommendation) -> str:
    if rec.verdict == THAILAND_BETTER:
        return (
            "По цене Таиланд заметно выгоднее. "
            "Если ноутбук нужен прямо сейчас в России, смотрите текущую цену отдельно: "
            "покупка в Таиланде может быть недоступна."
        )
    if rec.verdict == BUY_NOW_RUSSIA:
        extra = ""
        if rec.thailand_difference_pct and 0 < rec.thailand_difference_pct < float(config.RECOMMENDATION_COUNTRY_CLEAR_PCT):
            extra = " В Таиланде чуть дешевле, но разница не большая."
        return (
            "Если ноутбук нужен сейчас в России — цена хорошая, сигнал сильный."
            + extra
        )
    if rec.verdict == WAIT:
        return "Сейчас в России цена заметно выше своего минимума. Имеет смысл подождать новую проверку, а не покупать срочно."
    if rec.verdict == GOOD_PRICE_NOT_URGENT:
        hole = ""
        if rec.thailand_match_level in {None, NOT_FOUND} or not rec.country_comparison_trusted:
            hole = " Это не вывод, какая страна дешевле."
        return "Российская цена выглядит хорошей, но это не срочный сигнал покупать." + hole
    return "Данных пока мало для категоричного «покупать»: короткая история или неполное сравнение рынков."


def history_days_for_deal(deal: RankedDeal, now: datetime | None = None) -> float | None:
    """Same clock as BUY: days since the cluster's earliest stored observation."""
    raw = deal.history_started_at
    if not raw:
        return None
    try:
        started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - started).total_seconds() / 86400.0


def russian_price_quality(deal: RankedDeal, *, history_mature: bool) -> tuple[str, float | None]:
    price = deal.price
    hist = deal.historical_min
    over = None
    if price is not None and hist is not None and int(hist) > 0 and int(price) > 0:
        over = (int(price) - int(hist)) / float(hist)
    if not history_mature or over is None:
        return INSUFFICIENT_HISTORY, over
    if over <= 0.01:
        return AT_HISTORICAL_LOW, over
    if over <= float(config.BUY_STRONG_MAX_OVER_HIST_PCT):
        return NEAR_HISTORICAL_LOW, over
    if over <= float(config.BUY_RULE_A_MAX_OVER_HIST_PCT):
        return GOOD, over
    return NORMAL, over


def country_price_gap(ru_price: int | None, th_price: int | None) -> PriceGap:
    """(RU − TH) / RU. Positive percent means Thailand is cheaper."""
    if ru_price is None or th_price is None or int(ru_price) <= 0 or int(th_price) <= 0:
        return PriceGap()
    rub = int(ru_price) - int(th_price)
    pct = rub / float(ru_price) * 100.0
    mag = abs(pct)
    slight = float(config.RECOMMENDATION_COUNTRY_SLIGHT_PCT)
    clear = float(config.RECOMMENDATION_COUNTRY_CLEAR_PCT)
    if mag < slight:
        band = "comparable"
    elif mag < clear:
        band = "slight"
    else:
        band = "substantial"
    return PriceGap(difference_rub=rub, difference_pct=pct, band=band, thailand_cheaper=rub > 0)


def coverage_notes_from_stores(stores: Sequence[dict[str, Any]] | None) -> tuple[bool, list[str]]:
    """Enabled stores that were not actually checked. Policy-disabled stores are ignored."""
    notes: list[str] = []
    for row in stores or []:
        if row.get("policy_disabled") or row.get("collection_mode") in {"disabled"}:
            continue
        code = str(row.get("error_code") or "")
        mode = str(row.get("collection_mode") or "")
        if code == "NO_RESULTS":
            continue
        if code == "RATE_LIMITED" or mode == "rate_limited":
            notes.append(f"{row.get('store')}:rate_limited")
            continue
        if row.get("ok") is False or code in {"TIMEOUT", "NETWORK_ERROR", "HTTP_ERROR", "BLOCKED", "CHALLENGE"}:
            notes.append(f"{row.get('store')}:{code or mode or 'failed'}")
            continue
        if mode in {"skipped", "failed"}:
            notes.append(f"{row.get('store')}:{mode}")
    return (not notes), notes


def _in_stock_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if not row.get("out_of_stock") and row.get("price_rub") is not None and int(row.get("price_rub") or 0) > 0
    ]


def build_recommendation(
    deal: RankedDeal,
    search: dict[str, Any] | None = None,
    *,
    stores: Sequence[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> Recommendation:
    """One verdict from the current Russian deal and an already collected Thailand search."""
    search = search or {}
    days = history_days_for_deal(deal, now)
    mature = days is not None and days >= float(config.BUY_MIN_HISTORY_DAYS)
    quality, over = russian_price_quality(deal, history_mature=mature)
    signal = evaluate_buy_rules(deal)
    buy_level = signal.level if signal is not None else None
    coverage_ok, coverage_notes = coverage_notes_from_stores(stores)
    level = str(search.get("match_level") or NOT_FOUND)
    exact = list(search.get("exact_matches") or [])
    family = list(search.get("same_family_matches") or [])
    equivalent = list(search.get("equivalent_matches") or [])

    trusted_row = None
    spec_diffs: list[str] = []
    if level == EXACT:
        trusted_row = (_in_stock_rows(exact) or [None])[0]
    elif level == SAME_FAMILY:
        stocked = _in_stock_rows(family)
        if stocked and not list(stocked[0].get("spec_differences") or []):
            trusted_row = stocked[0]
        elif stocked:
            spec_diffs = list(stocked[0].get("spec_differences") or [])
        elif family:
            spec_diffs = list(family[0].get("spec_differences") or [])

    oos_row = None
    pool = exact if level == EXACT else family if level == SAME_FAMILY else []
    if trusted_row is None and pool and all(row.get("out_of_stock") for row in pool):
        oos_row = pool[0]

    eq_row = (_in_stock_rows(equivalent) or [None])[0]
    gap = country_price_gap(deal.price, None if trusted_row is None else trusted_row.get("price_rub"))
    trusted = trusted_row is not None and gap.difference_pct is not None
    clear = float(config.RECOMMENDATION_COUNTRY_CLEAR_PCT)
    thailand_better = bool(trusted and gap.thailand_cheaper and (gap.difference_pct or 0) >= clear)

    reasons: list[str] = []
    if buy_level == "STRONG_BUY":
        reasons.append("strong_buy")
    elif buy_level == "BUY":
        reasons.append("buy_signal")
    reasons.append("history_mature" if mature else "history_immature")
    if quality == AT_HISTORICAL_LOW:
        reasons.append("ru_at_historical_low")
    elif quality == NEAR_HISTORICAL_LOW:
        reasons.append("ru_near_historical_low")
    elif quality == GOOD:
        reasons.append("ru_good_price")
    elif quality == NORMAL:
        reasons.append("ru_above_historical_low")
    if not coverage_ok:
        reasons.append("thai_coverage_incomplete")
    if level == NOT_FOUND:
        reasons.append("thai_not_found")
    elif oos_row is not None:
        reasons.append("thai_out_of_stock")
    elif spec_diffs:
        reasons.append("thai_specs_differ")
    elif trusted and thailand_better:
        pct = gap.difference_pct or 0
        kind = "exact" if level == EXACT else "same_family"
        reasons.append(f"thai_{kind}_cheaper_{pct:.1f}_pct")
        reasons.append("thai_available")
    elif trusted and gap.band == "comparable":
        reasons.append("country_comparable")
        reasons.append("thai_available")
    elif eq_row is not None and country_price_gap(deal.price, eq_row.get("price_rub")).thailand_cheaper:
        reasons.append("thai_equivalent_alternative")

    if thailand_better:
        verdict = THAILAND_BETTER
    elif not mature:
        verdict = INSUFFICIENT_DATA
    elif (
        buy_level == "STRONG_BUY"
        and trusted
        and not thailand_better
        and (coverage_ok or trusted_row is not None)
    ):
        verdict = BUY_NOW_RUSSIA
    elif quality == NORMAL and buy_level is None:
        verdict = WAIT
    elif quality in {AT_HISTORICAL_LOW, NEAR_HISTORICAL_LOW, GOOD}:
        verdict = GOOD_PRICE_NOT_URGENT
    elif buy_level is None:
        verdict = WAIT
    else:
        verdict = INSUFFICIENT_DATA

    # A missed Thailand store must not become "Russia is the better country".
    if verdict == BUY_NOW_RUSSIA and not coverage_ok and not trusted:
        verdict = GOOD_PRICE_NOT_URGENT if quality in {AT_HISTORICAL_LOW, NEAR_HISTORICAL_LOW, GOOD} else INSUFFICIENT_DATA

    shown = trusted_row or oos_row
    return Recommendation(
        verdict=verdict,
        reasons=reasons,
        ru_price_quality=quality,
        history_days=None if days is None else round(days, 2),
        history_mature=mature,
        history_min=deal.historical_min,
        history_delta_pct=None if over is None else round(over * 100.0, 2),
        buy_level=buy_level,
        thailand_match_level=level,
        thailand_price=None if shown is None else shown.get("price_rub"),
        thailand_store=None if shown is None else shown.get("store"),
        thailand_available=False if oos_row is not None and trusted_row is None else (True if trusted_row else None),
        thailand_difference_rub=gap.difference_rub if trusted else None,
        thailand_difference_pct=None if not trusted or gap.difference_pct is None else round(gap.difference_pct, 2),
        thailand_coverage_complete=coverage_ok,
        country_comparison_trusted=trusted,
        spec_differences=spec_diffs,
        equivalent_price=None if eq_row is None else eq_row.get("price_rub"),
        equivalent_store=None if eq_row is None else eq_row.get("store"),
        coverage_notes=coverage_notes,
    )
