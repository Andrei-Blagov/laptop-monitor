"""Pure full-market matrix, Decimal price gaps and independent presentation views."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any, Mapping

from cross_market_matching import compare_configuration
from market_contracts import (ComparisonPolicy, ContractError, FxObservation, MarketSnapshot,
                              age_valid, moment, money, stable_id, decimal_sum)
from thai_market_ranking import assess_quote, build_thai_rankings


@dataclass(frozen=True)
class Expense:
    label: str
    amount_rub: Decimal
    confirmed: bool
    evidence_ref: str | None = None
    offer_id: str | None = None

    def __post_init__(self) -> None:
        amount = money(self.amount_rub)
        if amount is None or amount < 0 or not self.label:
            raise ContractError('invalid expense')
        object.__setattr__(self, 'amount_rub', amount)


def price_gap(ru: Decimal, th: Decimal, confirmed_costs: Decimal = Decimal(0)) -> dict:
    ru, th, costs = money(ru, positive=True), money(th, positive=True), money(confirmed_costs)
    if ru is None or th is None or costs is None or costs < 0:
        raise ContractError('invalid comparable price/cost')
    with localcontext() as ctx:
        ctx.prec = max(50, len(ru.as_tuple().digits) + len(th.as_tuple().digits) + len(costs.as_tuple().digits) + 20)
        total = th + costs
        saving = ru - total
        pct = saving * 100 / ru
        band = 'comparable' if abs(pct) < 5 else 'slight' if abs(pct) < 10 else 'clear'
    return {'ru_price_rub': str(ru), 'thai_base_rub': str(th), 'known_costs_rub': str(costs),
            'thai_known_total_rub': str(total), 'saving_rub': str(saving), 'saving_pct': str(pct),
            'band': band, 'cheaper_market': 'TH' if saving > 0 else 'RU' if saving < 0 else None,
            'cost_scope': 'known_costs_only'}


def _coverage(snapshot: MarketSnapshot | None, now: datetime, hours: int) -> bool:
    return snapshot is not None and snapshot.coverage_complete and all(
        not s.enabled or age_valid(s.checked_at, now, hours) for s in snapshot.coverage)


def compare_markets(thai: MarketSnapshot, russian: MarketSnapshot | None,
                    fx: FxObservation | None, *, now: datetime,
                    policy: ComparisonPolicy | None = None, expenses: tuple[Expense, ...] = (),
                    unknown_costs: tuple[str, ...] = ('delivery', 'customs', 'travel', 'exchange_spread')) -> dict[str, Any]:
    policy = policy or ComparisonPolicy()
    if moment(now) is None or thai.market != 'TH':
        raise ContractError('explicit UTC now and TH snapshot required')
    if russian is not None and (russian.market != 'RU' or russian.instance_id != thai.instance_id):
        raise ContractError('incompatible market/instance')
    rankings = build_thai_rankings(thai, fx, now=now, policy=policy)
    ru_current = russian is not None and age_valid(russian.created_at, now, policy.ru_quote_hours)
    ru = russian if ru_current else None
    ru_complete = _coverage(ru, now, policy.ru_quote_hours)
    th_complete = _coverage(thai, now, policy.catalog_hours) and rankings['discovery_current']
    variants = {oid: row['variant_id'] for row in rankings['eligible_models'] for oid in row['member_offer_ids']}
    pairs, by_ru, by_th = [], {}, {}
    for th_offer in sorted(thai.offers, key=lambda o: o.offer_id):
        variant_id = variants.get(th_offer.offer_id) or th_offer.variant_id or stable_id('unranked-th', th_offer.offer_id)
        th_quote = assess_quote(th_offer, thai, fx, now=now, policy=policy)
        scoped = [c for c in expenses if c.offer_id is None or c.offer_id == th_offer.offer_id]
        verified = [c for c in scoped if c.confirmed is True and c.evidence_ref]
        unverified_costs = [c.label for c in scoped if c not in verified]
        costs = decimal_sum(c.amount_rub for c in verified)
        for ru_offer in sorted(ru.offers if ru else (), key=lambda o: o.offer_id):
            proof = compare_configuration(ru_offer, th_offer, now=now)
            rq = assess_quote(ru_offer, ru, fx, now=now, policy=policy)
            status = proof.status
            quote_reasons = list(rq.reasons + th_quote.reasons)
            if status == 'confirmed' and quote_reasons:
                if 'not_in_stock' in quote_reasons:
                    status = 'oos'
                elif any('stale' in r for r in quote_reasons):
                    status = 'stale'
                else:
                    status = 'incomplete'
            pair_id = stable_id('pair', [ru_offer.offer_id, th_offer.offer_id])
            members = [dict(p) for p in ru.personal_profiles if ru_offer.model_id in p['model_ids']]
            pair = {**proof.to_dict(), 'pair_id': pair_id, 'ru_offer_id': ru_offer.offer_id,
                'th_offer_id': th_offer.offer_id, 'th_variant_id': variant_id,
                'comparison_status': status, 'quote_reasons': quote_reasons,
                'ru_membership': {'in_full_list': True, 'in_general_top': ru_offer.model_id in ru.top_model_ids,
                                  'in_personal_top3': bool(members),
                                  'personal_profiles': [{'key': p['key'], 'match_kind': p['match_kind']} for p in members]},
                'field_provenance': {'RU': {k: v.to_dict() for k, v in ru_offer.specs.items()},
                                     'TH': {k: v.to_dict() for k, v in th_offer.specs.items()}},
                'ru_observation': ru_offer.to_dict(), 'th_observation': th_offer.to_dict(),
                'unknown_costs': sorted((set(unknown_costs) - {c.label for c in verified}) | set(unverified_costs)),
                'confirmed_expenses': [{'label': c.label, 'amount_rub': str(c.amount_rub), 'evidence_ref': c.evidence_ref} for c in verified],
                'saving_rub': None, 'saving_pct': None, 'price_gap': None,
                'coverage_complete': ru_complete and th_complete}
            if status == 'confirmed':
                gap = price_gap(rq.price_rub, th_quote.price_rub, costs)
                pair.update(saving_rub=gap['saving_rub'], saving_pct=gap['saving_pct'], price_gap=gap,
                            known_total_within_budget=Decimal(gap['thai_known_total_rub']) <= policy.max_price_rub)
            pairs.append(pair)
            by_ru.setdefault(ru_offer.offer_id, []).append(pair_id)
            by_th.setdefault(variant_id, []).append(pair_id)
    best_pairs, unmatched = {}, []
    for row in rankings['eligible_models']:
        pool = [p for p in pairs if p['th_variant_id'] == row['variant_id']]
        confirmed = [p for p in pool if p['comparison_status'] == 'confirmed']
        if confirmed:
            # Use the cheapest comparable RU price, not a more expensive listing that inflates savings.
            selected = min(confirmed, key=lambda p: (Decimal(p['price_gap']['ru_price_rub']),
                Decimal(p['price_gap']['thai_known_total_rub']), p['pair_id']))
            best_pairs[row['variant_id']] = selected['pair_id']
            continue
        related = [p for p in pool if p['match_class'] != 'ALTERNATIVE' and (p['identifier_evidence']['shared_full_ids'] or p['identifier_evidence']['family'])]
        if ru is None:
            reason, details = 'limited_coverage', ['ru_snapshot_unavailable']
        elif any(p['comparison_status'] == 'conflict' for p in related):
            reason, details = 'conflict', sorted({k for p in related for k in p['conflicting_fields']})
        elif related:
            reason, details = 'incomplete', sorted({k for p in related for k in p['missing_fields']} | {r for p in related for r in p['quote_reasons']})
        elif not ru_complete:
            reason, details = 'limited_coverage', ['ru_sources_incomplete']
        else:
            reason, details = 'not_observed', []
        target = policy.target(row['gpu'])
        value = Decimal(row['score']) >= policy.unmatched_min_score and target is not None and Decimal(row['price_rub']) <= target
        unmatched.append({**row, 'status': 'NO_CONFIRMED_RU_MATCH', 'reason': reason, 'details': details,
            'unmatched_value_candidate': value, 'value_label': 'value_candidate' if value else 'eligible_model',
            'saving_rub': None, 'saving_pct': None, 'buy_signal': None,
            'related_ru_offer_ids': sorted({p['ru_offer_id'] for p in related})})
    cid = stable_id('comparison', [thai.snapshot_id, ru.snapshot_id if ru else None, fx.fx_id if fx else None,
        now.isoformat(), policy.policy_id,
        [(e.label, str(e.amount_rub), e.confirmed, e.evidence_ref, e.offer_id) for e in expenses], list(unknown_costs)])
    return {**rankings, 'schema_version': 1, 'comparison_id': cid, 'instance_id': thai.instance_id,
        'generated_at': now.isoformat(), 'ru_snapshot_id': ru.snapshot_id if ru else None,
        'input_ru_snapshot_id': russian.snapshot_id if russian else None, 'th_snapshot_id': thai.snapshot_id,
        'fx_id': fx.fx_id if fx else None, 'pairs': pairs, 'by_ru_offer_id': by_ru, 'by_th_variant_id': by_th,
        'best_pairs_by_th_variant_id': best_pairs, 'unmatched_th': unmatched,
        'unmatched_value_top': [r for r in unmatched if r['unmatched_value_candidate']][:3],
        'source_coverage': {'RU': [s.to_dict() for s in ru.coverage] if ru else [],
                            'TH': [s.to_dict() for s in thai.coverage]},
        'data_freshness': {'ru_current': ru_current, 'discovery_current': rankings['discovery_current'],
                           'fx_usable': bool(fx and fx.usable(now, policy))},
        'market_coverage_complete': ru_complete and th_complete,
        'limitations': ([] if ru_current else ['ru_snapshot_unavailable']) +
                      ([] if ru_complete and th_complete else ['inspected_coverage_incomplete'])}


def comparison_view(result: Mapping[str, Any], view: str) -> tuple[Mapping[str, Any], ...]:
    """Select existing rows only; no second calculation for TOP/personal/BUY views."""
    pairs = result['pairs']
    if view == 'all':
        return tuple(pairs)
    if view == 'general_top':
        return tuple(p for p in pairs if p['ru_membership']['in_general_top'])
    if view == 'personal_top3':
        return tuple(p for p in pairs if p['ru_membership']['in_personal_top3'])
    raise ContractError('unknown comparison view')
