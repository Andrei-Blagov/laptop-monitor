"""One conservative identity/configuration proof for both market directions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from market_contracts import CORE_FIELDS, HARD_EXTRAS, Offer, SpecField, identifier, money
from product_identity import normalize_cpu, normalize_resolution


def normalized_field(offer: Offer, key: str, now: datetime) -> Any:
    value = offer.value(key, now)
    if value is None:
        return None
    if key == 'gpu':
        text = str(value).upper()
        if re.search(r'DESKTOP|НАСТОЛЬН', text):
            return None
        text = re.sub(r'\b(?:NVIDIA|GEFORCE|LAPTOP|MOBILE|GPU)\b', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if re.fullmatch(r'RTX\s*5070\s*TI', text):
            return 'RTX 5070 Ti'
        if re.fullmatch(r'RTX\s*5080', text):
            return 'RTX 5080'
        return None
    if key == 'cpu':
        text = normalize_cpu(str(value))
        return text if text and re.search(r'\b(?:\d{3,5}[A-Z]{1,4}|(?:HX|HS)\s+\d{3})\b', text) else None
    if key == 'screen_resolution':
        return normalize_resolution(str(value))
    if key == 'ssd_gb' and re.fullmatch(r'1\s*TB', str(value), re.I):
        return 1000  # explicit nominal capacity from the offer's own evidence
    number = money(value, positive=True)
    if number is None:
        return None
    if key in ('ram_gb', 'ssd_gb'):
        if number != number.to_integral_value():
            return None
        result = int(number)
        if key == 'ssd_gb' and offer.nominal_ssd_gb == 1000 and result in (1000, 1024):
            return 1000
        return result
    return number if key == 'screen_inch' else None


def full_identifiers(offer: Offer) -> set[str]:
    result = set()
    for value in offer.identifiers:
        code = identifier(value)
        # Bare platform/series codes are not one sold configuration.
        if re.fullmatch(r'(?:G\d{3}[A-Z]{1,4}|\d{2}[A-Z]{3}\d{2}H|[A-Z]\d[A-Z]{4,6})', code):
            continue
        if re.fullmatch(r'[A-Z0-9][A-Z0-9._/\-]*', code) and len(code) >= 6 and any(c.isdigit() for c in code) and any(c.isalpha() for c in code):
            result.add(code)
    return result


def normalized_extra(entry: SpecField, offer: Offer, now: datetime, key: str) -> Any:
    if not entry.confirmed(offer, now):
        return None
    if key in ('gpu_vram_gb', 'gpu_tgp_w', 'screen_refresh_hz'):
        return money(entry.value, positive=True)
    return ' '.join(str(entry.value).upper().split())


@dataclass(frozen=True)
class MatchProof:
    match_class: str | None
    status: str
    shared_identifiers: tuple[str, ...]
    family: str | None
    matching_fields: dict
    conflicting_fields: dict
    missing_fields: tuple[str, ...]
    commercial_differences: dict
    unknown_extras: tuple[str, ...]

    def to_dict(self) -> dict:
        return {'match_class': self.match_class, 'comparison_status': self.status,
                'identifier_evidence': {'shared_full_ids': list(self.shared_identifiers), 'family': self.family},
                'matching_fields': self.matching_fields, 'conflicting_fields': self.conflicting_fields,
                'missing_fields': list(self.missing_fields), 'commercial_differences': self.commercial_differences,
                'unknown_extras': list(self.unknown_extras)}


def compare_configuration(left: Offer, right: Offer, *, now: datetime) -> MatchProof:
    shared = tuple(sorted(full_identifiers(left) & full_identifiers(right)))
    lb, rb = left.identity('brand', now), right.identity('brand', now)
    lf, rf = left.identity('manufacturer_family', now), right.identity('manufacturer_family', now)
    lg, rg = left.identity('manufacturer_generation', now), right.identity('manufacturer_generation', now)
    family = lf if lf and lf == rf and lb and lb == rb else None
    matching, conflicting, commercial, missing, unknown = {}, {}, {}, [], []
    for key in CORE_FIELDS:
        a, b = normalized_field(left, key, now), normalized_field(right, key, now)
        if a is None or b is None:
            missing.append(key)
        elif a != b:
            conflicting[key] = {'RU_or_left': str(a), 'TH_or_right': str(b)}
        else:
            matching[key] = str(a)
    for key in sorted(set(left.extras) | set(right.extras)):
        le, re_ = left.extras.get(key, SpecField()), right.extras.get(key, SpecField())
        a, b = normalized_extra(le, left, now, key), normalized_extra(re_, right, now, key)
        if a is None or b is None:
            unknown.append(key)
        if (le.value is not None and a is None) or (re_.value is not None and b is None):
            if key in HARD_EXTRAS:
                missing.append(key + '_evidence')
        if a is not None and b is not None and a != b:
            (conflicting if key in HARD_EXTRAS else commercial)[key] = {'RU_or_left': str(a), 'TH_or_right': str(b)}
    if lb is None or rb is None:
        missing.append('brand')
    # Different documented generations cannot be equated by an ID collision.
    if lg and rg and lg != rg and (shared or family):
        conflicting['manufacturer_generation'] = {'RU_or_left': lg, 'TH_or_right': rg}
    if shared and lf and rf and lf != rf:
        conflicting['manufacturer_family'] = {'RU_or_left': lf, 'TH_or_right': rf}
    if family and not shared and (not lg or not rg):
        missing.append('manufacturer_generation')
    related = bool(shared or family)
    if lb and rb and lb != rb:
        level, status = 'ALTERNATIVE', 'not_comparable'
    elif related and conflicting:
        level, status = ('SAME_FAMILY' if family else None), 'conflict'
    elif related and missing:
        level, status = ('SAME_FAMILY' if family else None), 'incomplete'
    elif shared:
        level, status = 'EXACT', 'confirmed'
    elif family:
        level, status = 'EQUIVALENT', 'confirmed'
    else:
        level, status = 'ALTERNATIVE', 'not_comparable'
    return MatchProof(level, status, shared, family, matching, conflicting,
                      tuple(missing), commercial, tuple(unknown))
