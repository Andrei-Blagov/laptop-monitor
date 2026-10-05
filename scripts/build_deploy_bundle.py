from __future__ import annotations

"""Build a clean production deploy bundle (contents from scripts.release_manifest)."""

import argparse
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path

from scripts.release_manifest import (
    MANIFEST_NAME,
    ROOT,
    bundle_relpaths,
    is_executable_path,
    is_text_path,
)
from version import get_version


def collect_bundle_paths(root: Path = ROOT) -> list[Path]:
    return [root / rel for rel in bundle_relpaths(root)]


def _payload(path: Path, rel: str) -> bytes:
    data = path.read_bytes()
    # Linux runtime / Docker build context expect LF even on Windows builders.
    if is_text_path(rel):
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def _build_mtime() -> int:
    # Real (non-zero, per-build) mtimes: BuildKit context sync keys files by
    # (path, size, mtime); constant mtimes let it reuse stale content.
    raw = (os.environ.get("SOURCE_DATE_EPOCH") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else int(time.time())


def _add_bytes(tar: tarfile.TarFile, arcname: str, payload: bytes, *, mode: int, mtime: int) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(payload)
    info.mode = mode
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(payload))


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
    prefix = f"laptop-monitor-{ver}"
    mtime = _build_mtime()

    payloads = {rel: _payload(root / rel, rel) for rel in bundle_relpaths(root)}
    manifest = {
        "version": ver,
        "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in payloads.items()},
    }
    payloads[MANIFEST_NAME] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for rel in sorted(payloads):
            mode = 0o755 if is_executable_path(rel) else 0o644
            _add_bytes(tar, f"{prefix}/{rel}", payloads[rel], mode=mode, mtime=mtime)

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
    print(f"Verify:  python -m scripts.release_verify --bundle {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
