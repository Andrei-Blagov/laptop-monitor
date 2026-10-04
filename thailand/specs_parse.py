from __future__ import annotations

"""Safe Thailand title/spec helpers (compatible with Russian anti-VRAM rules)."""

import re
from typing import Any

from deal_ranking import canonical_gpu
from stores.common import (
    clean_text,
    extract_specs_from_name,
    matches_target_gpu,
)

# Thai/EN availability markers
_AVAIL_MAP = (
    (re.compile(r"out[\s_]*of[\s_]*stock|หมด|สินค้าหมด|ไม่มีสินค้า", re.I), "out_of_stock"),
    (re.compile(r"pre[-\s_]?order|พรีออเดอร์|สั่งจอง", re.I), "preorder"),
    (
        re.compile(r"store[\s_]*pickup|รับที่ร้าน|pickup[\s_]*only", re.I),
        "store_pickup_only",
    ),
    (
        re.compile(
            r"online[\s_]*available|พร้อมส่ง|มีสินค้า|in[\s_]*stock|\bavailable\b",
            re.I,
        ),
        "online_available",
    ),
)

_REGIONAL_SUFFIX = re.compile(
    r"^(?P<base>.+?)[-_ ]?(?P<suf>TA|TH|THA|THL|THAI)$",
    re.I,
)

_BRANDS = (
    "ASUS",
    "MSI",
    "LENOVO",
    "HP",
    "DELL",
    "ACER",
    "GIGABYTE",
    "RAZER",
    "APPLE",
    "LG",
    "SAMSUNG",
)


def is_target_laptop_gpu(gpu: str | None, *extra: str | None) -> bool:
    """Only RTX 5070 Ti / RTX 5080 laptop GPUs."""
    key = canonical_gpu(gpu)
    if key in {"RTX 5070 Ti", "RTX 5080"}:
        return True
    return matches_target_gpu(gpu, *extra)


def exclude_non_target_gpu(text: str) -> bool:
    """True if text clearly indicates a non-target GPU (5050/5060/5070 non-Ti/5090/desktop)."""
    u = text.upper()
    # Desktop VGA / discrete graphics cards (Thai stores prefix VGA)
    if re.search(r"\bVGA\b|การ์ดแสดงผล|GRAPHICS\s*CARD|DESKTOP\s*GPU", u):
        return True
    if re.search(r"RTX\s*5090", u):
        return True
    if re.search(r"RTX\s*5060", u):
        return True
    if re.search(r"RTX\s*5050", u):
        return True
    # RTX 5070 without Ti
    if re.search(r"RTX\s*5070(?!\s*TI)", u) and "5070 TI" not in u and "5070TI" not in u:
        return True
    return False


def extract_brand(name: str) -> str | None:
    u = name.upper()
    for b in _BRANDS:
        if re.search(rf"\b{b}\b", u):
            return b.title() if b != "HP" else "HP"
    return None


def extract_mpn_candidates(name: str) -> list[str]:
    """Strong-ish manufacturer / regional SKU tokens from titles."""
    found: list[str] = []
    skip = {
        "NOTEBOOK",
        "GEFORCE",
        "WINDOWS",
        "PLATINUM",
        "GLACIER",
        "ECLIPSE",
        "LENOVO",
        "VECTOR",
        "LEGION",
        "CYBORG",
        "ZEPHYRUS",
        "ALIENWARE",
        "INTEL",
        "NVIDIA",
        "LAPTOP",
        "GAMING",
    }
    # Prefer tokens that mix digits+letters (true MPNs), e.g. 83F500NWTA.
    for m in re.finditer(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,}\b", name.upper()):
        tok = m.group(0)
        if tok in skip or tok in _BRANDS:
            continue
        found.append(tok)
    # Lenovo / ASUS style fallback without digit requirement already covered above.
    for m in re.finditer(r"\b([0-9A-Z]{4,}[A-Z]{2,}[0-9A-Z]*)\b", name.upper()):
        tok = m.group(1)
        if len(tok) < 6:
            continue
        if tok in skip or tok in _BRANDS:
            continue
        if tok not in found:
            found.append(tok)
    # ASUS style with hyphens: G614PR-TS113W
    for m in re.finditer(r"\b([A-Z0-9]{3,}(?:-[A-Z0-9]{2,})+)\b", name.upper()):
        found.append(m.group(1))
    # dedupe preserve order
    out: list[str] = []
    seen: set[str] = set()
    for t in found:
        if t not in seen:
            seen.add(t)
            out.append(t)

    def _mpn_rank(tok: str) -> tuple[int, int, int]:
        # Higher is better: regional suffix, starts with digit, longer.
        suf = 1 if re.search(r"(TA|TH|THA)$", tok) else 0
        digit_start = 1 if tok[:1].isdigit() else 0
        return (suf, digit_start, len(tok))

    out.sort(key=_mpn_rank, reverse=True)
    return out


def strip_regional_sku_suffix(sku: str | None) -> tuple[str | None, str | None]:
    """
    Split regional suffix for comparison.

    Does NOT authorize EXACT match — callers must treat base-only as SAME_FAMILY.
    """
    if not sku:
        return None, None
    m = _REGIONAL_SUFFIX.match(sku.strip())
    if not m:
        return sku.strip(), None
    return m.group("base"), m.group("suf").upper()


def normalize_availability(
    *,
    available_flag: bool | None = None,
    status_text: str | None = None,
) -> tuple[bool, str]:
    """
    Returns (available, availability_status).

    When neither status_text nor available_flag is informative:
    available=False, status=unknown (never invent in_stock).
    """
    text = status_text or ""
    for pat, status in _AVAIL_MAP:
        if pat.search(text):
            if status == "out_of_stock":
                return False, status
            if status == "preorder":
                return True, status
            if status == "store_pickup_only":
                return True, status
            # online_available / in_stock family
            return True, status if status != "online_available" else "online_available"
    if available_flag is False:
        return False, "out_of_stock"
    if available_flag is True:
        return True, "in_stock"
    return False, "unknown"


def parse_thb_price(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n > 0 else None
    text = str(value).strip().replace(",", "").replace("฿", "").replace("THB", "")
    text = re.sub(r"\s+", "", text)
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    try:
        n = int(float(m.group(1)))
    except ValueError:
        return None
    return n if n > 0 else None


def gpu_from_explicit_text(text: str | None) -> str | None:
    """Authoritative GPU only from explicit product text (title/detail), never search query."""
    if not text:
        return None
    # Prefer explicit graphics line markers when present.
    for m in re.finditer(
        r"(?:กราฟิก|GPU|Graphics)\s*[:：]\s*[^<\n]{0,80}?(RTX\s*50\d0(?:\s*Ti)?)",
        text,
        re.I,
    ):
        key = canonical_gpu(m.group(1))
        if key:
            return key
    specs = extract_specs_from_name(text)
    gpu = specs.get("gpu")
    return str(gpu) if isinstance(gpu, str) else None


def specs_from_text(
    name: str,
    *,
    catalog_gpu: str | None = None,
    apply_catalog_gpu: bool = False,
) -> dict[str, Any]:
    """
    Safe specs extraction.

    catalog_gpu / search context is NEVER written to authoritative gpu unless
    apply_catalog_gpu=True (legacy/tests only — production JIB must keep False).
    """
    specs = extract_specs_from_name(name)
    out: dict[str, Any] = dict(specs)
    cg = canonical_gpu(catalog_gpu) if catalog_gpu else None
    if cg:
        out["candidate_gpu"] = cg
    if apply_catalog_gpu and not out.get("gpu") and cg:
        # Intentionally opt-in only — unsafe for search_suggestion discovery.
        out["gpu"] = cg
        out["gpu_source"] = "search_query"
    elif out.get("gpu"):
        out["gpu_source"] = "explicit_title"
    # Thai titles often encode screen as leading digits in model (16IAX / G16 / 15 MAX)
    if out.get("screen_inch") is None:
        m = re.search(
            r"\b(?:G|PRO\s*)?(15\.6|16|17|18)(?:IAX|IRX|AHP|\"|\s)",
            name,
            re.I,
        )
        if m:
            try:
                out["screen_inch"] = float(m.group(1))
            except ValueError:
                pass
    brand = extract_brand(name)
    if brand:
        out["brand"] = brand
    mpns = extract_mpn_candidates(name)
    if mpns:
        out["manufacturer_part_number"] = mpns[0]
        out["sku_candidates"] = mpns
    out["name"] = clean_text(name)
    return out


def is_purchasable_for_best(status: str | None, available: bool) -> bool:
    """Legacy helper — unknown/None availability is NOT purchasable for automatic best."""
    if not available:
        return False
    if status in {"out_of_stock", "unknown", None}:
        return False
    if status == "preorder":
        return False
    return status in {
        "in_stock",
        "online_available",
        "store_pickup_only",
    }
