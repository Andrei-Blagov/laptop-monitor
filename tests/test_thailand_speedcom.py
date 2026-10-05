from __future__ import annotations

import unittest
from datetime import datetime, timezone

from thailand.eligibility import offer_user_facing_eligible, price_within_cap
from thailand.fx import FxRate
from thailand.models import ThailandOffer
from thailand.speedcom import parse_speedcom_product
from thailand.verification import is_verified_for_ranking


def _variant(
    vid: int,
    price: str,
    *,
    title: str = "Default Title",
    sku: str = "ASUS-G614PR-TS113W",
    available: bool = True,
    compare: str | None = None,
) -> dict:
    return {
        "id": vid,
        "product_id": 100,
        "title": title,
        "option1": title,
        "sku": sku,
        "price": price,
        "compare_at_price": compare,
        "available": available,
    }


def _product(
    *,
    title: str,
    body: str,
    variants: list[dict],
    tags: list[str] | None = None,
    product_type: str = "NOTEBOOK",
    handle: str = "rog-g614pr",
) -> dict:
    return {
        "id": 100,
        "title": title,
        "handle": handle,
        "vendor": "SpeedCom",
        "product_type": product_type,
        "tags": tags or [],
        "body_html": body,
        "variants": variants,
    }


def _fx() -> FxRate:
    return FxRate(
        source="test",
        currency="THB",
        nominal=1,
        official_rate=1.0,
        rub_per_thb=1.0,
        published_date=None,
        fetched_at=datetime.now(timezone.utc),
    )


_BODY_5070 = (
    "<p>Graphics: NVIDIA GeForce RTX 5070 Ti Laptop GPU 12GB GDDR7 "
    "RAM: 16GB DDR5 SSD: 1TB ขนาดหน้าจอ: 16.0 inch 2560 x 1600</p>"
)
_TITLE = "โน๊ตบุ๊ค ASUS ROG Strix G16 G614PR-TS113W"


class SpeedComParseTests(unittest.TestCase):
    def test_shopify_product_json_parse(self) -> None:
        offers = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00")])
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].store, "speedcom")

    def test_rtx_5070_ti(self) -> None:
        offer = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00")])
        )[0]
        self.assertEqual(offer.gpu, "RTX 5070 Ti")

    def test_rtx_5080(self) -> None:
        body = _BODY_5070.replace("5070 Ti", "5080")
        offer = parse_speedcom_product(
            _product(title=_TITLE, body=body, variants=[_variant(11, "119990.00")])
        )[0]
        self.assertEqual(offer.gpu, "RTX 5080")

    def test_exclude_5070_non_ti(self) -> None:
        body = "<p>Graphics: NVIDIA GeForce RTX 5070 Laptop GPU 8GB GDDR7</p>"
        self.assertEqual(parse_speedcom_product(_product(title=_TITLE, body=body, variants=[_variant(11, "70000.00")])), [])

    def test_exclude_5060(self) -> None:
        body = "<p>Graphics: NVIDIA GeForce RTX 5060 Laptop GPU 8GB</p>"
        self.assertEqual(parse_speedcom_product(_product(title=_TITLE, body=body, variants=[_variant(11, "50000.00")])), [])

    def test_variant_price_mapping(self) -> None:
        offers = parse_speedcom_product(
            _product(
                title=_TITLE,
                body=_BODY_5070,
                variants=[
                    _variant(1, "91000.00", title="Black"),
                    _variant(2, "89990.00", title="Gray"),
                ],
            )
        )
        by_variant = {o.variant_id: o.price_thb for o in offers}
        self.assertEqual(by_variant, {"1": 91000, "2": 89990})

    def test_wrong_cheap_variant_not_used_for_target_gpu(self) -> None:
        offers = parse_speedcom_product(
            _product(
                title="ASUS ROG Strix G16",
                body="<p>Several configurations</p>",
                variants=[
                    _variant(1, "45000.00", title="RTX 5060 16GB", sku="CHEAP"),
                    _variant(2, "99000.00", title="RTX 5070 Ti 32GB", sku="TARGET"),
                ],
            )
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].gpu, "RTX 5070 Ti")
        self.assertEqual(offers[0].price_thb, 99000)
        self.assertEqual(offers[0].sku, "TARGET")

    def test_sku_and_product_ids(self) -> None:
        offer = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00")])
        )[0]
        self.assertEqual(offer.sku, "ASUS-G614PR-TS113W")
        self.assertEqual(offer.manufacturer_part_number, "G614PR-TS113W")
        self.assertEqual(offer.product_id, "100")
        self.assertEqual(offer.variant_id, "11")

    def test_availability(self) -> None:
        in_stock = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00", available=True)])
        )[0]
        out = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00", available=False)])
        )[0]
        self.assertEqual(in_stock.availability_status, "in_stock")
        self.assertTrue(is_verified_for_ranking(in_stock))
        self.assertEqual(out.availability_status, "out_of_stock")
        self.assertFalse(is_verified_for_ranking(out))

    def test_public_sale_price(self) -> None:
        offer = parse_speedcom_product(
            _product(
                title=_TITLE,
                body=_BODY_5070,
                variants=[_variant(11, "89990.00", compare="99990.00")],
            )
        )[0]
        self.assertEqual(offer.price_thb, 89990)
        self.assertEqual(offer.regular_price_thb, 99990)

    def test_specs(self) -> None:
        offer = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00")])
        )[0]
        self.assertEqual(offer.ram_gb, 16)
        self.assertEqual(offer.ssd_gb, 1024)
        self.assertEqual(offer.screen_size_inch, 16.0)

    def test_cap_boundaries(self) -> None:
        self.assertTrue(price_within_cap(299_999))
        self.assertTrue(price_within_cap(300_000))
        self.assertFalse(price_within_cap(300_001))
        inside = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "300000.00")])
        )[0]
        outside = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "300001.00")])
        )[0]
        ok_in, _, _ = offer_user_facing_eligible(inside, fx=_fx())
        ok_out, reason, _ = offer_user_facing_eligible(outside, fx=_fx())
        self.assertTrue(ok_in)
        self.assertFalse(ok_out)
        self.assertEqual(reason, "price_above_300000_rub")

    def test_detail_url(self) -> None:
        offer = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00")], handle="rog-g614pr")
        )[0]
        self.assertIn("https://speedcom.co.th/products/rog-g614pr", offer.url)
        self.assertIn("variant=11", offer.url)

    def test_explicit_gpu_line_beats_conflicting_tag(self) -> None:
        offers = parse_speedcom_product(
            _product(
                title="โน๊ตบุ๊ค Acer Predator Helios Neo16S AI PHN16S-71-76AZ",
                body="<p>GPU: NVIDIA GeForce RTX 5050 Laptop 8GB GDDR6 RAM: 16GB DDR5 SSD: 1TB</p>",
                tags=["GeForce RTX 5070Ti", "โน๊ตบุ๊ค"],
                variants=[_variant(11, "47990.00", sku="ACER-PHN16S-71-76AZ")],
            )
        )
        self.assertEqual(offers, [])

    def test_desktop_card_excluded(self) -> None:
        offers = parse_speedcom_product(
            _product(
                title="การ์ดจอ ASUS RTX 5070 Ti",
                body=_BODY_5070,
                variants=[_variant(11, "30000.00")],
                product_type="VGA",
            )
        )
        self.assertEqual(offers, [])


class SpeedComEligibilityTypeTests(unittest.TestCase):
    def test_offer_type(self) -> None:
        offer = parse_speedcom_product(
            _product(title=_TITLE, body=_BODY_5070, variants=[_variant(11, "89990.00")])
        )[0]
        self.assertIsInstance(offer, ThailandOffer)


if __name__ == "__main__":
    unittest.main()
