"""Independent full Thai catalogue ranking. Pure, deterministic and Decimal-safe."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

import config
from cross_market_matching import compare_configuration, normalized_field, full_identifiers, normalized_extra
from market_contracts import (ComparisonPolicy, FxObservation, HARD_FIELDS, MarketSnapshot,
                              Offer, age_valid, moment, stable_id, decimal_product)
from thailand.scoring import international_value_score


@dataclass(frozen=True)
class QuoteAssessment:
    eligible: bool
    price_rub: Decimal | None
    reasons: tuple[str, ...]


def assess_quote(offer: Offer, snapshot: MarketSnapshot, fx: FxObservation | None,
                 *, now: datetime, policy: ComparisonPolicy) -> QuoteAssessment:
    reasons = []
    if offer.price is None or offer.price <= 0:
        reasons.append('price_unconfirmed')
    rub = offer.price if offer.market == 'RU' else (decimal_product(offer.price, fx.rub_per_thb)
        if offer.price is not None and fx and fx.usable(now, policy) else None)
    if offer.market == 'TH' and (fx is None or not fx.usable(now, policy)):
        reasons.append('fx_unusable')
    if rub is not None and rub > policy.max_price_rub:
        reasons.append('price_above_cap')
    ttl = policy.ru_quote_hours if offer.market == 'RU' else policy.quote_hours
    if not age_valid(offer.price_checked_at, now, ttl):
        reasons.append('price_stale_or_unknown')
    if not age_valid(offer.availability_checked_at, now, ttl):
        reasons.append('availability_stale_or_unknown')
    if offer.availability_state not in ('in_stock', 'online_available', 'store_pickup_only'):
        reasons.append('not_in_stock')
    if not (offer.availability_confirmed and offer.availability_evidence
            and offer.availability_evidence.supports(offer, now)
            and age_valid(offer.availability_evidence.captured_at, now, ttl)
            and offer.availability_evidence.captured_at == offer.availability_checked_at):
        reasons.append('availability_unconfirmed')
    if not offer.price_verified_for_variant:
        reasons.append('variant_price_unconfirmed')
    if offer.seller_trust not in ('direct_retailer', 'A', 'B'):
        reasons.append('seller_unconfirmed')
    source = snapshot.source(offer.source)
    if source is None or not source.enabled:
        reasons.append('source_unconfirmed_or_disabled')
    elif offer.market == 'RU' and (source.state != 'ok' or not age_valid(source.checked_at, now, policy.ru_quote_hours)):
        reasons.append('ru_source_not_current')
    values = {key: normalized_field(offer, key, now) for key in HARD_FIELDS}
    for key, value in values.items():
        if value is None:
            reasons.append('spec_unconfirmed:' + key)
    if values['gpu'] not in ('RTX 5070 Ti', 'RTX 5080'):
        reasons.append('gpu_rejected')
    from product_policy import LAPTOP_VRAM_GB
    vram = offer.extras.get('gpu_vram_gb')
    if vram is not None and vram.value is not None:
        from market_contracts import money
        if money(vram.value, positive=True) != LAPTOP_VRAM_GB.get(values['gpu']):
            reasons.append('gpu_vram_conflict')
    if values['ram_gb'] is not None and values['ram_gb'] < policy.min_ram:
        reasons.append('ram_rejected')
    if values['ssd_gb'] is not None and values['ssd_gb'] < policy.min_ssd:
        reasons.append('ssd_rejected')
    if values['screen_resolution']:
        width, height = map(int, values['screen_resolution'].split('x'))
        if width <= 1920 or height < 1440 or width * height < 2560 * 1440:
            reasons.append('screen_rejected')
    size = normalized_field(offer, 'screen_inch', now)
    if size is None:
        reasons.append('spec_unconfirmed:screen_inch')
    elif size < Decimal(str(config.MIN_SCREEN_INCH)):
        reasons.append('screen_size_rejected')
    return QuoteAssessment(not reasons, rub, tuple(reasons))


def value_score(offer: Offer, price: Decimal, *, now: datetime, policy: ComparisonPolicy) -> tuple[Decimal, dict, int]:
    values = {key: normalized_field(offer, key, now) for key in ('gpu', 'cpu', 'ram_gb', 'ssd_gb', 'screen_inch', 'screen_resolution')}
    base = international_value_score(price_rub=None, gpu=values['gpu'], cpu=values['cpu'],
        ram_gb=values['ram_gb'], ssd_gb=values['ssd_gb'],
        screen_inch=float(values['screen_inch']) if values['screen_inch'] is not None else None,
        screen_resolution=values['screen_resolution'])
    breakdown = {k: Decimal(str(v)) for k, v in base.breakdown.items()}
    target = policy.target(values['gpu'])
    with localcontext() as ctx:
        ctx.prec = 40
        at, maximum = Decimal(str(config.SCORE_PRICE_AT_TARGET)), Decimal(str(config.INTL_SCORE_PRICE_MAX))
        if target is None:
            pts = Decimal(0)
        elif price <= target:
            discount = (target - price) / target
            pts = at + (maximum - at) * min(Decimal(1), discount / Decimal(str(config.SCORE_PRICE_UNDER_SPAN)))
        else:
            over = (price - target) / target
            pts = at * max(Decimal(0), 1 - over / Decimal(str(config.SCORE_PRICE_OVER_ZERO_AT)))
        breakdown['price'] = min(maximum, max(Decimal(0), pts)).quantize(Decimal('.01'))
        raw = sum(breakdown.values())
        score = (raw * 100 / Decimal(str(config.INTL_SCORE_RAW_MAX))).quantize(Decimal('.01'))
    confidence = 70 + (10 if values['cpu'] is not None else 0) + 8 + 7 + (5 if values['screen_inch'] is not None else 0)
    return score, {k: str(v) for k, v in breakdown.items()}, confidence


def build_thai_rankings(snapshot: MarketSnapshot, fx: FxObservation | None, *, now: datetime,
                        policy: ComparisonPolicy | None = None) -> dict[str, Any]:
    policy = policy or ComparisonPolicy()
    if snapshot.market != 'TH' or moment(now) is None:
        raise ValueError('TH snapshot and explicit UTC now required')
    assessments, eligible, excluded = {}, [], []
    for offer in sorted(snapshot.offers, key=lambda o: o.offer_id):
        a = assess_quote(offer, snapshot, fx, now=now, policy=policy)
        assessments[offer.offer_id] = a
        if a.eligible:
            eligible.append(offer)
        else:
            excluded.append({'offer_id': offer.offer_id, 'reasons': list(a.reasons)})
    groups: list[list[Offer]] = []
    for offer in eligible:
        for group in groups:
            proofs = [compare_configuration(member, offer, now=now) for member in group]
            common_ids = set.intersection(*(full_identifiers(o) for o in [*group, offer]))
            if common_ids and all(p.match_class == 'EXACT' and p.status == 'confirmed' and not p.commercial_differences and not p.unknown_extras for p in proofs):
                group.append(offer)
                break
        else:
            groups.append([offer])
    rows = []
    for group in groups:
        candidates = []
        for offer in group:
            a = assessments[offer.offer_id]
            score, breakdown, confidence = value_score(offer, a.price_rub, now=now, policy=policy)
            candidates.append((offer, a.price_rub, score, breakdown, confidence))
        candidates.sort(key=lambda x: (-x[2], -x[4], x[1], x[0].offer_id))
        best, rub, score, breakdown, confidence = candidates[0]
        common = set.intersection(*(full_identifiers(o) for o in group)) if group else set()
        anchor = group[0]
        core = {k: str(normalized_field(anchor, k, now)) for k in anchor.specs}
        proven = compare_configuration(anchor, anchor, now=now).status == 'confirmed'
        extras = {k: str(normalized_extra(v, anchor, now, k)) for k, v in anchor.extras.items()}
        # Conservative groups retain regional/configuration variants; no name-only merge.
        model_id = stable_id('thai-model', [anchor.identity('brand', now), sorted(common), core,
            extras, [] if common and proven else [o.offer_id for o in group]])
        rows.append({'model_id': model_id, 'variant_id': model_id,
            'best_offer_id': best.offer_id, 'cheapest_offer_id': min(candidates, key=lambda x: (x[1], x[0].offer_id))[0].offer_id,
            'member_offer_ids': sorted(o.offer_id for o in group),
            'offers': [o.to_dict() for o in group], 'price_rub': str(rub), 'price_thb': str(best.price),
            'gpu': normalized_field(best, 'gpu', now), 'score': str(score), 'confidence': confidence,
            'score_breakdown': breakdown, 'name': best.name, 'url': best.url,
            'pickup_only': best.availability_state == 'store_pickup_only'})
    rows.sort(key=lambda r: (-Decimal(r['score']), -r['confidence'], Decimal(r['price_rub']), r['model_id']))
    cheapest = sorted(rows, key=lambda r: (Decimal(assessments[r['cheapest_offer_id']].price_rub),
                                          -Decimal(r['score']), r['model_id']))
    return {'snapshot_id': snapshot.snapshot_id, 'policy_id': policy.policy_id,
        'all_offers': [o.to_dict() for o in snapshot.offers], 'eligible_models': rows,
        'thai_top_value': rows[:policy.top_limit], 'thai_cheapest': cheapest,
        'exclusions': excluded, 'discovery_current': age_valid(snapshot.discovery_checked_at, now, policy.catalog_hours),
        'coverage_complete': snapshot.coverage_complete}
