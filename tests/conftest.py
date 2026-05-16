"""Shared fixtures + a sys.path shim that lets the test modules import
the hyphenated script files (`sync-engine.py`, `create-signed-commit.py`)
as Python modules without renaming them on disk.

The scripts are designed to be run from CI as `python3 scripts/foo.py`,
so they don't need to be importable from the source tree. The tests use
`importlib.util.spec_from_file_location` via `_load_script` below to
load them as pseudo-modules — cleaner than spawning subprocesses for
every function-level unit test.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = ModuleType(name)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sync_engine() -> ModuleType:
    return _load_script("sync_engine", SCRIPTS_DIR / "sync-engine.py")


@pytest.fixture(scope="session")
def create_signed_commit() -> ModuleType:
    return _load_script("create_signed_commit", SCRIPTS_DIR / "create-signed-commit.py")


@pytest.fixture
def upstream_repo(tmp_path: Path) -> Path:
    """A skeleton upstream checkout: scripts/sync-engine.py + scripts/sync-targets.yml.

    Tests write their own sync-targets.yml + source files; the engine is
    invoked against this fixture as `--upstream-repo`.
    """
    upstream = tmp_path / "upstream"
    (upstream / "scripts").mkdir(parents=True)
    return upstream


@pytest.fixture
def consumer_dir(tmp_path: Path) -> Path:
    """A skeleton consumer working tree. Tests write their own .platform-config.yml."""
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    return consumer


@pytest.fixture
def write_targets(upstream_repo: Path) -> Iterator[None]:
    """Sentinel to mark that a test should populate sync-targets.yml itself.

    No-op placeholder; tests use `upstream_repo` directly. Kept for
    discoverability — if a future test wants a helper that writes a
    canonical-shape manifest, it can hook here.
    """
    yield None
