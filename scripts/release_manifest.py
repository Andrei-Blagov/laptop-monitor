from __future__ import annotations

"""Single source of truth for production release bundle contents.

Used by scripts.build_deploy_bundle (what goes into the bundle) and
scripts.release_verify (what a valid bundle / image must contain).
The Docker image is built from the extracted bundle; deploy/.dockerignore-style
whitelist in the repo root `.dockerignore` must match IMAGE_ALLOW_PATTERNS.
"""

import fnmatch
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MANIFEST_NAME = "RELEASE_MANIFEST.json"

# Top-level non-Python files shipped in the bundle.
ROOT_FILES = (
    "VERSION",
    "requirements.txt",
    "README.md",
    "CHANGELOG.md",
    ".gitattributes",
    ".dockerignore",
    ".env.example",
)

# Python packages shipped whole (minus exclusions).
RUNTIME_PACKAGES = (
    "parsers",
    "stores",
    "integrations",
    "thailand",
    "ops",
)

# Non-runtime directories shipped whole (deploy configs, systemd, n8n).
DEPLOY_DIRS = ("deploy",)

# Operational scripts shipped in the bundle (scripts/ is not shipped whole).
SCRIPTS = (
    "scripts/__init__.py",
    "scripts/backup_db.py",
    "scripts/migrate_db.py",
    "scripts/build_deploy_bundle.py",
    "scripts/release_manifest.py",
    "scripts/release_verify.py",
    "scripts/diagnose_model_price_history.py",
    "scripts/diagnose_price_history_chart.py",
    "scripts/diagnose_spec_coverage.py",
    "scripts/diagnose_thailand_async.py",
    "scripts/diagnose_thailand_market_v2.py",
    "scripts/diagnose_thailand_corrective.py",
    "scripts/diagnose_thailand.py",
)

# Modules that production actually starts (compose, systemd, control bot).
RUNTIME_ENTRYPOINTS = (
    "run_pipeline",
    "control_bot",
    "thailand.worker",
    "scripts.backup_db",
    "scripts.migrate_db",
    "ops.n8n_watchdog",
)

SYSTEMD_UNITS = (
    "laptop-monitor.service",
    "laptop-monitor.timer",
    "laptop-monitor-control.service",
    "laptop-monitor-backup.service",
    "laptop-monitor-backup.timer",
    "laptop-monitor-thailand.path",
    "laptop-monitor-thailand.service",
    "n8n-watchdog.service",
    "n8n-watchdog.timer",
)

REQUIRED_FILES = (
    "VERSION",
    "requirements.txt",
    ".dockerignore",
    "run_pipeline.py",
    "control_bot.py",
    "thailand/worker.py",
    "scripts/backup_db.py",
    "scripts/migrate_db.py",
    "ops/n8n_watchdog.py",
    "deploy/Dockerfile",
    "deploy/docker-compose.yml",
    "deploy/compose.sh",
) + tuple(f"deploy/systemd/{unit}" for unit in SYSTEMD_UNITS)

# Paths (relative, posix) that enter the Docker image via COPY . — the root
# `.dockerignore` whitelist must allow exactly these.
IMAGE_ALLOW_PATTERNS = (
    "VERSION",
    "requirements.txt",
    MANIFEST_NAME,
    "*.py",
) + RUNTIME_PACKAGES + ("scripts",)

# Files that must use LF and are checked for CR bytes.
LF_SUFFIXES = frozenset(
    {
        ".sh",
        ".service",
        ".timer",
        ".path",
        ".py",
        ".md",
        ".txt",
        ".yml",
        ".yaml",
        ".json",
        ".example",
    }
)
LF_NAMES = frozenset({"VERSION", ".dockerignore", ".gitattributes"})

_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".github",
        ".venv",
        "venv",
        "tests",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "screenshots",
        "agent-transcripts",
    }
)
_EXCLUDED_TOP = frozenset({"data", "logs", "dist", "backups"})
_EXCLUDED_PATTERNS = (
    "*.pyc",
    "*.pyo",
    "*.db",
    "*.db-*",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "*.pem",
    "*.key",
)


def is_text_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in LF_NAMES or Path(name).suffix in LF_SUFFIXES


def is_executable_path(rel: str) -> bool:
    return rel.endswith(".sh")


def is_excluded(rel: str) -> bool:
    """True if a relative posix path must never appear in a release bundle."""
    norm = rel.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    parts = norm.split("/")
    if parts[0] in _EXCLUDED_TOP:
        return True
    if any(part in _EXCLUDED_PARTS for part in parts):
        return True
    if norm.startswith("scripts/windows/") or norm == "scripts/windows":
        return True
    name = parts[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in _EXCLUDED_PATTERNS)


def _git_candidates(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [name for name in proc.stdout.decode("utf-8").split("\0") if name]


def _walk_candidates(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [
            d
            for d in dirnames
            if not is_excluded(d if rel_dir == "." else f"{rel_dir}/{d}")
        ]
        for filename in filenames:
            out.append(filename if rel_dir == "." else f"{rel_dir}/{filename}")
    return out


def candidate_files(root: Path = ROOT) -> list[str]:
    """Tracked files (git index) or, outside a git checkout, the file tree."""
    names = _git_candidates(root)
    return names if names is not None else _walk_candidates(root)


def bundle_relpaths(root: Path = ROOT) -> list[str]:
    """Sorted relative posix paths that make up the release bundle."""
    selected: set[str] = set()
    scripts = set(SCRIPTS)
    root_files = set(ROOT_FILES)
    dirs = RUNTIME_PACKAGES + DEPLOY_DIRS
    for rel in candidate_files(root):
        if is_excluded(rel) or not (root / rel).is_file():
            continue
        top = rel.split("/", 1)[0]
        if "/" not in rel:
            if rel in root_files or rel.endswith(".py"):
                selected.add(rel)
        elif rel in scripts or top in dirs:
            selected.add(rel)
    return sorted(selected)


def is_image_path(rel: str) -> bool:
    """True if the Docker image (COPY . with root .dockerignore) contains rel."""
    if is_excluded(rel):
        return False
    top = rel.split("/", 1)[0]
    if "/" not in rel:
        return any(fnmatch.fnmatch(rel, pat) for pat in IMAGE_ALLOW_PATTERNS if "/" not in pat)
    return top in IMAGE_ALLOW_PATTERNS
