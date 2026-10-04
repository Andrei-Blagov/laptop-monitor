from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from integrations.n8n import (
    build_pipeline_completed_payload,
    post_pipeline_webhook,
    sign_webhook,
    verify_webhook_signature,
)
from parsers.citilink import fetch_target_laptops as fetch_citilink
from parsers.citilink import parse_citilink_product_html
from parsers.kns import fetch_target_laptops as fetch_kns
from parsers.kns import parse_kns_catalog_html
from run_pipeline import PIPELINE_STATUS_SUCCESS, PipelineResult, run_pipeline
from stores.common import looks_like_challenge_page, parse_price
from stores.registry import enabled_slugs, get_adapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class KnsParserTests(unittest.TestCase):
    def test_parse_fixture_public_price(self) -> None:
        html = (FIXTURES / "kns_5070ti.html").read_text(encoding="utf-8")
        products = parse_kns_catalog_html(html)
        self.assertGreaterEqual(len(products), 5)
        for p in products:
            self.assertEqual(p.store, "kns")
            self.assertIsNotNone(p.price)
            self.assertLess(p.price, 2_000_000)
            self.assertGreater(p.price, 50_000)
            self.assertTrue(p.url.startswith("https://www.kns.ru/"))
            member = (p.metadata or {}).get("member_price")
            if member is not None:
                self.assertNotEqual(member, p.price)
                self.assertLess(int(member), int(p.price))

    def test_decimal_price_not_inflated(self) -> None:
        self.assertEqual(parse_price("254784.9000"), 254784)

    def test_challenge_detection(self) -> None:
        self.assertTrue(looks_like_challenge_page("<html><title>HTTP 403</title></html>"))
        self.assertFalse(
            looks_like_challenge_page("<html><title>Каталог</title>" + ("x" * 30000) + "</html>")
        )


class CitilinkParserTests(unittest.TestCase):
    def test_json_ld_public_price(self) -> None:
        html = (FIXTURES / "citilink_product_5070ti.html").read_text(encoding="utf-8")
        product = parse_citilink_product_html(
            html,
            "https://www.citilink.ru/product/noutbuk-asus-rog-strix-g614pr-rv027-2144010/",
        )
        self.assertIsNotNone(product)
        assert product is not None
        self.assertEqual(product.external_id, "2144010")
        self.assertEqual(product.price, 309460)
        self.assertTrue(product.available)
        self.assertIn("5070", product.name.upper())

    def test_fetch_pages_fixture(self) -> None:
        pages = [
            (
                "https://www.citilink.ru/product/a-2144010/",
                (FIXTURES / "citilink_product_5070ti.html").read_text(encoding="utf-8"),
            ),
            (
                "https://www.citilink.ru/product/b-2153986/",
                (FIXTURES / "citilink_product_5080.html").read_text(encoding="utf-8"),
            ),
        ]
        products = fetch_citilink(html_pages=pages)
        self.assertEqual(len(products), 2)
        ids = {p.external_id for p in products}
        self.assertEqual(ids, {"2144010", "2153986"})


class StoreMetaTests(unittest.TestCase):
    def test_moscow_region_and_modes(self) -> None:
        for slug in ("regard", "andpro", "kns", "citilink"):
            a = get_adapter(slug)
            self.assertIsNotNone(a)
            assert a is not None
            self.assertEqual(a.region, "moscow")
            self.assertEqual(a.price_semantics, "public")
        self.assertEqual(get_adapter("citilink").collection_mode, "browser")
        self.assertEqual(get_adapter("kns").collection_mode, "http")
        self.assertEqual(enabled_slugs(), ["regard", "andpro", "kns", "citilink"])


class N8nIntegrationTests(unittest.TestCase):
    def test_payload_has_no_secrets(self) -> None:
        payload = build_pipeline_completed_payload(
            run_id=1,
            status="success",
            started_at="t0",
            finished_at="t1",
            duration_seconds=1.2,
            store_statuses={"regard": {"status": "ok", "products_count": 10}},
            alerts_created=2,
            messages_sent=1,
            messages_failed=0,
            top_deals=[{"name": "X", "store": "regard", "price": 200000, "score": 50}],
        )
        blob = json.dumps(payload)
        self.assertNotIn("TELEGRAM", blob)
        self.assertNotIn("token", blob.lower())
        self.assertNotIn(".env", blob)
        self.assertEqual(payload["event"], "pipeline.completed")
        self.assertEqual(payload["stores"][0]["slug"], "regard")

    def test_hmac_roundtrip(self) -> None:
        body = b'{"event":"pipeline.completed"}'
        ts, sig = sign_webhook(body, "secret", timestamp="1700000000")
        self.assertTrue(
            verify_webhook_signature(
                body, "secret", ts, sig, now=1700000000, max_skew_seconds=10
            )
        )
        self.assertFalse(
            verify_webhook_signature(
                body, "wrong", ts, sig, now=1700000000, max_skew_seconds=10
            )
        )

    def test_disabled_mode(self) -> None:
        with patch("integrations.n8n.config.get_n8n_webhook_config", return_value=(None, None, 1.0)):
            result = post_pipeline_webhook({"event": "pipeline.completed"})
            self.assertTrue(result["skipped"])
            self.assertTrue(result["ok"])

    def test_timeout_does_not_raise(self) -> None:
        client = MagicMock()
        client.post.side_effect = httpx.TimeoutException("timeout")
        with patch(
            "integrations.n8n.config.get_n8n_webhook_config",
            return_value=("https://example.test/hook", "sec", 1.0),
        ):
            result = post_pipeline_webhook({"event": "pipeline.completed"}, client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    def test_http_500_does_not_raise(self) -> None:
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        client.post.return_value = resp
        with patch(
            "integrations.n8n.config.get_n8n_webhook_config",
            return_value=("https://example.test/hook", None, 1.0),
        ):
            result = post_pipeline_webhook({"event": "x"}, client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 500)

    def test_n8n_failure_does_not_change_pipeline_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            products = [
                type(
                    "P",
                    (),
                    {
                        "store": "regard",
                        "external_id": "1",
                        "url": "https://r/1",
                        "name": "Laptop A SKU1 RTX 5070 Ti",
                        "sku": "SKU1",
                        "price": 220000,
                        "available": True,
                        "checked_at": datetime.now(timezone.utc),
                    },
                )()
            ]
            # Use real Product
            from models import Product

            products = [
                Product(
                    store="regard",
                    external_id="1",
                    url="https://r/1",
                    name="Laptop A SKU1 RTX 5070 Ti",
                    sku="SKU1",
                    price=220000,
                    available=True,
                    checked_at=datetime.now(timezone.utc),
                ),
                Product(
                    store="andpro",
                    external_id="2",
                    url="https://a/2",
                    name="Laptop A SKU1 RTX 5070 Ti",
                    sku="SKU1",
                    price=230000,
                    available=True,
                    checked_at=datetime.now(timezone.utc),
                ),
            ]

            def fetch_r():
                return [products[0]]

            def fetch_a():
                return [products[1]]

            with patch(
                "run_pipeline.post_pipeline_webhook",
                side_effect=RuntimeError("boom"),
            ):
                result = run_pipeline(
                    db,
                    fetch_regard=fetch_r,
                    fetch_andpro=fetch_a,
                    deliver=False,
                    use_lock=False,
                    enrich_identities=False,
                )
            self.assertEqual(result.status, PIPELINE_STATUS_SUCCESS)

    def test_n8n_called_on_all_stores_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            calls: list[dict] = []

            def capture(payload, **kwargs):
                calls.append(dict(payload))
                return {"enabled": True, "ok": True, "skipped": False}

            def boom():
                raise RuntimeError("down")

            with patch("run_pipeline.post_pipeline_webhook", side_effect=capture):
                result = run_pipeline(
                    db,
                    fetch_regard=boom,
                    fetch_andpro=boom,
                    deliver=False,
                    use_lock=False,
                    enrich_identities=False,
                )
            self.assertEqual(result.status, "failed")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["event"], "pipeline.completed")
            self.assertEqual(calls[0]["status"], "failed")
            from version import get_version

            self.assertEqual(calls[0]["app_version"], get_version())


if __name__ == "__main__":
    unittest.main()
