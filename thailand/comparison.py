from __future__ import annotations

"""Russia vs Thailand country comparison / verdicts."""

import config
from thailand.fx import fx_usable_for_verdict
from thailand.models import CountryComparison, CrossCountryMatch, FxRate

VERDICT_RUSSIA_CLEARLY = "RUSSIA_CLEARLY_BETTER"
VERDICT_RUSSIA_SLIGHTLY = "RUSSIA_SLIGHTLY_BETTER"
VERDICT_EQUAL = "ROUGHLY_EQUAL"
VERDICT_THAI_SLIGHTLY = "THAILAND_SLIGHTLY_BETTER"
VERDICT_THAI_CLEARLY = "THAILAND_CLEARLY_BETTER"
VERDICT_NO_COMPARABLE = "NO_COMPARABLE_MODEL"
VERDICT_FX_UNAVAILABLE = "FX_UNAVAILABLE"
VERDICT_CONFIG_DIFFERS = "CONFIGURATION_DIFFERS"
VERDICT_THAI_SPEC_HIGHER = "THAILAND_BETTER_SPEC_HIGHER_PRICE"


def _price_band_verdict(delta_percent: float) -> str:
    """delta_percent = (thai_rub - russian_rub) / russian_rub."""
    equal = float(config.COUNTRY_VERDICT_EQUAL_PCT)
    slight = float(config.COUNTRY_VERDICT_SLIGHT_PCT)
    ad = abs(delta_percent)
    if ad <= equal:
        return VERDICT_EQUAL
    if delta_percent < 0:
        return VERDICT_THAI_CLEARLY if ad > slight else VERDICT_THAI_SLIGHTLY
    return VERDICT_RUSSIA_CLEARLY if ad > slight else VERDICT_RUSSIA_SLIGHTLY


def _spec_advantage(differences: list[str]) -> str | None:
    thai_better = False
    ru_better = False
    for d in differences:
        if not (d.startswith("ram:") or d.startswith("ssd:")):
            continue
        try:
            a, b = d.split(":", 1)[1].split("vs")
            if int(b) > int(a):
                thai_better = True
            elif int(a) > int(b):
                ru_better = True
        except ValueError:
            continue
    if thai_better and not ru_better:
        return "thai"
    if ru_better and not thai_better:
        return "russia"
    if thai_better or ru_better:
        return "mixed"
    return None


def country_verdict(
    match: CrossCountryMatch | None,
    *,
    fx: FxRate | None,
) -> CountryComparison:
    if match is None:
        return CountryComparison(
            verdict=VERDICT_NO_COMPARABLE,
            reasons=["нет сопоставимой модели в Таиланде"],
        )

    if match.level == "ALTERNATIVE":
        return CountryComparison(
            verdict=VERDICT_NO_COMPARABLE,
            reasons=["только альтернативы, не та же конфигурация"],
            match=match,
        )

    if not fx_usable_for_verdict(fx):
        return CountryComparison(
            verdict=VERDICT_FX_UNAVAILABLE,
            reasons=["нет актуального курса THB/RUB для вердикта"],
            match=match,
        )

    if match.thai_price_rub is None or match.russian_price_rub is None:
        return CountryComparison(
            verdict=VERDICT_FX_UNAVAILABLE,
            reasons=["не удалось рассчитать RUB equivalent"],
            match=match,
        )

    diffs = list(match.differences or [])
    hard_diffs = [
        d
        for d in diffs
        if d.startswith("ram:")
        or d.startswith("ssd:")
        or d.startswith("screen:")
        or d in {"cpu", "gpu"}
    ]
    if match.level == "SAME_FAMILY" and hard_diffs:
        adv = _spec_advantage(hard_diffs)
        reasons = [f"конфигурация отличается: {', '.join(hard_diffs)}"]
        if adv == "thai" and match.delta_percent is not None and match.delta_percent > 0:
            return CountryComparison(
                verdict=VERDICT_THAI_SPEC_HIGHER,
                reasons=reasons + ["Таиланд дороже, но спецификация выше"],
                match=match,
            )
        return CountryComparison(
            verdict=VERDICT_CONFIG_DIFFERS,
            reasons=reasons,
            match=match,
        )

    if match.delta_percent is None:
        return CountryComparison(
            verdict=VERDICT_FX_UNAVAILABLE,
            reasons=["delta недоступна"],
            match=match,
        )

    verdict = _price_band_verdict(match.delta_percent)
    reasons: list[str] = []
    if match.delta_rub is not None:
        if match.delta_rub < 0:
            reasons.append(
                f"Таиланд дешевле: {match.delta_rub:,} ₽ / "
                f"{match.delta_percent * 100:.1f}%".replace(",", " ")
            )
        elif match.delta_rub > 0:
            reasons.append(
                f"Россия дешевле: {match.delta_rub:,} ₽ / "
                f"{match.delta_percent * 100:.1f}%".replace(",", " ")
            )
        else:
            reasons.append("цены эквивалентны")
    reasons.append(f"match={match.level}")
    return CountryComparison(verdict=verdict, reasons=reasons, match=match)


def verdict_label_ru(verdict: str) -> str:
    return {
        VERDICT_RUSSIA_CLEARLY: "Россия заметно выгоднее",
        VERDICT_RUSSIA_SLIGHTLY: "Россия чуть выгоднее",
        VERDICT_EQUAL: "Примерно одинаково",
        VERDICT_THAI_SLIGHTLY: "Таиланд чуть выгоднее",
        VERDICT_THAI_CLEARLY: "Таиланд заметно выгоднее",
        VERDICT_NO_COMPARABLE: "Сопоставимой модели нет",
        VERDICT_FX_UNAVAILABLE: "Курс недоступен — сравнение ограничено",
        VERDICT_CONFIG_DIFFERS: "Конфигурации отличаются",
        VERDICT_THAI_SPEC_HIGHER: "Таиланд: выше спецификация при более высокой цене",
    }.get(verdict, verdict)
