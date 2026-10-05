from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from buy_thailand_flow import evaluate_buy_opportunity_flow
from deal_ranking import RankedDeal
from models import Product
from run_pipeline import run_pipeline
from thailand.job_queue import pending_count
from thailand.worker import drain

REPO = Path(__file__).resolve().parents[1]
GUARDED_DIRS = tuple(REPO / name for name in ("data", "logs", "dist"))


def _guarded(path) -> Path | None:
    try:
        p = Path(os.fspath(path))
    except TypeError:
        return None
    p = (p if p.is_absolute() else Path.cwd() / p).resolve()
    for root in GUARDED_DIRS:
        if p == root or root in p.parents:
            return p
    return None


class RepoWriteGuard:
    """Record any write/delete/create under repo data/ logs/ dist/."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self._stack = ExitStack()

    def _hit(self, kind: str, path) -> None:
        p = _guarded(path)
        if p is not None:
            self.hits.append(f"{kind} {p.relative_to(REPO)}")

    def __enter__(self) -> "RepoWriteGuard":
        real_open, real_replace = os.open, os.replace
        real_unlink, real_mkdir = os.unlink, os.mkdir
        real_connect = sqlite3.connect

        def g_open(path, flags, *a, **k):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                self._hit("os.open", path)
            return real_open(path, flags, *a, **k)

        def g_replace(src, dst, *a, **k):
            self._hit("os.replace", dst)
            return real_replace(src, dst, *a, **k)

        def g_unlink(path, *a, **k):
            self._hit("os.unlink", path)
            return real_unlink(path, *a, **k)

        def g_mkdir(path, *a, **k):
            if not Path(os.fspath(path)).exists():
                self._hit("os.mkdir", path)
            return real_mkdir(path, *a, **k)

        def g_connect(database, *a, **k):
            if not (isinstance(database, str) and database.startswith(("file:", ":memory:"))):
                self._hit("sqlite3.connect", database)
            return real_connect(database, *a, **k)

        for target, fake in (
            ("os.open", g_open),
            ("os.replace", g_replace),
            ("os.unlink", g_unlink),
            ("os.mkdir", g_mkdir),
            ("sqlite3.connect", g_connect),
        ):
            self._stack.enter_context(patch(target, fake))
        return self

    def __exit__(self, *exc) -> None:
        self._stack.close()


def _products() -> tuple:
    now = datetime.now(timezone.utc)
    regard = Product(
        store="regard", external_id="1", url="https://r/1",
        name="Laptop A SKU1 RTX 5070 Ti", sku="SKU1", price=220000,
        available=True, checked_at=now,
    )
    andpro = Product(
        store="andpro", external_id="2", url="https://a/2",
        name="Laptop A SKU1 RTX 5070 Ti", sku="SKU1", price=230000,
        available=True, checked_at=now,
    )
    return (lambda: [regard]), (lambda: [andpro])


def _mature_deal() -> RankedDeal:
    return RankedDeal(
        score=79.0,
        reasons=["x"],
        offer={"store": "kns", "external_id": "1", "name": "MSI"},
        cluster_name="MSI Vector",
        gpu="RTX 5070 Ti",
        store="kns",
        price=229990,
        url="https://example/1",
        ram_gb=32,
        ssd_gb=1024,
        screen_inch=17.0,
        cpu="Intel Core Ultra 9 275HX",
        confidence=100,
        historical_min=224990,
        history_started_at="2020-01-01T00:00:00+00:00",
    )


class RuntimePathIsolationTests(unittest.TestCase):
    def test_guard_classifies_paths(self) -> None:
        self.assertIsNotNone(_guarded(REPO / "data" / "product_specs.json"))
        self.assertIsNotNone(_guarded(Path("data") / "thailand_worker_state.json"))
        self.assertIsNotNone(_guarded(REPO / "logs" / "x.log"))
        self.assertIsNone(_guarded(REPO / "tests" / "x.py"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_guarded(Path(tmp) / "data" / "product_specs.json"))
            with RepoWriteGuard() as guard:
                Path(tmp, "probe.json").write_text("{}", encoding="utf-8")
            self.assertEqual(guard.hits, [])
        guard = RepoWriteGuard()
        guard._hit("os.replace", REPO / "data" / "product_specs.json")
        self.assertEqual(guard.hits, [f"os.replace {Path('data', 'product_specs.json')}"])

    def test_pipeline_with_explicit_paths_never_writes_repo_data(self) -> None:
        fetch_r, fetch_a = _products()
        with tempfile.TemporaryDirectory() as tmp, RepoWriteGuard() as guard:
            root = Path(tmp)
            with patch("run_pipeline.post_pipeline_webhook", return_value={"enabled": False}):
                result = run_pipeline(
                    root / "t.db",
                    lock_path=root / "pipeline.lock",
                    fetch_regard=fetch_r,
                    fetch_andpro=fetch_a,
                    deliver=False,
                    use_lock=True,
                    enrich_identities=False,
                    specs_path=root / "specs.json",
                    last_run_path=root / "last_run.json",
                    buy_state_path=root / "buy_state.json",
                    thailand_jobs_dir=root / "thailand_jobs",
                )
            self.assertTrue((root / "specs.json").exists())
        self.assertEqual(result.status, "success")
        self.assertEqual(guard.hits, [])

    def test_buy_enqueue_and_worker_never_write_repo_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, RepoWriteGuard() as guard:
            root = Path(tmp)
            jobs = root / "jobs"
            out = evaluate_buy_opportunity_flow(
                russian_deals=[_mature_deal()],
                deliver=False,
                state_path=root / "buy_state.json",
                jobs_dir=jobs,
            )
            self.assertEqual(out["thailand_scan_status"], "queued")
            self.assertEqual(pending_count(jobs), 1)
            processed = drain(
                jobs_dir=jobs,
                state_path=root / "buy_state.json",
                snapshot_dir=root / "snapshots",
                deliver=False,
                scan_fn=lambda **k: {"status": "ok", "stores": []},
                worker_state_path=root / "worker_state.json",
            )
            self.assertEqual(processed, 1)
            self.assertTrue((root / "worker_state.json").exists())
        self.assertEqual(guard.hits, [])


if __name__ == "__main__":
    unittest.main()
