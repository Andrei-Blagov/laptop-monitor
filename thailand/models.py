from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ThailandOffer:
    store: str
    external_id: str
    name: str
    url: str
    price_thb: int | None
    available: bool
    collected_at: datetime
    sku: str | None = None
    manufacturer_part_number: str | None = None
    brand: str | None = None
    model_family: str | None = None
    cpu: str | None = None
    gpu: str | None = None
    ram_gb: int | None = None
    ssd_gb: int | None = None
    screen_size_inch: float | None = None
    screen_resolution: str | None = None
    regular_price_thb: int | None = None
    availability_status: str | None = None
    warranty: str | None = None
    # Provenance / verification (Thailand hardening)
    gpu_source: str | None = None
    candidate_gpu: str | None = None
    availability_source: str | None = None
    availability_confirmed: bool = False
    price_source: str | None = None
    verification_status: str | None = None
    verification_reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["collected_at"] = self.collected_at.isoformat()
        return data


@dataclass
class StoreScanResult:
    store: str
    ok: bool
    offers: list[ThailandOffer] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float | None = None
    unverified_candidates: list[ThailandOffer] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.offers)


@dataclass
class FxRate:
    source: str
    currency: str
    nominal: int
    official_rate: float
    rub_per_thb: float
    published_date: str | None
    fetched_at: datetime
    stale: bool = False
    rate_age_hours: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "currency": self.currency,
            "nominal": self.nominal,
            "official_rate": self.official_rate,
            "rub_per_thb": self.rub_per_thb,
            "published_date": self.published_date,
            "fetched_at": self.fetched_at.isoformat(),
            "stale": self.stale,
            "rate_age_hours": self.rate_age_hours,
            "error": self.error,
        }


MatchLevel = str  # EXACT | SAME_FAMILY | EQUIVALENT | ALTERNATIVE


@dataclass
class CrossCountryMatch:
    level: MatchLevel
    russian_name: str
    thai_offer: ThailandOffer
    russian_price_rub: int | None
    thai_price_thb: int | None
    thai_price_rub: int | None
    delta_rub: int | None = None
    delta_percent: float | None = None
    differences: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class CountryComparison:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    match: CrossCountryMatch | None = None
