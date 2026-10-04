from __future__ import annotations

"""International value score (no Russian history / cross-store saving)."""

from dataclasses import dataclass, field
from typing import Any

import config
from deal_ranking import (
    classify_cpu,
    score_gpu,
    score_price_value,
    score_ram,
    score_screen,
    score_ssd,
)


@dataclass
class InternationalScore:
    score: float
    raw: float
    breakdown: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "raw": self.raw,
            "breakdown": dict(self.breakdown),
            "reasons": list(self.reasons),
        }


def international_value_score(
    *,
    price_rub: int | None,
    gpu: str | None,
    cpu: str | None = None,
    ram_gb: int | None = None,
    ssd_gb: int | None = None,
    screen_inch: float | None = None,
    screen_resolution: str | None = None,
) -> InternationalScore:
    """
    Personal international value score 0..100.

    Uses the same personal GPU RUB targets as Russia (not Thai market fair value).
    Raw max = 88 (GPU25+price25+CPU15+RAM10+SSD5+screen8); normalize *100/88.
    Unknown fields contribute 0 (no unfair history penalty).
    """
    breakdown: dict[str, float] = {}
    reasons: list[str] = []

    g_pts, g_reason = score_gpu(gpu)
    # Clamp to intl maxima (same numeric values as Russian GPU component).
    g_pts = min(float(g_pts), float(config.INTL_SCORE_GPU_MAX))
    breakdown["gpu"] = g_pts
    if g_reason:
        reasons.append(g_reason)

    p_pts, p_reason = score_price_value(price_rub, gpu)
    p_pts = min(float(p_pts), float(config.INTL_SCORE_PRICE_MAX))
    breakdown["price"] = p_pts
    if p_reason:
        reasons.append(p_reason)

    c_pts, c_reason = classify_cpu(cpu)
    c_pts = min(float(c_pts), float(config.INTL_SCORE_CPU_MAX))
    breakdown["cpu"] = c_pts
    if c_reason:
        reasons.append(c_reason)

    r_pts, r_reason = score_ram(ram_gb)
    r_pts = min(float(r_pts), float(config.INTL_SCORE_RAM_MAX))
    breakdown["ram"] = r_pts
    if r_reason:
        reasons.append(r_reason)

    s_pts, s_reason = score_ssd(ssd_gb)
    s_pts = min(float(s_pts), float(config.INTL_SCORE_SSD_MAX))
    breakdown["ssd"] = s_pts
    if s_reason:
        reasons.append(s_reason)

    sc_pts, sc_reason = score_screen(screen_inch, screen_resolution)
    sc_pts = min(float(sc_pts), float(config.INTL_SCORE_SCREEN_MAX))
    breakdown["screen"] = sc_pts
    if sc_reason:
        reasons.append(sc_reason)

    raw = sum(breakdown.values())
    raw_max = float(config.INTL_SCORE_RAW_MAX) or 88.0
    score = round(max(0.0, min(100.0, (raw / raw_max) * 100.0)), 2)
    return InternationalScore(score=score, raw=round(raw, 2), breakdown=breakdown, reasons=reasons)
