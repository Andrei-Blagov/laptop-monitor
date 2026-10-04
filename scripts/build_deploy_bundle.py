from __future__ import annotations

"""Build a clean production deploy bundle (allowlist only)."""

import argparse
import hashlib
import io
import tarfile
from pathlib import Path

from version import get_version

ROOT = Path(__file__).resolve().parent.parent

# Explicit production allowlist (relative to repo root).
ALLOWLIST_FILES = (
    "VERSION",
    "requirements.txt",
    "README.md",
    ".dockerignore",
    ".env.example",
    "admin_notify.py",
    "alerts.py",
    "collection.py",
    "compare.py",
    "comparison.py",
    "config.py",
    "control_bot.py",
    "deal_ranking.py",
    "deliver.py",
    "enrichment.py",
    "identity_sync.py",
    "main.py",
    "model_price_history.py",
    "models.py",
    "monitor.py",
    "notification_groups.py",
    "notifications.py",
    "pipeline_lock.py",
    "product_identity.py",
    "run_pipeline.py",
    "store_freshness.py",
    "storage.py",
    "target_gpu.py",
    "telegram_safe.py",
    "telegram_sender.py",
    "version.py",
)

ALLOWLIST_DIRS = (
    "parsers",
    "stores",
    "integrations",
    "deploy",
    "ops",
)

ALLOWLIST_SCRIPTS = (
    "scripts/__init__.py",
    "scripts/backup_db.py",
    "scripts/migrate_db.py",
    "scripts/build_deploy_bundle.py",
    "scripts/diagnose_model_price_history.py",
)

FORBIDDEN_NAME_PARTS = (
    ".git",
    ".github",
    ".venv",
    "tests",
    "scripts/windows",
    "screenshots",
    "agent-transcripts",
    "__pycache__",
    ".pytest_cache",
    ".idea",
    ".vscode",
)


def _should_skip(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    if norm.endswith(".pyc") or norm.endswith(".pyo"):
        return True
    if norm.endswith(".db") or ".db-" in norm:
        return True
    if norm == ".env" or norm.startswith(".env."):
        return True
    if norm.startswith("data/") or norm.startswith("logs/") or norm.startswith("dist/"):
        return True
    for part in FORBIDDEN_NAME_PARTS:
        if norm == part or norm.startswith(part + "/") or f"/{part}/" in f"/{norm}/":
            return True
    return False


def collect_bundle_paths(root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for rel in ALLOWLIST_FILES:
        path = root / rel
        if path.is_file():
            files.append(path)
    for rel in ALLOWLIST_SCRIPTS:
        path = root / rel
        if path.is_file():
            files.append(path)
    for dirname in ALLOWLIST_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if _should_skip(rel):
                continue
            files.append(path)
    # Deduplicate preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for path in files:
        key = path.resolve().as_posix()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def build_deploy_bundle(
    *,
    root: Path = ROOT,
    dist_dir: Path | None = None,
    version: str | None = None,
) -> tuple[Path, str]:
    ver = (version or get_version()).strip()
    out_dir = dist_dir or (root / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"laptop-monitor-{ver}.tar.gz"
    archive_path = out_dir / archive_name

    members = collect_bundle_paths(root)
    # Deterministic-ish: sort by relative posix path, fixed mtime.
    members = sorted(members, key=lambda p: p.relative_to(root).as_posix())

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for path in members:
            rel = path.relative_to(root).as_posix()
            if _should_skip(rel):
                continue
            info = tar.gettarinfo(str(path), arcname=f"laptop-monitor-{ver}/{rel}")
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = 0
            with path.open("rb") as fh:
                tar.addfile(info, fh)

    data = buf.getvalue()
    archive_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    sha_path = out_dir / f"{archive_name}.sha256"
    sha_path.write_text(f"{digest}  {archive_name}\n", encoding="utf-8")
    return archive_path, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build clean deploy bundle")
    parser.add_argument("--dist-dir", default=None)
    args = parser.parse_args(argv)
    dist = Path(args.dist_dir) if args.dist_dir else None
    path, digest = build_deploy_bundle(dist_dir=dist)
    print(f"Version: {get_version()}")
    print(f"Archive: {path}")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
