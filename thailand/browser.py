from __future__ import annotations

"""Plain Chromium helper for Thailand adapters (no stealth / no bypass)."""

import logging
from contextlib import contextmanager
from typing import Iterator

import config

logger = logging.getLogger(__name__)


@contextmanager
def chromium_page(
    *,
    locale: str = "th-TH",
    timeout_ms: int | None = None,
    block_heavy_resources: bool | None = None,
) -> Iterator[object]:
    """
    Yield a Playwright page; always close browser in finally.

    Raises RuntimeError if playwright is unavailable.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright required for Thailand browser collection. "
            "Install: pip install playwright && playwright install chromium"
        ) from exc

    timeout_ms = int(
        timeout_ms
        if timeout_ms is not None
        else getattr(config, "THAILAND_BROWSER_TIMEOUT_MS", 45_000)
    )
    if block_heavy_resources is None:
        block_heavy_resources = bool(
            getattr(config, "THAILAND_LAZADA_BLOCK_HEAVY_RESOURCES", True)
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = None
        try:
            context = browser.new_context(
                locale=locale,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
            )
            if block_heavy_resources:
                def _route(route) -> None:  # type: ignore[no-untyped-def]
                    # Keep stylesheets — Lazada search DOM often depends on them.
                    rtype = route.request.resource_type
                    if rtype in {"image", "media", "font"}:
                        route.abort()
                    else:
                        route.continue_()

                try:
                    context.route("**/*", _route)
                except Exception:  # noqa: BLE001
                    pass
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            yield page
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                logger.warning("thailand browser.close() failed", exc_info=False)
