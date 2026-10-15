from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

import httpx

import config
from thailand.models import FxRate

logger = logging.getLogger(__name__)

CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


def parse_cbr_thb_xml(xml_text: str, *, fetched_at: datetime | None = None) -> FxRate:
    """Parse CBR daily XML and compute RUB per 1 THB (handles nominal)."""
    fetched_at = fetched_at or datetime.now(timezone.utc)
    root = ET.fromstring(xml_text)
    published = root.attrib.get("Date")
    for valute in root.findall("Valute"):
        char = (valute.findtext("CharCode") or "").strip().upper()
        if char != "THB":
            continue
        nominal = int(float((valute.findtext("Nominal") or "1").replace(",", ".")))
        value_raw = (valute.findtext("Value") or "").replace(",", ".")
        official = float(value_raw)
        if nominal <= 0:
            raise ValueError("CBR THB nominal invalid")
        rub_per_thb = official / nominal
        return FxRate(
            source="CBR",
            currency="THB",
            nominal=nominal,
            official_rate=official,
            rub_per_thb=rub_per_thb,
            published_date=published,
            fetched_at=fetched_at,
            stale=False,
            rate_age_hours=0.0,
        )
    raise ValueError("CBR XML missing THB")


def fetch_cbr_thb_rate(
    *,
    client: httpx.Client | None = None,
    timeout: float | None = None,
) -> FxRate | None:
    """Fetch current CBR THB→RUB. Returns None on failure (fail-safe)."""
    owns = client is None
    timeout = float(timeout if timeout is not None else 15.0)
    client = client or httpx.Client(follow_redirects=True, timeout=timeout)
    try:
        resp = client.get(CBR_DAILY_URL)
        if resp.status_code >= 400:
            logger.warning("CBR FX HTTP %s", resp.status_code)
            return None
        # CBR uses windows-1251
        text = resp.content.decode("windows-1251", errors="replace")
        return parse_cbr_thb_xml(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CBR FX failed: %s", type(exc).__name__)
        return None
    finally:
        if owns:
            client.close()


def cbr_published_iso(raw: str | None) -> str | None:
    """CBR prints ``DD.MM.YYYY``. The market observation stores an ISO date."""
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def refresh_fx_observation(
    path: Path | str | None = None,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Update the configured FX file with ``fetch_cbr_thb_rate``.

    A failed fetch leaves the previous file in place. An unreadable publication
    date is stored as missing, so it cannot authorize a current RUB benefit.
    """
    from pathlib import Path as PathType

    import config
    from thailand.job_queue import _atomic_write

    destination = PathType(path or config.FX_OBSERVATION_PATH)
    rate = fetch_cbr_thb_rate(client=client)
    if rate is None:
        if not destination.is_file():
            return None
        try:
            import json
            data = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    mark_stale_if_old(rate, now=now)
    published = cbr_published_iso(rate.published_date)
    payload = {
        "fx_id": f"cbr-{published or 'undated'}-{rate.nominal}",
        "source": "CBR",
        "currency": rate.currency or "THB",
        "nominal": rate.nominal,
        "official_rate": rate.official_rate,
        "rub_per_thb": str(rate.rub_per_thb),
        "fetched_at": rate.fetched_at.isoformat(),
        "published_date": published,
        "stale": rate.stale,
        "rate_age_hours": rate.rate_age_hours,
        "error": rate.error,
    }
    _atomic_write(destination, payload)
    return payload


def thb_to_rub(price_thb: int | float | None, fx: FxRate | None) -> int | None:
    if price_thb is None or fx is None or fx.rub_per_thb <= 0:
        return None
    try:
        return int(round(float(price_thb) * float(fx.rub_per_thb)))
    except (TypeError, ValueError):
        return None


def mark_stale_if_old(fx: FxRate, *, now: datetime | None = None) -> FxRate:
    now = now or datetime.now(timezone.utc)
    age_h = max(0.0, (now - fx.fetched_at).total_seconds() / 3600.0)
    fx.rate_age_hours = age_h
    fx.stale = age_h > float(config.THAILAND_FX_STALE_MAX_HOURS)
    return fx


def fx_usable_for_verdict(fx: FxRate | None) -> bool:
    if fx is None:
        return False
    if fx.stale:
        return False
    if fx.error:
        return False
    return fx.rub_per_thb > 0
