from __future__ import annotations

"""Shared store constants / helpers."""

import re

TARGET_GPU_PATTERNS = (
    re.compile(r"RTX\s*5070\s*Ti", re.IGNORECASE),
    re.compile(r"RTX\s*5080(?!\s*Ti)", re.IGNORECASE),
)

MONITOR_REGION_MOSCOW = "moscow"

# VRAM / GDDR markers — must not be treated as system RAM.
_VRAM_CONTEXT = re.compile(
    r"(?:GDDR\d*|VRAM|видеопамят|для\s+ноутбуков?\s*[-–—]?\s*\d+\s*Г?Б)",
    re.IGNORECASE,
)


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


def _extract_gpu_from_name(name: str) -> str | None:
    if re.search(r"RTX\s*5070\s*Ti", name, re.I):
        return "RTX 5070 Ti"
    if re.search(r"RTX\s*5080(?!\s*Ti)", name, re.I):
        return "RTX 5080"
    return None


def _extract_cpu_from_name(name: str) -> str | None:
    patterns = (
        r"((?:Intel\s+)?Core\s+Ultra\s+[79](?:\s+\d{3,5}[A-Z]{0,4})?)",
        r"((?:Intel\s+)?Core\s+i[3579]-?\d{4,5}[A-Z]{0,4})",
        r"((?:Intel\s+)?Core\s+i[3579]\s+\d{4,5}[A-Z]{0,4})",
        r"((?:Intel\s+)?Core\s+[79]\s+\d{3,5}[A-Z]{0,4})",
        r"((?:AMD\s+)?Ryzen\s+AI\s+[79](?:\s+\d{3,5}[A-Z]{0,4})?)",
        r"((?:AMD\s+)?Ryzen\s+[79]\s+\d{3,5}[A-Z]{0,4})",
    )
    for pat in patterns:
        m = re.search(pat, name, re.I)
        if m:
            return clean_text(m.group(1))
    return None


def _ram_candidate_ok(name: str, start: int, end: int, amount: int) -> bool:
    """Reject GPU VRAM amounts mistaken for system RAM."""
    if amount not in {8, 16, 24, 32, 48, 64}:
        return False
    # Tight window only — a prior "16GB GDDR7" clause must not poison later RAM.
    window_before = name[max(0, start - 18) : start]
    window_after = name[end : min(len(name), end + 18)]
    if _VRAM_CONTEXT.search(window_before + " " + window_after):
        return False
    # "RTX 5080 16GB GDDR7" / "RTX 5070 Ti - 12 ГБ"
    if re.search(r"RTX\s*50\d0(?:\s*Ti)?\s*$", window_before, re.I):
        return False
    if re.search(r"GDDR", window_after, re.I):
        return False
    return True


def _extract_ram_gb_from_name(name: str) -> int | None:
    # Explicit RAM / DDR / ОЗУ context (RU + EN).
    # (?<!G)DDR avoids matching the DDR inside GDDR7 VRAM markers.
    explicit_pat = (
        r"(\d{1,2})\s*(?:GB|ГБ|Gб)\s*"
        r"(?:RAM|ОЗУ|(?<![Gg])DDR\d*[Xx]?|LPDDR\d*[Xx]?)"
    )
    for m in re.finditer(explicit_pat, name, re.I):
        amount = int(m.group(1))
        if amount in {8, 16, 24, 32, 48, 64}:
            return amount
    for m in re.finditer(
        r"(?:RAM|ОЗУ|память)\s*[:\-]?\s*(\d{1,2})\s*(?:GB|ГБ)",
        name,
        re.I,
    ):
        amount = int(m.group(1))
        if _ram_candidate_ok(name, m.start(1), m.end(), amount):
            return amount
    # Product-format fallback with anti-VRAM guards.
    for m in re.finditer(
        r"(?<![A-Z0-9])(\d{2})\s*(?:GB|ГБ)(?!\s*GDDR)(?=\s*[,;/]|\s+(?:(?<!G)DDR|LPDDR|RAM|ОЗУ))",
        name,
        re.I,
    ):
        amount = int(m.group(1))
        if _ram_candidate_ok(name, m.start(1), m.end(), amount):
            return amount
    return None


def _extract_ssd_gb_from_name(name: str) -> int | None:
    patterns = (
        r"(\d+(?:[.,]\d+)?)\s*(TB|ТБ|Gb|GB|ГБ)\s*(?:SSD|NVMe|M\.?2)",
        r"(\d+(?:[.,]\d+)?)\s*(TB|ТБ|Gb|GB|ГБ)\s*(?:накопител)",
        r"(?:SSD|NVMe)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(TB|ТБ|Gb|GB|ГБ)",
    )
    for pat in patterns:
        m = re.search(pat, name, re.I)
        if not m:
            continue
        try:
            amount = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        unit = (m.group(2) or "").upper().replace("ТБ", "TB").replace("ГБ", "GB")
        if unit == "TB":
            return int(amount * 1024)
        return int(amount)
    # "1ТБ SSD" already covered; "1024GB SSD"
    m = re.search(r"(\d{3,4})\s*(?:GB|ГБ)\s*SSD", name, re.I)
    if m:
        return int(m.group(1))
    return None


def _extract_screen_inch_from_name(name: str) -> float | None:
    # Explicit inch marks: 15.6" / 16” / 17.3″
    m = re.search(
        r"\b(15\.6|16\.1|17\.3|18(?:\.0)?|17(?:\.0)?|16(?:\.0)?)\s*(?:\"|”|″|''|дюйм)",
        name,
        re.I,
    )
    if m:
        return float(m.group(1))
    # Common marketplace: `16", IPS` / `18", IPS`
    m = re.search(
        r"\b(15\.6|16\.1|17\.3|18|17|16)\s*(?:\"|”|″)\s*,",
        name,
    )
    if m:
        return float(m.group(1))
    # `16" 2560x1600` / `18" 2560x1600`
    m = re.search(
        r"\b(15\.6|16\.1|17\.3|18|17|16)\s*(?:\"|”|″)\s+\d{3,4}\s*[xх×]",
        name,
        re.I,
    )
    if m:
        return float(m.group(1))
    # Panel token after size: `16" IPS` already covered; bare `16, IPS` / `18, IPS`
    m = re.search(
        r"\b(15\.6|16\.1|17\.3|18|17|16)\s*,\s*(?:IPS|OLED|WQXGA|WUXGA|QHD|FHD|Mini-?LED)",
        name,
        re.I,
    )
    if m:
        return float(m.group(1))
    return None


def _extract_resolution_from_name(name: str) -> str | None:
    m = re.search(r"\b(\d{3,4})\s*[xх×]\s*(\d{3,4})\b", name, re.I)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w < 1280 or h < 720:
        return None
    return f"{w}x{h}"


def extract_specs_from_name(name: str) -> dict[str, object]:
    """
    Safe best-effort specs from product title.

    Does not invent values; avoids treating GPU VRAM as system RAM.
    """
    out: dict[str, object] = {}
    if not name:
        return out

    gpu = _extract_gpu_from_name(name)
    if gpu:
        out["gpu"] = gpu

    cpu = _extract_cpu_from_name(name)
    if cpu:
        out["cpu"] = cpu

    ram = _extract_ram_gb_from_name(name)
    if ram is not None:
        out["ram_gb"] = ram

    ssd = _extract_ssd_gb_from_name(name)
    if ssd is not None:
        out["ssd_gb"] = ssd

    screen = _extract_screen_inch_from_name(name)
    if screen is not None:
        out["screen_inch"] = screen

    resolution = _extract_resolution_from_name(name)
    if resolution:
        out["screen_resolution"] = resolution

    return out
