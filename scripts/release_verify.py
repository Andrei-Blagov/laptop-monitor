from __future__ import annotations

"""Verify a release artifact before deploy (stdlib only — runs on the VPS host too).

    python -m scripts.release_verify --bundle dist/laptop-monitor-X.Y.Z.tar.gz
    python -m scripts.release_verify --tree /opt/laptop-monitor
    python -m scripts.release_verify --image laptop-monitor:X.Y.Z
"""

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from scripts.release_manifest import (
    MANIFEST_NAME,
    REQUIRED_FILES,
    ROOT,
    RUNTIME_ENTRYPOINTS,
    is_excluded,
    is_executable_path,
    is_image_path,
    is_text_path,
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Before 2001-09-09: zero / epoch-ish mtimes broke BuildKit context sync.
MIN_PLAUSIBLE_MTIME = 1_000_000_000
SECRET_PATTERNS = (
    ("telegram bot token", re.compile(rb"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b")),
    ("private key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "secret assignment",
        re.compile(
            rb"(?m)^\s*(?:export\s+)?(?:TELEGRAM_BOT_TOKEN|N8N_WEBHOOK_SECRET)=[^\s#]{8,}"
        ),
    ),
)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def merge(self, other: "Report") -> None:
        self.errors.extend(other.errors)
        self.info.extend(other.info)


# --- import graph ---------------------------------------------------------


def _module_file(base: Path, dotted: str) -> Path | None:
    path = base.joinpath(*dotted.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _is_package(base: Path, dotted: str) -> bool:
    return base.joinpath(*dotted.split(".")).is_dir()


def _repo_first_party(repo_root: Path | None) -> set[str]:
    if repo_root is None or not repo_root.is_dir():
        return set()
    names = {p.stem for p in repo_root.glob("*.py")}
    names |= {
        p.name
        for p in repo_root.iterdir()
        if p.is_dir() and not is_excluded(p.name) and any(p.glob("*.py"))
    }
    return names


def _imports_of(path: Path, module: str) -> Iterable[tuple[str, bool]]:
    """Yield (dotted name, is_definitely_a_module)."""
    tree = ast.parse(path.read_bytes(), filename=str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, True
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base_mod = ".".join(parts + ([node.module] if node.module else []))
            else:
                base_mod = node.module or ""
            if not base_mod:
                continue
            yield base_mod, True
            for alias in node.names:
                if alias.name != "*":
                    # `from pkg import name` — name may be a submodule or a symbol.
                    yield f"{base_mod}.{alias.name}", False


def first_party_closure(
    base: Path,
    *,
    roots: Iterable[str],
    repo_root: Path | None = ROOT,
) -> tuple[set[str], list[str]]:
    """
    Walk first-party imports (including function-local ones) from roots.

    Returns (relative posix paths of reached module files, missing modules).
    A module is missing if it is first-party (present in base or in the repo
    checkout) but cannot be resolved inside base.
    """
    known = _repo_first_party(repo_root) | _repo_first_party(base)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    seen: set[str] = set()
    files: set[str] = set()
    missing: set[str] = set()
    queue = list(roots)
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_file(base, module)
        if path is None:
            if module.split(".", 1)[0] in known and not _is_package(base, module):
                missing.add(module)
            continue
        files.add(path.relative_to(base).as_posix())
        for name, is_module in _imports_of(path, module):
            top = name.split(".", 1)[0]
            if top in stdlib or top not in known:
                continue
            if _module_file(base, name) is not None:
                queue.append(name)
            elif _is_package(base, name):
                continue
            elif is_module:
                missing.add(name)
    return files, sorted(missing)


def check_imports(base: Path, modules: Iterable[str], *, python: str = sys.executable) -> Report:
    report = Report()
    mods = list(modules)
    code = (
        "import importlib, sys\n"
        "failed = []\n"
        f"for m in {mods!r}:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as exc:\n"
        "        failed.append(f'{m}: {type(exc).__name__}: {exc}')\n"
        "print('\\n'.join(failed))\n"
        "sys.exit(1 if failed else 0)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [python, "-B", "-c", code],
        cwd=str(base),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        detail = (proc.stdout.strip() or proc.stderr.strip()[-800:]) or f"exit {proc.returncode}"
        report.error(f"entrypoint import failed: {detail}")
    else:
        report.info.append(f"importable: {', '.join(mods)}")
    return report


# --- bundle -----------------------------------------------------------------


def _check_member_bytes(rel: str, data: bytes, report: Report) -> None:
    if is_text_path(rel) and b"\r" in data:
        report.error(f"CR line endings: {rel}")
    if rel.endswith(".example"):
        return
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(data):
            report.error(f"possible secret ({label}): {rel}")


def verify_bundle(
    bundle: Path,
    *,
    expected_version: str | None = None,
    import_check: bool = True,
    repo_root: Path | None = ROOT,
) -> Report:
    report = Report()
    try:
        tar = tarfile.open(bundle, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        report.error(f"cannot open bundle {bundle}: {type(exc).__name__}: {exc}")
        return report

    sha_file = bundle.with_name(bundle.name + ".sha256")
    if sha_file.exists():
        expected_sha = sha_file.read_text(encoding="utf-8").split()[0]
        actual_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
        if expected_sha != actual_sha:
            report.error(f"sha256 mismatch: file={actual_sha} expected={expected_sha}")

    with tar, tempfile.TemporaryDirectory(prefix="release_verify_") as tmp:
        members = tar.getmembers()
        files = [m for m in members if m.isfile()]
        prefixes = {m.name.split("/", 1)[0] for m in members}
        if len(prefixes) != 1:
            report.error(f"bundle must have one top-level directory, got {sorted(prefixes)}")
            return report
        prefix = prefixes.pop()
        rels: dict[str, tarfile.TarInfo] = {}
        unsafe = {
            m.name
            for m in members
            if m.issym() or m.islnk() or m.name.startswith("/") or ".." in m.name.split("/")
        }
        for name in sorted(unsafe):
            report.error(f"unsafe member: {name}")
        for m in files:
            if m.name in unsafe:
                continue
            rel = m.name.split("/", 1)[1] if "/" in m.name else ""
            rels[rel] = m
            if is_excluded(rel):
                report.error(f"forbidden file in bundle: {rel}")
            if m.mtime < MIN_PLAUSIBLE_MTIME:
                report.error(f"implausible mtime {m.mtime}: {rel}")
            if is_executable_path(rel) and not (m.mode & 0o111):
                report.error(f"not executable: {rel}")
            fh = tar.extractfile(m)
            data = fh.read() if fh else b""
            _check_member_bytes(rel, data, report)
            dest = Path(tmp, *rel.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        for rel in REQUIRED_FILES:
            if rel not in rels:
                report.error(f"required file missing: {rel}")

        version_file = Path(tmp, "VERSION")
        version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
        if not SEMVER_RE.match(version):
            report.error(f"VERSION invalid: {version!r}")
        if prefix != f"laptop-monitor-{version}":
            report.error(f"top-level dir {prefix!r} does not match VERSION {version!r}")
        if bundle.name != f"laptop-monitor-{version}.tar.gz":
            report.error(f"archive name {bundle.name!r} does not match VERSION {version!r}")
        if expected_version and version != expected_version:
            report.error(f"VERSION {version!r} != expected {expected_version!r}")
        report.info.append(f"version: {version}")
        report.info.append(f"files: {len(rels)}")

        report.merge(verify_tree(Path(tmp), expect_image_subset=False))
        manifest_files = _load_manifest(Path(tmp), Report()) or {}
        for rel in sorted(set(rels) - set(manifest_files) - {MANIFEST_NAME}):
            report.error(f"bundle file not listed in {MANIFEST_NAME}: {rel}")

        _files, missing = first_party_closure(
            Path(tmp),
            roots=list(RUNTIME_ENTRYPOINTS)
            + [_module_name(rel) for rel in rels if rel.endswith(".py")],
            repo_root=repo_root,
        )
        for name in missing:
            report.error(f"first-party module imported but not in bundle: {name}")

        if import_check:
            report.merge(check_imports(Path(tmp), RUNTIME_ENTRYPOINTS))
    return report


def _module_name(rel: str) -> str:
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


# --- tree (extracted bundle on host / build context) --------------------------


def _load_manifest(base: Path, report: Report) -> dict[str, str] | None:
    path = base / MANIFEST_NAME
    if not path.is_file():
        report.error(f"{MANIFEST_NAME} missing in {base}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data["files"]
        if not isinstance(files, dict):
            raise TypeError("files")
        return {str(k): str(v) for k, v in files.items()}
    except (ValueError, KeyError, TypeError) as exc:
        report.error(f"{MANIFEST_NAME} unreadable: {type(exc).__name__}")
        return None


def verify_tree(base: Path, *, expect_image_subset: bool = True) -> Report:
    """
    Compare image-relevant files under base with RELEASE_MANIFEST.json.

    expect_image_subset=True: base is a deploy dir / build context; only files
    that COPY . would put in the image are compared, extra ones are stale.
    """
    report = Report()
    files = _load_manifest(base, report)
    if files is None:
        return report
    wanted = {rel: sha for rel, sha in files.items() if not expect_image_subset or is_image_path(rel)}
    for rel, sha in sorted(wanted.items()):
        path = base / rel
        if not path.is_file():
            report.error(f"missing vs manifest: {rel}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            report.error(f"content differs from manifest: {rel}")
    if expect_image_subset:
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            if rel == MANIFEST_NAME or not is_image_path(rel):
                continue
            if rel not in files:
                report.error(f"stale file not in release manifest: {rel}")
    report.info.append(f"manifest files checked: {len(wanted)}")
    return report


# --- image --------------------------------------------------------------------

_IMAGE_SCRIPT = r"""
import hashlib, importlib, json, pathlib, sys
out = {"version": pathlib.Path("VERSION").read_text().strip(), "import_errors": []}
for m in sys.argv[1].split(","):
    try:
        importlib.import_module(m)
    except Exception as exc:
        out["import_errors"].append(f"{m}: {type(exc).__name__}: {exc}")
man = pathlib.Path("RELEASE_MANIFEST.json")
out["manifest"] = json.loads(man.read_text())["files"] if man.exists() else None
out["hashes"] = {
    p.relative_to(".").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in pathlib.Path(".").rglob("*")
    if p.is_file() and "__pycache__" not in p.parts and p.parts[0] not in ("data", "logs")
}
print(json.dumps(out))
"""


def verify_image(
    image: str,
    *,
    expected_version: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Report:
    report = Report()
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "python",
        image,
        "-B",
        "-c",
        _IMAGE_SCRIPT,
        ",".join(RUNTIME_ENTRYPOINTS),
    ]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        report.error(f"docker run failed: {type(exc).__name__}: {exc}")
        return report
    if proc.returncode != 0:
        report.error(f"docker run exit {proc.returncode}: {(proc.stderr or '').strip()[-800:]}")
        return report
    try:
        data = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        report.error("image verify produced no JSON")
        return report

    version = str(data.get("version") or "")
    report.info.append(f"image version: {version}")
    if not SEMVER_RE.match(version):
        report.error(f"image VERSION invalid: {version!r}")
    if expected_version and version != expected_version:
        report.error(f"image VERSION {version!r} != expected {expected_version!r}")
    if ":" in image and image.rsplit(":", 1)[1] not in (version, "latest"):
        report.error(f"image tag {image!r} does not match VERSION {version!r}")
    for err in data.get("import_errors") or []:
        report.error(f"image import failed: {err}")

    manifest = data.get("manifest")
    hashes = data.get("hashes") or {}
    if manifest is None:
        report.error(f"{MANIFEST_NAME} missing in image (not built from a release bundle?)")
        return report
    for rel, sha in sorted(manifest.items()):
        if not is_image_path(rel):
            continue
        if rel not in hashes:
            report.error(f"image missing file: {rel}")
        elif hashes[rel] != sha:
            report.error(f"image file differs from manifest (stale build?): {rel}")
    for rel in sorted(hashes):
        if rel != MANIFEST_NAME and is_image_path(rel) and rel not in manifest:
            report.error(f"stale file in image not in release manifest: {rel}")
    report.info.append(f"image files checked: {sum(1 for r in manifest if is_image_path(r))}")
    return report


# --- CLI ----------------------------------------------------------------------


def _print(report: Report, title: str) -> None:
    print(f"== {title}: {'OK' if report.ok else 'FAILED'}")
    for line in report.info:
        print(f"   {line}")
    for line in report.errors:
        print(f"   ERROR {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify laptop-monitor release artifacts")
    parser.add_argument("--bundle", type=Path, help="dist/laptop-monitor-X.Y.Z.tar.gz")
    parser.add_argument("--tree", type=Path, help="extracted deploy dir / build context")
    parser.add_argument("--image", help="built image, e.g. laptop-monitor:X.Y.Z")
    parser.add_argument("--expect-version", default=None)
    parser.add_argument(
        "--no-import-check",
        action="store_true",
        help="skip importing entrypoints (host without third-party deps)",
    )
    args = parser.parse_args(argv)
    if not (args.bundle or args.tree or args.image):
        parser.error("give at least one of --bundle / --tree / --image")

    ok = True
    if args.bundle:
        rep = verify_bundle(
            args.bundle,
            expected_version=args.expect_version,
            import_check=not args.no_import_check,
        )
        _print(rep, f"bundle {args.bundle}")
        ok &= rep.ok
    if args.tree:
        rep = verify_tree(args.tree)
        _print(rep, f"tree {args.tree}")
        ok &= rep.ok
    if args.image:
        rep = verify_image(args.image, expected_version=args.expect_version)
        _print(rep, f"image {args.image}")
        ok &= rep.ok
    print("RELEASE VERIFY: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
