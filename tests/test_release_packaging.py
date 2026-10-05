from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.build_deploy_bundle import build_deploy_bundle
from scripts.release_manifest import (
    IMAGE_ALLOW_PATTERNS,
    MANIFEST_NAME,
    RUNTIME_ENTRYPOINTS,
    SYSTEMD_UNITS,
    bundle_relpaths,
    is_image_path,
)
from scripts.release_verify import (
    first_party_closure,
    verify_bundle,
    verify_image,
    verify_tree,
)
from version import get_version

ROOT = Path(__file__).resolve().parent.parent


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tar_bundle(path: Path, files: dict[str, tuple[bytes, int, int]], version: str = "9.9.9") -> Path:
    """files: rel -> (payload, mode, mtime)."""
    with tarfile.open(path, "w:gz") as tar:
        for rel, (payload, mode, mtime) in files.items():
            info = tarfile.TarInfo(f"laptop-monitor-{version}/{rel}")
            info.size = len(payload)
            info.mode = mode
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(payload))
    return path


class RuntimeImportGraphTests(unittest.TestCase):
    def test_runtime_import_closure_is_in_bundle(self) -> None:
        """Every first-party module reachable from production entrypoints ships."""
        reached, missing = first_party_closure(ROOT, roots=RUNTIME_ENTRYPOINTS, repo_root=ROOT)
        self.assertEqual(missing, [])
        bundle = set(bundle_relpaths(ROOT))
        not_shipped = sorted(reached - bundle)
        self.assertEqual(
            not_shipped,
            [],
            "runtime modules missing from release bundle "
            "(new package? add to scripts/release_manifest.py; new file? git add it)",
        )
        self.assertIn("thailand/worker.py", reached)
        self.assertIn("run_pipeline.py", reached)

    def test_unshipped_package_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "VERSION", "9.9.9\n")
            _write(root, "run_pipeline.py", "def main():\n    from newpkg.mod import run\n")
            _write(root, "newpkg/__init__.py", "")
            _write(root, "newpkg/mod.py", "def run():\n    pass\n")
            archive, _ = build_deploy_bundle(root=root, dist_dir=root / "dist", version="9.9.9")
            report = verify_bundle(archive, import_check=False, repo_root=root)
        self.assertIn(
            "first-party module imported but not in bundle: newpkg.mod", report.errors
        )

    def test_missing_submodule_of_shipped_package_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "thailand/__init__.py", "")
            _write(root, "run_pipeline.py", "import thailand.gone\n")
            _reached, missing = first_party_closure(root, roots=["run_pipeline"], repo_root=root)
        self.assertEqual(missing, ["thailand.gone"])


class BundleVerifyTests(unittest.TestCase):
    def test_repo_bundle_passes_full_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive, _ = build_deploy_bundle(dist_dir=Path(tmp))
            report = verify_bundle(archive, expected_version=get_version())
            with tarfile.open(archive, "r:gz") as tar:
                members = {m.name.split("/", 1)[1]: m for m in tar.getmembers() if m.isfile()}
                manifest = json.loads(tar.extractfile(members[MANIFEST_NAME]).read())
        self.assertEqual(report.errors, [])
        self.assertEqual(manifest["version"], get_version())
        self.assertEqual(set(manifest["files"]), set(members) - {MANIFEST_NAME})
        self.assertEqual(members["deploy/compose.sh"].mode & 0o777, 0o755)
        self.assertGreater(members["VERSION"].mtime, 1_000_000_000)
        self.assertFalse(any(rel.startswith("tests/") for rel in members))

    def test_broken_bundle_reports_each_problem(self) -> None:
        good_mtime = 1_800_000_000
        files = {
            "VERSION": (b"9.9.9\n", 0o644, good_mtime),
            "run_pipeline.py": (b"x = 1\r\n", 0o644, good_mtime),
            "deploy/compose.sh": (b"#!/bin/sh\n", 0o644, good_mtime),
            "control_bot.py": (b"x = 1\n", 0o644, 0),
            ".env": (b"TELEGRAM_BOT_TOKEN=x\n", 0o600, good_mtime),
            "data/laptop_monitor.db": (b"sqlite", 0o644, good_mtime),
            "tests/test_x.py": (b"", 0o644, good_mtime),
            "config.py": (b'T = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"\n', 0o644, good_mtime),
        }
        with tempfile.TemporaryDirectory() as tmp:
            archive = _tar_bundle(Path(tmp) / "laptop-monitor-9.9.9.tar.gz", files)
            report = verify_bundle(archive, import_check=False, repo_root=None)
        joined = "\n".join(report.errors)
        for expected in (
            "CR line endings: run_pipeline.py",
            "not executable: deploy/compose.sh",
            "implausible mtime 0: control_bot.py",
            "forbidden file in bundle: .env",
            "forbidden file in bundle: data/laptop_monitor.db",
            "forbidden file in bundle: tests/test_x.py",
            "possible secret (telegram bot token): config.py",
            "required file missing: thailand/worker.py",
            f"{MANIFEST_NAME} missing",
        ):
            self.assertIn(expected, joined)

    def test_version_mismatch_and_unopenable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = _tar_bundle(
                Path(tmp) / "laptop-monitor-9.9.9.tar.gz",
                {"VERSION": (b"9.9.8\n", 0o644, 1_800_000_000)},
            )
            report = verify_bundle(archive, import_check=False, repo_root=None)
            junk = Path(tmp) / "junk.tar.gz"
            junk.write_bytes(b"not a tar")
            junk_report = verify_bundle(junk, import_check=False, repo_root=None)
        self.assertTrue(any("does not match VERSION" in e for e in report.errors))
        self.assertFalse(junk_report.ok)


class TreeVerifyTests(unittest.TestCase):
    def test_stale_and_modified_files_in_build_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive, _ = build_deploy_bundle(dist_dir=Path(tmp) / "dist")
            ctx = Path(tmp) / "ctx"
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(ctx, filter="data")
            tree = next(ctx.iterdir())
            self.assertEqual(verify_tree(tree).errors, [])

            (tree / "main_old.py").write_text("x = 1\n", encoding="utf-8")
            (tree / "config.py").write_text("changed = True\n", encoding="utf-8")
            (tree / "data").mkdir()
            (tree / "data" / "scratch.py").write_text("", encoding="utf-8")
            errors = verify_tree(tree).errors
        self.assertIn("stale file not in release manifest: main_old.py", errors)
        self.assertIn("content differs from manifest: config.py", errors)
        self.assertFalse(any("data/" in e for e in errors))


class ImageVerifyTests(unittest.TestCase):
    def _runner(self, payload: dict, returncode: int = 0):
        calls: list[list[str]] = []

        def run(cmd, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, json.dumps(payload) + "\n", "")

        return run, calls

    def test_image_matching_manifest(self) -> None:
        files = {"VERSION": "a", "run_pipeline.py": "b", "deploy/Dockerfile": "c"}
        runner, calls = self._runner(
            {
                "version": "1.2.3",
                "import_errors": [],
                "manifest": files,
                "hashes": {"VERSION": "a", "run_pipeline.py": "b", MANIFEST_NAME: "z"},
            }
        )
        report = verify_image("laptop-monitor:1.2.3", runner=runner)
        self.assertEqual(report.errors, [])
        self.assertIn("--network", calls[0])
        self.assertIn(",".join(RUNTIME_ENTRYPOINTS), calls[0])

    def test_image_stale_content_version_and_imports(self) -> None:
        runner, _ = self._runner(
            {
                "version": "1.2.2",
                "import_errors": ["thailand.worker: ModuleNotFoundError: No module named 'thailand'"],
                "manifest": {"VERSION": "a", "config.py": "b"},
                "hashes": {"VERSION": "a", "config.py": "OLD", "main.py": "m"},
            }
        )
        errors = verify_image("laptop-monitor:1.2.3", expected_version="1.2.3", runner=runner).errors
        joined = "\n".join(errors)
        self.assertIn("image file differs from manifest (stale build?): config.py", joined)
        self.assertIn("stale file in image not in release manifest: main.py", joined)
        self.assertIn("image import failed: thailand.worker", joined)
        self.assertIn("!= expected '1.2.3'", joined)

    def test_image_without_manifest_fails(self) -> None:
        runner, _ = self._runner({"version": "1.2.3", "import_errors": [], "manifest": None})
        self.assertFalse(verify_image("laptop-monitor:1.2.3", runner=runner).ok)


class DockerContextTests(unittest.TestCase):
    def test_dockerignore_whitelist_matches_manifest(self) -> None:
        lines = [
            ln.strip()
            for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        self.assertEqual(lines[0], "*")
        allowed = {ln[1:] for ln in lines if ln.startswith("!")}
        self.assertEqual(allowed, set(IMAGE_ALLOW_PATTERNS))
        for secret in (".env", "data/laptop_monitor.db", "backups/x/laptop_monitor.db", "tests/t.py"):
            self.assertFalse(is_image_path(secret), secret)

    def test_dockerfile_has_no_per_module_copy(self) -> None:
        copies = [
            ln.strip()
            for ln in (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8").splitlines()
            if ln.strip().upper().startswith("COPY")
        ]
        self.assertEqual(copies, ["COPY requirements.txt .", "COPY . ./"])

    def test_systemd_units_listed_in_manifest(self) -> None:
        actual = {p.name for p in (ROOT / "deploy" / "systemd").iterdir() if p.is_file()}
        self.assertEqual(actual, set(SYSTEMD_UNITS))


class ComposeBuildTrapTests(unittest.TestCase):
    def test_profiled_build_service_is_built_by_wrapper(self) -> None:
        compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
        services = re.split(r"(?m)^  (?=[a-z][\w-]*:\s*$)", compose.split("services:", 1)[1])
        built = [s for s in services if re.search(r"(?m)^    build:", s)]
        self.assertEqual(len(built), 1)
        self.assertTrue(built[0].startswith("pipeline:"))
        self.assertIn('profiles: ["manual"]', built[0])

        wrapper = (ROOT / "deploy" / "compose.sh").read_text(encoding="utf-8")
        self.assertRegex(wrapper, r"build \| build-image\)")
        self.assertIn("compose --profile manual build", wrapper)

    def test_docs_use_canonical_build_command(self) -> None:
        for doc in ("README.md", "deploy/README.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            self.assertIn("./deploy/compose.sh build-image", text, doc)
            self.assertNotRegex(text, r"(?m)^\s*docker compose\b[^\n]*\bbuild\b", doc)


if __name__ == "__main__":
    unittest.main()
