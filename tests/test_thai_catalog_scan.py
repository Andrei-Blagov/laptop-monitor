"""C2.1 offline integration of store results into the Thai catalog."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from thai_catalog import ThaiCatalogError, build_thai_catalog, load_thai_catalog
from thailand.catalog_scan import publish_collected_catalog
from thailand.discovery import GPU_SEARCH_QUERIES
from thailand.invadeit import enrich_offer_from_specifications, parse_invadeit_item
from thailand.jib import apply_detail_verification, parse_read_product_html, parse_suggestion_rec
from thailand.models import FxRate, StoreScanResult, ThailandOffer
from thailand.scanner import run_thailand_scan

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
OBSERVED = NOW - timedelta(minutes=20)
DETAIL_AT = NOW - timedelta(minutes=10)
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "invadeit"


def _evidence(collected: datetime) -> dict:
    entry = {"method": "structured_api", "captured_at": collected.isoformat()}
    return {name: dict(entry) for name in ("cpu", "ram_gb", "ssd_gb", "screen_inch", "screen_resolution")}


def _offer(external_id: str, sku: str, price: int) -> ThailandOffer:
    return ThailandOffer(
        store="jib",
        external_id=external_id,
        name=f"ASUS ROG Strix {sku}",
        url=f"https://example.test/jib/{external_id}",
        price_thb=price,
        available=True,
        collected_at=OBSERVED,
        sku=sku,
        manufacturer_part_number=sku,
        brand="ASUS",
        cpu="Intel Core Ultra 9 275HX",
        gpu="RTX 5070 Ti",
        gpu_source="structured_api",
        ram_gb=32,
        ssd_gb=1024,
        screen_size_inch=16.0,
        screen_resolution="2560x1600",
        availability_status="in_stock",
        availability_confirmed=True,
        availability_source="structured_api",
        price_source="structured_api",
        price_verified_for_variant=True,
        channel="direct",
        metadata={"field_evidence": _evidence(OBSERVED)},
    )


def _store(offers: list[ThailandOffer], *, store: str = "jib", finished: list[str] | None = None, rate_limited: bool = False) -> StoreScanResult:
    return StoreScanResult(
        store=store,
        ok=not rate_limited,
        offers=offers,
        queries_finished=["notebook rtx 5070 ti"] if rate_limited else list(finished or GPU_SEARCH_QUERIES),
        discovery_queries=list(GPU_SEARCH_QUERIES),
        request_count=1 if rate_limited else len(GPU_SEARCH_QUERIES),
        error_code="RATE_LIMITED" if rate_limited else None,
        discovery_rate_limited=rate_limited,
        collection_mode="rate_limited" if rate_limited else "http",
        retry_after_seconds=60 if rate_limited else None,
    )


def _fx() -> dict:
    return {
        "fx_id": "cbr-c21",
        "source": "CBR",
        "currency": "THB",
        "nominal": 1,
        "rub_per_thb": "2.5",
        "fetched_at": OBSERVED.isoformat(),
        "published_date": "2026-10-14",
        "error": None,
    }


def _russian_25() -> dict:
    observed = OBSERVED.isoformat()
    evidence = {
        "source": "regard",
        "external_id": "ru-17",
        "captured_at": observed,
        "method": "specs_cache",
        "capture_state": "known",
    }
    specs = {
        "cpu": "Intel Core Ultra 9 275HX",
        "gpu": "RTX 5070 Ti",
        "ram_gb": 32,
        "ssd_gb": 1024,
        "screen_inch": 16,
        "screen_resolution": "2560x1600",
    }
    offers = []
    for index in range(1, 26):
        code = "SKU-MATCH-17" if index == 17 else f"SKU-RU-{index:02d}"
        eid = f"ru-{index:02d}"
        row_evidence = {**evidence, "external_id": eid}
        offers.append({
            "offer_id": eid,
            "external_id": eid,
            "store": "regard",
            "model_id": f"model-{eid}",
            "sku_candidates": [code],
            "mpn_candidates": [],
            "public_price_rub": 180_000 + index * 1000,
            "price_checked_at": observed,
            "availability_checked_at": observed,
            "observed_at": observed,
            "stock_state": "in_stock",
            "availability_evidence": {**row_evidence, "method": "store_listing", "available": True},
            "product_name": f"ASUS ROG Strix {code}",
            "specs": {name: {"value": value, "evidence": row_evidence} for name, value in specs.items()},
        })
    return {
        "schema_version": 1,
        "snapshot_id": "ru-25",
        "instance_id": "c21-test",
        "created_at": NOW.isoformat(),
        "offers": offers,
        "source_coverage": [{"store": "regard", "state": "ok", "latest_attempt_at": observed}],
        "top_model_ids": [f"model-ru-{index:02d}" for index in range(1, 11)],
        "personal_profiles": [],
    }


class AdapterEvidenceTests(unittest.TestCase):
    def test_jib_title_and_detail_keep_separate_observations(self) -> None:
        discovered = parse_suggestion_rec(
            [{
                "id": "0000085696",
                "title": "NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W Intel Core Ultra 9 275HX 32GB RAM 1TB SSD RTX 5080 16\"",
                "price": "90000.00",
                "salePrice": "84990.00",
            }],
            catalog_gpu="RTX 5070 Ti",
            collected_at=OBSERVED,
        )[0]
        self.assertEqual(discovered.metadata["field_evidence"]["cpu"]["method"], "explicit_title")
        self.assertEqual(discovered.metadata["field_evidence"]["cpu"]["captured_at"], OBSERVED.isoformat())
        self.assertNotIn("gpu", discovered.metadata["field_evidence"])
        self.assertNotIn("screen_resolution", discovered.metadata["field_evidence"])
        html = """
        <html><h1>NOTEBOOK ASUS ROG STRIX G16 G614PR-TS113W</h1>
        <p>• ซีพียู : AMD Ryzen 9 8940HX</p>
        <p>• กราฟิก : Nvidia GeForce RTX 5070 Ti 12GB GDDR7</p>
        <p>• เอสเอสดี : 1TB PCIe 4/NVMe M.2 SSD</p>
        <p>• จอแสดงผล : 16&quot; 2.5K (2560 x 1600, WQXGA)</p>
        <script>qty = 2;</script></html>
        """
        detail = parse_read_product_html(html, external_id="0000085696", collected_at=DETAIL_AT)
        offer = apply_detail_verification(discovered, detail)
        evidence = offer.metadata["field_evidence"]
        self.assertEqual(evidence["cpu"]["captured_at"], DETAIL_AT.isoformat())
        self.assertEqual(evidence["cpu"]["method"], "structured_product_page")
        self.assertEqual(evidence["screen_resolution"]["captured_at"], DETAIL_AT.isoformat())
        self.assertEqual(evidence["ram_gb"]["captured_at"], OBSERVED.isoformat())
        self.assertEqual(offer.gpu_source, "structured_product_page")
        self.assertEqual(evidence["gpu"]["captured_at"], DETAIL_AT.isoformat())

    def test_invadeit_detail_does_not_copy_resolution_between_skus(self) -> None:
        payload = json.loads((FIXTURES / "81984.json").read_text(encoding="utf-8"))
        first = parse_invadeit_item({
            "id": 81984,
            "name": "Notebook SKU-A",
            "productNumber": "SKU-A",
            "urlName": "sku-a",
            "price": 99990,
            "inStock": True,
            "keySpecs": "GeForce RTX 5070 Ti 12GB",
            "manufacturerName": "ASUS",
            "categoryName": "Notebooks / Laptops",
            "categoryUrlName": "notebooks-laptops",
            "manufacturerUrlName": "asus",
        }, collected_at=OBSERVED)
        second = parse_invadeit_item({
            "id": 81991,
            "name": "Notebook SKU-B",
            "productNumber": "SKU-B",
            "urlName": "sku-b",
            "price": 109990,
            "inStock": True,
            "keySpecs": "GeForce RTX 5070 Ti 12GB",
            "manufacturerName": "ASUS",
            "categoryName": "Notebooks / Laptops",
            "categoryUrlName": "notebooks-laptops",
            "manufacturerUrlName": "asus",
        }, collected_at=OBSERVED)
        assert first is not None and second is not None
        enrich_offer_from_specifications(first, payload, fetched_at=DETAIL_AT)
        self.assertEqual(first.screen_resolution, "2560x1600")
        self.assertEqual(first.metadata["field_evidence"]["screen_resolution"]["captured_at"], DETAIL_AT.isoformat())
        self.assertIsNone(second.screen_resolution)
        self.assertNotIn("screen_resolution", second.metadata.get("field_evidence") or {})


class CatalogIntegrationTests(unittest.TestCase):
    def test_full_catalog_ranks_without_russia_and_matches_model_17(self) -> None:
        offers = [_offer(f"th-{index}", f"SKU-TH-{index:02d}", 70000 + index * 1000) for index in range(1, 6)]
        offers.append(_offer("th-match", "SKU-MATCH-17", 80_000))
        stale = _offer("th-stale", "SKU-STALE", 60_000)
        stale.collected_at = NOW - timedelta(hours=8)
        stale.metadata = {"field_evidence": _evidence(stale.collected_at)}
        document_results = [
            _store(offers + [stale]),
            _store([], store="advice", rate_limited=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            published = publish_collected_catalog(
                document_results,
                snapshot_root=tmp,
                now=NOW,
                discovery_checked_at=OBSERVED,
                instance_id="c21-test",
                fx=_fx(),
            )
            self.assertEqual(published["status"], "published")
            self.assertIsNone(published["comparison"])
            self.assertGreaterEqual(published["offer_count"], 6)
            self.assertEqual(published["top_count"], 5)
            self.assertGreaterEqual(published["eligible_count"], 6)
            catalog = load_thai_catalog(tmp)
            self.assertEqual(
                next(row["price_checked_at"] for row in catalog["offers"] if row["external_id"] == "th-stale"),
                stale.collected_at.isoformat(),
            )
            self.assertNotEqual(catalog["offers"][0]["price_checked_at"], catalog["created_at"])
            coverage = {row["source"]: row for row in catalog["source_coverage"]}
            self.assertEqual(coverage["advice"]["state"], "429")
            self.assertTrue(coverage["advice"]["not_completed_keys"])
            compared = publish_collected_catalog(
                document_results,
                snapshot_root=tmp,
                now=NOW,
                discovery_checked_at=OBSERVED,
                instance_id="c21-test",
                russian_snapshot=_russian_25(),
                fx=_fx(),
            )
            pairs = [pair for pair in compared["comparison"]["pairs"] if pair["comparison_status"] == "confirmed"]
            self.assertEqual({pair["ru_offer_id"] for pair in pairs}, {"ru-17"})
            self.assertNotIn("ru-01", {pair["ru_offer_id"] for pair in pairs})

    def test_publication_error_keeps_previous_latest(self) -> None:
        offers = [_offer("th-1", "SKU-ONE", 80_000)]
        with tempfile.TemporaryDirectory() as tmp:
            first = publish_collected_catalog(
                [_store(offers)],
                snapshot_root=tmp,
                now=NOW,
                discovery_checked_at=OBSERVED,
                instance_id="c21-test",
                fx=_fx(),
            )
            raw = (Path(tmp) / "snapshots" / f"{first['snapshot_id']}.json").read_bytes()
            with patch("thailand.catalog_scan.export_thai_catalog", side_effect=ThaiCatalogError("disk")):
                failed = publish_collected_catalog(
                    [_store(offers)],
                    snapshot_root=tmp,
                    now=NOW + timedelta(hours=1),
                    discovery_checked_at=OBSERVED,
                    instance_id="c21-test",
                    fx=_fx(),
                )
            self.assertEqual(failed["status"], "export_failed")
            self.assertEqual(load_thai_catalog(tmp)["snapshot_id"], first["snapshot_id"])
            self.assertEqual((Path(tmp) / "snapshots" / f"{first['snapshot_id']}.json").read_bytes(), raw)

    def test_scanner_catalog_is_optional_and_does_not_replace_legacy_top(self) -> None:
        offer = _offer("th-1", "SKU-ONE", 80_000)
        result = _store([offer])
        fx = FxRate(
            source="CBR",
            currency="THB",
            nominal=1,
            official_rate=2.5,
            rub_per_thb=2.5,
            published_date="2026-10-14",
            fetched_at=OBSERVED,
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "thailand.scanner.fetch_cbr_thb_rate", return_value=fx
        ) as rate, patch(
            "thailand.scanner.collect_thailand_offers", return_value=([result], [offer], [], 0.01)
        ) as collect, patch("thailand.scanner.record_observation"):
            plain = run_thailand_scan(trigger_type="manual", write_snapshot=False)
            self.assertNotIn("market_catalog", plain)
            self.assertIn("thailand_top", plain)
            published = run_thailand_scan(
                trigger_type="manual",
                write_snapshot=False,
                catalog_root=tmp,
            )
            self.assertEqual(rate.call_count, 2)
            self.assertEqual(collect.call_count, 2)
            self.assertEqual(published["market_catalog"]["status"], "published")
            self.assertIn("thailand_top", published)
            self.assertGreaterEqual(published["market_catalog"]["offer_count"], 1)
            stored = load_thai_catalog(tmp)
            self.assertTrue(stored["offers"])
        self.assertEqual(build_thai_catalog([result], now=NOW, discovery_checked_at=OBSERVED, instance_id="x")["top_model_ids"], [])


if __name__ == "__main__":
    unittest.main()
