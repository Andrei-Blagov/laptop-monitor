from __future__ import annotations

"""Shared store constants / helpers."""

import re

TARGET_GPU_PATTERNS = (
    re.compile(r"RTX\s*5070\s*Ti", re.IGNORECASE),
    re.compile(r"RTX\s*5080(?!\s*Ti)", re.IGNORECASE),
)

MONITOR_REGION_MOSCOW = "moscow"


def matches_target_gpu(*parts: str | None) -> bool:
    text = " ".join(p for p in parts if p)
    if not text:
        return False
    return any(p.search(text) for p in TARGET_GPU_PATTERNS)


def parse_price(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n > 0 else None
    text = str(value).strip().replace("\u00a0", " ").replace(" ", "")
    # "254784.9000" / "254784,90"
    if re.fullmatch(r"\d+[.,]\d+", text):
        try:
            n = int(float(text.replace(",", ".")))
            return n if n > 0 else None
        except ValueError:
            return None
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    n = int(digits)
    return n if n > 0 else None


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def looks_like_challenge_page(html: str) -> bool:
    low = html.lower()
    markers = (
        "ddos-guard",
        "cf-browser-verification",
        "captcha-delivery",
        "checking your browser",
        "подождите",
        "access denied",
        "http 403",
        "http 401",
    )
    if not any(m in low for m in markers):
        return False
    if len(html) < 20_000:
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = clean_text(m.group(1)).lower() if m else ""
    return any(x in title for x in ("подождите", "403", "401", "ddos", "captcha", "access"))


def extract_specs_from_name(name: str) -> dict[str, object]:
    """Best-effort specs from product title (common RU marketplace format)."""
    out: dict[str, object] = {}
    if re.search(r"RTX\s*5080", name, re.I) and not re.search(
        r"RTX\s*5080\s*Ti", name, re.I
    ):
        out["gpu"] = "RTX 5080"
    elif re.search(r"RTX\s*5070\s*Ti", name, re.I):
        out["gpu"] = "RTX 5070 Ti"
    ram = re.search(r"(\d+)\s*G(?:B|б)\s*(?:RAM|ОЗУ|DDR)?", name, re.I)
    if not ram:
        ram = re.search(r"\b(\d{2})\s*GB\b", name, re.I)
    if ram:
        try:
            out["ram_gb"] = int(ram.group(1))
        except ValueError:
            pass
    ssd = re.search(r"(\d+)\s*(?:GB|ГБ|Tb|TB|ТБ)\s*SSD", name, re.I)
    if ssd:
        n = int(ssd.group(1))
        unit = ssd.group(0).upper()
        out["ssd_gb"] = n * 1024 if ("TB" in unit or "ТБ" in unit) else n
    screen = re.search(r"(\d{2}(?:[.,]\d)?)\s*(?:\"|”|″)", name)
    if not screen:
        screen = re.search(
            r"\b(\d{2}(?:[.,]\d)?)\s*(?:IPS|OLED|WQXGA|WUXGA|QHD|FHD)", name, re.I
        )
    if screen:
        try:
            out["screen_inch"] = float(screen.group(1).replace(",", "."))
        except ValueError:
            pass
    for pat in (
        r"(Intel\s+Core\s+Ultra\s+\d\s+\d+\w*)",
        r"(AMD\s+Ryzen\s+\d\s+\d+\w*)",
        r"(Core\s+Ultra\s+\d\s+\d+\w*)",
        r"(Ryzen\s+\d\s+\d+\w*)",
    ):
        m = re.search(pat, name, re.I)
        if m:
            out["cpu"] = clean_text(m.group(1))
            break
    return out
