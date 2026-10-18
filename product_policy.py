"""Approved 18 October 2026 purchase policy; pure policy, no I/O."""
MAX_PRICE_RUB = 290_000
GPU_TARGETS = (("RTX 5070 Ti", 230_000), ("RTX 5080", 280_000))
MIN_RAM_GB = 32
MIN_SSD_GB = 1000  # Both nominal 1000 and binary 1024 GB satisfy 1 TB.
MIN_SCREEN_INCH = 15.6
MIN_SCREEN_WIDTH_EXCLUSIVE = 1920
MIN_SCREEN_HEIGHT = 1440
MIN_SCREEN_PIXELS = 2560 * 1440
GPU_SPEC_URL = "https://www.nvidia.com/en-us/geforce/laptops/50-series/"
# NVIDIA Standard Memory Configuration, verified 2026-10-18. Laptop GPUs only.
LAPTOP_VRAM_GB = {"RTX 5070 Ti": 12, "RTX 5080": 16}


def exact_target_gpu(value):
    """Reject ambiguous ranges and explicitly non-laptop variants before normalization."""
    import re
    text = str(value or '').upper()
    if re.search(r'DESKTOP|НАСТОЛЬН|\bVGA\b', text):
        return None
    matches = {(number, suffix) for number, suffix in re.findall(r'(?<!\d)(50\d{2})\s*(TI|SUPER)?\b', text)}
    if len(matches) != 1 or not re.search(r'\bRTX\s*(5070\s*TI|5080)\b', text):
        return None
    number, suffix = next(iter(matches))
    name = 'RTX ' + number + (' Ti' if suffix == 'TI' else (' ' + suffix if suffix else ''))
    return name if name in LAPTOP_VRAM_GB else None
