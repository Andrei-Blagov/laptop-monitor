"""Typed, in-memory adapters for B/C snapshots. No I/O or implicit clock."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Any, Mapping
from zoneinfo import ZoneInfo
import product_policy

CORE_FIELDS = ('cpu', 'gpu', 'ram_gb', 'ssd_gb', 'screen_inch', 'screen_resolution')
HARD_FIELDS = ('gpu', 'ram_gb', 'ssd_gb', 'screen_resolution')
HARD_EXTRAS = ('gpu_vram_gb', 'gpu_tgp_w', 'screen_refresh_hz', 'panel_type')
METHODS = frozenset({'title', 'explicit_title', 'specs_cache', 'structured_api',
                     'structured_product_page', 'product_detail', 'store_listing'})


class ContractError(ValueError):
    pass


def money(value: Any, *, positive: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or (positive and result <= 0) or abs(result.as_tuple().exponent) > 1000 or result.adjusted() > 1000:
        return None
    return result


def decimal_product(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = max(50, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 10)
        return left * right


def decimal_sum(values: Any) -> Decimal:
    values = tuple(values)
    if not values:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = max(50, max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values) + len(values) + 10)
        return sum(values, Decimal(0))


def moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if result.utcoffset() != timedelta(0):
        return None
    return result.astimezone(timezone.utc)


def age_valid(value: datetime | None, now: datetime, hours: Decimal | int | None) -> bool:
    if value is None or moment(now) is None or value > now:
        return False
    return hours is None or now - value <= timedelta(seconds=float(Decimal(str(hours)) * 3600))


def identifier(value: Any) -> str:
    return re.sub(r'\s+', '', str(value or '')).upper()


def stable_id(prefix: str, values: Any) -> str:
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':'))
    return prefix + '-' + hashlib.sha256(payload.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Evidence:
    source: str
    external_id: str
    captured_at: datetime | None
    method: str
    capture_state: str = 'known'

    def supports(self, offer: Offer, now: datetime) -> bool:
        return bool(self.source == offer.source and self.external_id == offer.external_id
                    and self.method in METHODS and self.capture_state != 'unknown'
                    and age_valid(self.captured_at, now, None))

    def to_dict(self) -> dict:
        return {'source': self.source, 'external_id': self.external_id,
                'captured_at': self.captured_at.isoformat() if self.captured_at else None,
                'method': self.method, 'capture_state': self.capture_state}


@dataclass(frozen=True)
class SpecField:
    value: Any = None
    evidence: Evidence | None = None

    def confirmed(self, offer: Offer, now: datetime) -> bool:
        return self.value is not None and self.evidence is not None and self.evidence.supports(offer, now)

    def to_dict(self) -> dict:
        value = self.value
        if isinstance(value, Decimal):
            value = str(value) if value.is_finite() else None
        elif isinstance(value, (int, float)) and money(value) is None:
            value = None
        return {'value': value, 'evidence': self.evidence.to_dict() if self.evidence else None}


@dataclass(frozen=True)
class Offer:
    market: str
    offer_id: str
    source: str
    external_id: str
    price: Decimal | None
    price_checked_at: datetime | None
    availability_state: str
    availability_checked_at: datetime | None
    availability_confirmed: bool
    availability_evidence: Evidence | None
    specs: Mapping[str, SpecField]
    identities: Mapping[str, SpecField] = field(default_factory=dict)
    extras: Mapping[str, SpecField] = field(default_factory=dict)
    identifiers: tuple[str, ...] = ()
    model_id: str | None = None
    variant_id: str | None = None
    name: str = ''
    url: str | None = None
    seller_trust: str = 'unknown'
    price_verified_for_variant: bool = False
    nominal_ssd_gb: int | None = None
    price_history: Mapping[str, Any] = field(default_factory=dict)

    def value(self, key: str, now: datetime) -> Any:
        entry = self.specs.get(key, SpecField())
        return entry.value if entry.confirmed(self, now) else None

    def identity(self, key: str, now: datetime) -> str | None:
        entry = self.identities.get(key, SpecField())
        return str(entry.value).strip().upper() if entry.confirmed(self, now) else None

    def to_dict(self) -> dict:
        return {'market': self.market, 'offer_id': self.offer_id, 'source': self.source,
                'external_id': self.external_id, 'model_id': self.model_id, 'variant_id': self.variant_id,
                'price': str(self.price) if self.price is not None else None,
                'price_checked_at': self.price_checked_at.isoformat() if self.price_checked_at else None,
                'availability_state': self.availability_state,
                'availability_checked_at': self.availability_checked_at.isoformat() if self.availability_checked_at else None,
                'availability_confirmed': self.availability_confirmed,
                'availability_evidence': self.availability_evidence.to_dict() if self.availability_evidence else None,
                'seller_trust': self.seller_trust, 'price_verified_for_variant': self.price_verified_for_variant,
                'identifiers': list(self.identifiers), 'specs': {k: v.to_dict() for k, v in self.specs.items()},
                'known_extras': {k: v.to_dict() for k, v in self.extras.items()},
                'identity_evidence': {k: v.to_dict() for k, v in self.identities.items()},
                'name': self.name, 'url': self.url, 'price_history': dict(self.price_history)}


@dataclass(frozen=True)
class SourceCoverage:
    source: str
    state: str
    checked_at: datetime | None
    enabled: bool = True
    incomplete_work: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {'source': self.source, 'state': self.state, 'enabled': self.enabled,
                'checked_at': self.checked_at.isoformat() if self.checked_at else None,
                'not_completed_keys': list(self.incomplete_work)}


@dataclass(frozen=True)
class MarketSnapshot:
    market: str
    snapshot_id: str
    instance_id: str
    offers: tuple[Offer, ...]
    coverage: tuple[SourceCoverage, ...]
    created_at: datetime | None
    discovery_checked_at: datetime | None = None
    discovery_scope_hash: str | None = None
    top_model_ids: tuple[str, ...] = ()
    personal_profiles: tuple[Mapping[str, Any], ...] = ()

    @property
    def coverage_complete(self) -> bool:
        active = [s for s in self.coverage if s.enabled]
        return bool(active) and all(s.state == 'ok' and not s.incomplete_work for s in active)

    def source(self, name: str) -> SourceCoverage | None:
        return next((s for s in self.coverage if s.source == name), None)


@dataclass(frozen=True)
class FxObservation:
    fx_id: str
    rub_per_thb: Decimal | None
    fetched_at: datetime | None
    published_date: date | None
    source: str = 'CBR'
    currency: str = 'THB'
    nominal: int = 1
    error: str | None = None

    def usable(self, now: datetime, policy: ComparisonPolicy) -> bool:
        if not age_valid(self.fetched_at, now, policy.fx_hours) or self.error:
            return False
        if self.source != 'CBR' or self.currency != 'THB' or not isinstance(self.nominal, int) or isinstance(self.nominal, bool) or self.nominal <= 0:
            return False
        if money(self.rub_per_thb, positive=True) is None or self.published_date is None:
            return False
        days = (now.astimezone(ZoneInfo('Europe/Moscow')).date() - self.published_date).days
        return 0 <= days <= policy.fx_date_days


@dataclass(frozen=True)
class ComparisonPolicy:
    max_price_rub: Decimal = Decimal(product_policy.MAX_PRICE_RUB)
    min_ram: int = product_policy.MIN_RAM_GB
    min_ssd: int = product_policy.MIN_SSD_GB
    quote_hours: int = 6
    ru_quote_hours: int = 3
    catalog_hours: int = 26
    fx_hours: int = 36
    fx_date_days: int = 4
    top_limit: int = 5
    unmatched_min_score: Decimal = Decimal('75')
    gpu_targets: tuple[tuple[str, Decimal], ...] = tuple((gpu, Decimal(value)) for gpu, value in product_policy.GPU_TARGETS)

    def __post_init__(self) -> None:
        budget, threshold = money(self.max_price_rub, positive=True), money(self.unmatched_min_score)
        if budget is None or threshold is None or not 0 <= threshold <= 100:
            raise ContractError('invalid budget/value threshold')
        integer_limits = (self.min_ram, self.min_ssd, self.quote_hours, self.ru_quote_hours,
                          self.catalog_hours, self.fx_hours, self.fx_date_days, self.top_limit)
        if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in integer_limits):
            raise ContractError('invalid integer policy limit')
        targets = tuple((str(key), money(value, positive=True)) for key, value in self.gpu_targets)
        if len(dict(targets)) != len(targets) or any(value is None for _, value in targets):
            raise ContractError('invalid GPU targets')
        object.__setattr__(self, 'max_price_rub', budget)
        object.__setattr__(self, 'unmatched_min_score', threshold)
        object.__setattr__(self, 'gpu_targets', targets)

    def target(self, gpu: str) -> Decimal | None:
        return dict(self.gpu_targets).get(gpu)

    @property
    def policy_id(self) -> str:
        from dataclasses import asdict
        return stable_id('comparison-policy', asdict(self))


def _evidence(raw: Any) -> Evidence | None:
    if not isinstance(raw, Mapping):
        return None
    return Evidence(str(raw.get('source') or ''), str(raw.get('external_id') or ''),
                    moment(raw.get('captured_at')), str(raw.get('method') or ''),
                    str(raw.get('capture_state') or 'known'))


def _field(raw: Any) -> SpecField:
    if not isinstance(raw, Mapping):
        return SpecField(raw)
    value = raw.get('value')
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal, type(None))):
        value = None
    return SpecField(value, _evidence(raw.get('evidence')))


def _required(raw: Mapping, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError('missing ' + key)
    return value


def _snapshot(raw: Mapping[str, Any], market: str, identity_evidence: Mapping[str, Mapping] | None = None) -> MarketSnapshot:
    if raw.get('schema_version') != 1 or isinstance(raw.get('schema_version'), bool):
        raise ContractError('unsupported snapshot schema')
    snapshot_id, instance = _required(raw, 'snapshot_id'), _required(raw, 'instance_id')
    coverage = []
    for s in raw.get('source_coverage', ()):
        coverage.append(SourceCoverage(str(s.get('source') or s.get('store') or ''),
            str(s.get('state') or s.get('status') or 'unknown'),
            moment(s.get('latest_attempt_at') or s.get('checked_at')),
            s.get('enabled', True) is True and not bool(s.get('policy_disabled')),
            tuple(str(v) for v in s.get('not_completed', s.get('not_completed_keys', ())))))
    offers = []
    for r in raw.get('offers', ()):
        oid = _required(r, 'offer_id')
        source = str(r.get('source') or r.get('store') or '')
        eid = _required(r, 'external_id')
        if not source:
            raise ContractError('missing offer source')
        specs = dict(r.get('specs') or {})
        if 'screen_inch' not in specs and 'screen_size_inch' in specs:
            specs['screen_inch'] = specs.pop('screen_size_inch')
        identities = dict(r.get('identity_evidence') or {})
        identities.update((identity_evidence or {}).get(oid, {}))
        name = str(r.get('product_name') or r.get('name') or '')
        if 'brand' not in identities:
            # An explicit own-listing brand is evidence, never a fuzzy family guess.
            hit = re.search(r'\b(ASUS|MSI|LENOVO|ACER|HP|DELL|GIGABYTE|AORUS|RAZER|SAMSUNG|HUAWEI|MACHENIKE|MAIBENBEN)\b', name, re.I)
            if hit:
                identities['brand'] = {'value': hit.group(1), 'evidence': {'source': source,
                    'external_id': eid, 'captured_at': r.get('observed_at'), 'method': 'title'}}
        availability = r.get('availability_evidence') or {}
        confirmed = availability.get('available') is True if market == 'RU' else r.get('availability_confirmed') is True
        ids = list(r.get('sku_candidates') or []) + list(r.get('mpn_candidates') or [])
        for key in ('sku', 'manufacturer_part_number'):
            if r.get(key):
                ids.append(r[key])
        ids.extend(r.get('strong_identifiers') or ())
        if any(not isinstance(v, str) for v in ids):
            raise ContractError('manufacturer identifiers must be strings')
        offers.append(Offer(market, oid, source, eid,
            money(r.get('public_price_rub') if market == 'RU' else r.get('price_thb'), positive=True),
            moment(r.get('price_checked_at')), str(r.get('stock_state') if market == 'RU' else r.get('availability_state') or 'unknown'),
            moment(r.get('availability_checked_at')), confirmed, _evidence(availability),
            MappingProxyType({k: _field(specs.get(k)) for k in CORE_FIELDS}),
            MappingProxyType({k: _field(v) for k, v in identities.items()}),
            MappingProxyType({k: _field(v) for k, v in (r.get('known_extras') or {}).items()}),
            tuple(sorted({identifier(v) for v in ids if identifier(v)})),
            r.get('model_id'), r.get('variant_id'), name, r.get('source_url') or r.get('url'),
            'direct_retailer' if market == 'RU' else str(r.get('seller_trust') or 'unknown'),
            market == 'RU' or r.get('price_verified_for_variant') is True,
            r.get('nominal_ssd_gb'), MappingProxyType(dict(r.get('price_history') or {}))))
    if len({s.source for s in coverage}) != len(coverage) or any(not s.source for s in coverage):
        raise ContractError('duplicate/missing coverage source')
    if len({o.offer_id for o in offers}) != len(offers):
        raise ContractError('duplicate offer ID')
    profiles = tuple(MappingProxyType({'key': p.get('key'), 'match_kind': p.get('match_kind'),
                                     'model_ids': tuple(p.get('model_ids') or ())}) for p in raw.get('personal_profiles', ()))
    return MarketSnapshot(market, snapshot_id, instance, tuple(offers), tuple(coverage),
        moment(raw.get('created_at') or raw.get('finished_at')), moment(raw.get('discovery_checked_at')),
        raw.get('discovery_scope_hash'), tuple(raw.get('top_model_ids') or ()), profiles)


def russian_snapshot(raw: Mapping[str, Any], *, identity_evidence: Mapping[str, Mapping] | None = None) -> MarketSnapshot:
    return _snapshot(raw, 'RU', identity_evidence)


def thai_snapshot(raw: Mapping[str, Any]) -> MarketSnapshot:
    return _snapshot(raw, 'TH')


def fx_observation(raw: Mapping[str, Any]) -> FxObservation:
    try:
        published = date.fromisoformat(str(raw.get('published_date')))
    except ValueError:
        published = None
    return FxObservation(_required(raw, 'fx_id'), money(raw.get('rub_per_thb'), positive=True),
        moment(raw.get('fetched_at')), published, str(raw.get('source') or ''),
        str(raw.get('currency') or ''), raw.get('nominal', 1), raw.get('error'))
