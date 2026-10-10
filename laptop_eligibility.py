from __future__ import annotations

"""Hard eligibility for user-facing laptop selection. Collection is not filtered."""

import re
from dataclasses import dataclass, field
from typing import Any

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
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value >= int(config.MIN_INSTALLED_RAM_GB)


def evaluate_hardware(
    *,
    gpu: Any,
    ram_gb: Any,
    screen_resolution: Any,
) -> Eligibility:
    """GPU, installed RAM, and screen. Unknown values are not treated as a pass."""
    details: list[str] = []
    if canonical_gpu(str(gpu) if gpu else None) not in TARGET_GPUS:
        return Eligibility(False, GPU_NOT_DETECTED, ["gpu_unknown" if not gpu else f"gpu:{gpu}"])

    ram_ok = ram_meets_minimum(ram_gb)
    screen_ok = screen_meets_minimum(screen_resolution)
    missing = []
    if ram_ok is None:
        missing.append("ram_unknown")
    if screen_ok is None:
        missing.append("resolution_unknown")
    rejected = []
    if ram_ok is False:
        rejected.append(f"ram_{int(ram_gb)}gb")
    if screen_ok is False:
        w, h = parse_resolution(screen_resolution) or (0, 0)
        rejected.append(f"resolution_{w}x{h}")
    details = rejected + missing
    if rejected:
        return Eligibility(False, HARD_FILTER_REJECTED, details)
    if missing:
        return Eligibility(False, SPECS_MISSING, details)
    return Eligibility(True, None, [])


def deal_is_eligible(deal: Any) -> bool:
    """Same rule for a RankedDeal that already passed availability and the price cap."""
    price = getattr(deal, "price", None)
    if not config.is_price_in_tracking_scope(price):
        return False
    return evaluate_hardware(
        gpu=getattr(deal, "gpu", None),
        ram_gb=getattr(deal, "ram_gb", None),
        screen_resolution=getattr(deal, "screen_resolution", None),
    ).eligible


REASON_TEXT_RU = {
    NOT_DISCOVERED: "не найдена в магазинах",
    GPU_NOT_DETECTED: "видеокарта не подтверждена",
    NOT_AVAILABLE: "нет в наличии",
    SPECS_MISSING: "характеристики не подтверждены",
    PRICE_OVER_CAP: "цена выше лимита",
    HARD_FILTER_REJECTED: "не подходит по RAM или экрану",
    LOW_RANK: "подходит, но ниже ТОП",
    DUPLICATE_MODEL: "та же модель уже есть в рейтинге",
}
