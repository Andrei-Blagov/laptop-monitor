from __future__ import annotations

"""Hard eligibility for user-facing laptop selection. Collection is not filtered."""

import re
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import config
from deal_ranking import canonical_gpu

NOT_DISCOVERED = "NOT_DISCOVERED"
GPU_NOT_DETECTED = "GPU_NOT_DETECTED"
NOT_AVAILABLE = "NOT_AVAILABLE"
SPECS_MISSING = "SPECS_MISSING"
PRICE_OVER_CAP = "PRICE_OVER_CAP"
HARD_FILTER_REJECTED = "HARD_FILTER_REJECTED"
LOW_RANK = "LOW_RANK"
DUPLICATE_MODEL = "DUPLICATE_MODEL"

TARGET_GPUS = frozenset({"RTX 5070 Ti", "RTX 5080"})

# 2560x1440, 2560×1440, 2560*1440, 2560 х 1440 (Cyrillic). Marketing labels
# such as QHD / 2K carry no numbers and stay unknown on purpose.
_RESOLUTION_RE = re.compile(r"(\d{3,4})\s*[xX×*хХ]\s*(\d{3,4})")


@dataclass
class Eligibility:
    eligible: bool
    reason: str | None = None
    details: list[str] = field(default_factory=list)


def parse_resolution(value: Any) -> tuple[int, int] | None:
    """(horizontal, vertical) in pixels, or None when not stated as numbers."""
    if value is None:
        return None
    match = _RESOLUTION_RE.search(str(value))
    if not match:
        return None
    a, b = int(match.group(1)), int(match.group(2))
    if a <= 0 or b <= 0:
        return None
    return (max(a, b), min(a, b))


def screen_meets_minimum(value: Any) -> bool | None:
    """True / False for a stated resolution, None when unknown."""
    parsed = parse_resolution(value)
    if parsed is None:
        return None
    width, height = parsed
    return (
        width > int(config.MIN_SCREEN_WIDTH_EXCLUSIVE)
        and height >= int(config.MIN_SCREEN_HEIGHT)
        and width * height >= int(config.MIN_SCREEN_PIXELS)
    )


def ram_meets_minimum(ram_gb: Any) -> bool | None:
    """Installed RAM only. None when unknown."""
    if ram_gb is None or isinstance(ram_gb, bool):
        return None
    try:
        value = int(ram_gb)
    except (TypeError, ValueError, OverflowError):
        return None
    if value <= 0:
        return None
    return value >= int(config.MIN_INSTALLED_RAM_GB)


def ssd_meets_minimum(ssd_gb: Any) -> bool | None:
    """Installed SSD capacity, accepting catalog representations of 1 TB."""
    if ssd_gb is None or isinstance(ssd_gb, bool):
        return None
    try:
        value = int(ssd_gb)
    except (TypeError, ValueError, OverflowError):
        return None
    if value <= 0:
        return None
    return value >= int(config.MIN_SSD_GB)


def evaluate_hardware(
    *,
    gpu: Any,
    ram_gb: Any,
    screen_resolution: Any,
    ssd_gb: Any = None,
    screen_inch: Any = None,
    gpu_vram_gb: Any = None,
) -> Eligibility:
    """Shared hardware policy. Unknown values are not treated as a pass."""
    from product_policy import exact_target_gpu
    if exact_target_gpu(gpu) is None:
        return Eligibility(False, GPU_NOT_DETECTED, ["gpu_unknown" if not gpu else f"gpu:{gpu}"])

    # An exact Laptop GPU has a published standard memory configuration.
    # Missing shop VRAM is allowed; contradictory or malformed stated VRAM is not.
    from product_policy import LAPTOP_VRAM_GB
    if gpu_vram_gb is not None:
        try:
            memory_matches = not isinstance(gpu_vram_gb, bool) and float(gpu_vram_gb) == LAPTOP_VRAM_GB[canonical_gpu(str(gpu))]
        except (TypeError, ValueError, OverflowError, KeyError):
            memory_matches = False
        if not memory_matches:
            return Eligibility(False, HARD_FILTER_REJECTED, ["gpu_vram_conflict"])
    ram_ok = ram_meets_minimum(ram_gb)
    screen_ok = screen_meets_minimum(screen_resolution)
    ssd_ok = ssd_meets_minimum(ssd_gb)
    try:
        diagonal = float(screen_inch) if not isinstance(screen_inch, bool) else 0
        size_ok = diagonal >= config.MIN_SCREEN_INCH if math.isfinite(diagonal) and diagonal > 0 else None
    except (TypeError, ValueError, OverflowError):
        size_ok = None
    missing = []
    if size_ok is None:
        missing.append("screen_size_unknown")
    if ram_ok is None:
        missing.append("ram_unknown")
    if screen_ok is None:
        missing.append("resolution_unknown")
    if ssd_ok is None:
        missing.append("ssd_unknown")
    rejected = []
    if size_ok is False:
        rejected.append(f"screen_size_{diagonal:g}")
    if ram_ok is False:
        rejected.append(f"ram_{int(ram_gb)}gb")
    if screen_ok is False:
        w, h = parse_resolution(screen_resolution) or (0, 0)
        rejected.append(f"resolution_{w}x{h}")
    if ssd_ok is False:
        rejected.append(f"ssd_{int(ssd_gb)}gb")
    details = rejected + missing
    if rejected:
        return Eligibility(False, HARD_FILTER_REJECTED, details)
    if missing:
        return Eligibility(False, SPECS_MISSING, details)
    return Eligibility(True, None, [])


def evaluate_purchase(*, price_rub: Any, **hardware: Any) -> Eligibility:
    """One price and hardware gate for both markets (after FX conversion)."""
    if isinstance(price_rub, bool) or not config.is_price_in_tracking_scope(price_rub):
        return Eligibility(False, PRICE_OVER_CAP, ["price_missing_or_out_of_scope"])
    return evaluate_hardware(**hardware)


def deal_is_eligible(deal: Any) -> bool:
    """Defensive gate for BUY / recommendations, including restored job data."""
    return evaluate_purchase(
        price_rub=getattr(deal, "price", None),
        gpu=getattr(deal, "gpu", None),
        ram_gb=getattr(deal, "ram_gb", None),
        ssd_gb=getattr(deal, "ssd_gb", None),
        screen_resolution=getattr(deal, "screen_resolution", None),
        screen_inch=getattr(deal, "screen_inch", None),
        gpu_vram_gb=getattr(deal, "gpu_vram_gb", None),
    ).eligible


def product_is_eligible(product: Mapping[str, Any], identity: Any = None) -> bool:
    """Individual Russian offer: cache evidence first, explicit title fallback.

    No specs are copied from the personal TOP-3, search query, or another SKU.
    """
    from stores.common import extract_specs_from_name

    if not product.get("available"):
        return False
    inferred = extract_specs_from_name(str(product.get("name") or ""))

    def value(key: str) -> Any:
        known = getattr(identity, key, None)
        return known if known is not None else inferred.get(key)

    return evaluate_purchase(
        price_rub=product.get("price"),
        gpu=value("gpu"),
        gpu_vram_gb=value("gpu_vram_gb"),
        ram_gb=value("ram_gb"),
        ssd_gb=value("ssd_gb"),
        screen_resolution=value("screen_resolution"),
        screen_inch=getattr(identity, "screen_size_inch", None) or inferred.get("screen_inch"),
    ).eligible


def row_hardware_eligible(row: Mapping[str, Any], *, price_key: str = "price_rub") -> bool:
    """Same gate for serialized recommendations and cached Thailand results."""
    return evaluate_purchase(
        price_rub=row.get(price_key),
        gpu=row.get("gpu"),
        gpu_vram_gb=row.get("gpu_vram_gb"),
        ram_gb=row.get("ram_gb"),
        ssd_gb=row.get("ssd_gb"),
        screen_resolution=row.get("screen_resolution"),
        screen_inch=row.get("screen_inch") or row.get("screen_size_inch"),
    ).eligible


REASON_TEXT_RU = {
    NOT_DISCOVERED: "не найдена в магазинах",
    GPU_NOT_DETECTED: "видеокарта не подтверждена",
    NOT_AVAILABLE: "нет в наличии",
    SPECS_MISSING: "характеристики не подтверждены",
    PRICE_OVER_CAP: "цена выше лимита",
    HARD_FILTER_REJECTED: "не подходит по RAM, SSD или экрану",
    LOW_RANK: "подходит, но ниже ТОП",
    DUPLICATE_MODEL: "та же модель уже есть в рейтинге",
}
