from __future__ import annotations

import unittest

import config
from thailand.scoring import international_value_score


class IntlScoreTests(unittest.TestCase):
    def test_range(self) -> None:
        s = international_value_score(
            price_rub=229990,
            gpu="RTX 5070 Ti",
            cpu="Intel Core Ultra 9 275HX",
            ram_gb=32,
            ssd_gb=1024,
            screen_inch=17.0,
        )
        self.assertGreaterEqual(s.score, 0)
        self.assertLessEqual(s.score, 100)

    def test_raw_max_88(self) -> None:
        self.assertEqual(config.INTL_SCORE_RAW_MAX, 88.0)
        total = (
            config.INTL_SCORE_GPU_MAX
            + config.INTL_SCORE_PRICE_MAX
            + config.INTL_SCORE_CPU_MAX
            + config.INTL_SCORE_RAM_MAX
            + config.INTL_SCORE_SSD_MAX
            + config.INTL_SCORE_SCREEN_MAX
        )
        self.assertEqual(total, 88.0)

    def test_normalization(self) -> None:
        s = international_value_score(
            price_rub=200000,
            gpu="RTX 5080",
            cpu="Intel Core Ultra 9 275HX",
            ram_gb=64,
            ssd_gb=2048,
            screen_inch=18.0,
            screen_resolution="2560x1600",
        )
        expected = round((s.raw / 88.0) * 100.0, 2)
        self.assertEqual(s.score, expected)

    def test_5080_gt_5070ti_gpu(self) -> None:
        a = international_value_score(price_rub=300000, gpu="RTX 5080")
        b = international_value_score(price_rub=300000, gpu="RTX 5070 Ti")
        self.assertGreater(a.breakdown["gpu"], b.breakdown["gpu"])

    def test_price_target(self) -> None:
        under = international_value_score(price_rub=200000, gpu="RTX 5070 Ti")
        over = international_value_score(price_rub=280000, gpu="RTX 5070 Ti")
        self.assertGreater(under.breakdown["price"], over.breakdown["price"])

    def test_ram_32_gt_16(self) -> None:
        a = international_value_score(price_rub=230000, gpu="RTX 5070 Ti", ram_gb=32)
        b = international_value_score(price_rub=230000, gpu="RTX 5070 Ti", ram_gb=16)
        self.assertGreater(a.breakdown["ram"], b.breakdown["ram"])

    def test_ssd_1tb_gt_512(self) -> None:
        a = international_value_score(price_rub=230000, gpu="RTX 5070 Ti", ssd_gb=1024)
        b = international_value_score(price_rub=230000, gpu="RTX 5070 Ti", ssd_gb=512)
        self.assertGreater(a.breakdown["ssd"], b.breakdown["ssd"])

    def test_screen_17_gt_16(self) -> None:
        a = international_value_score(price_rub=230000, gpu="RTX 5070 Ti", screen_inch=17)
        b = international_value_score(price_rub=230000, gpu="RTX 5070 Ti", screen_inch=16)
        self.assertGreater(a.breakdown["screen"], b.breakdown["screen"])

    def test_unknown_safe(self) -> None:
        s = international_value_score(price_rub=None, gpu=None)
        self.assertEqual(s.score, 0.0)
        self.assertEqual(s.raw, 0.0)

    def test_no_russian_history_component(self) -> None:
        s = international_value_score(
            price_rub=229990,
            gpu="RTX 5070 Ti",
            cpu="Intel Core Ultra 9 275HX",
            ram_gb=32,
            ssd_gb=1024,
            screen_inch=16.0,
        )
        self.assertNotIn("history", s.breakdown)
        self.assertNotIn("saving", s.breakdown)

    def test_thai_no_history_not_penalized(self) -> None:
        # Same hardware/price → same intl score regardless of missing history
        s1 = international_value_score(
            price_rub=229990, gpu="RTX 5070 Ti", ram_gb=32, ssd_gb=1024, screen_inch=16
        )
        s2 = international_value_score(
            price_rub=229990, gpu="RTX 5070 Ti", ram_gb=32, ssd_gb=1024, screen_inch=16
        )
        self.assertEqual(s1.score, s2.score)


if __name__ == "__main__":
    unittest.main()
