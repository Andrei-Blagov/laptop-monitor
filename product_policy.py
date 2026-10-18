"""Approved 18 October 2026 purchase policy; constants only, no I/O."""
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
