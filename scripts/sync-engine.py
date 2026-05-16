#!/usr/bin/env python3
"""Sync canonical files from an upstream repo into a consumer repo.

Reads `scripts/sync-targets.yml` from the upstream checkout to learn which
files belong to which destinations and which placeholders need substitution.
Reads `.platform-config.yml` from the consumer to resolve those placeholders.
Writes substituted files into the consumer working directory.

A target with `delete: true` instead causes the engine to *unlink* the
destination on the consumer (idempotent; no-op if already absent), then
prune empty parent directories up to the consumer root. Use this to
retire files that were previously synced — without it, deprecated stubs
linger forever as dead bytes on consumer disks.

Run from the consumer repo's CI (via `sync-from-upstream.yml.template`) or
locally for testing:

    python3 /tmp/upstream/scripts/sync-engine.py \\
        --upstream-repo /tmp/upstream \\
        --consumer-dir .

Exit codes:
    0  success (changes may or may not have been written; check `git diff`)
    1  config or input error (missing required placeholder, malformed YAML, etc.)
    2  invocation error (bad arguments, missing files)
"""
from __future__ import annotations

import argparse
import errno
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, Required, TypedDict

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "PyYAML is required. Install with `pip install pyyaml` or "
        "ensure the consumer's sync workflow does so before invoking.\n"
    )
    sys.exit(2)


class Target(TypedDict, total=False):
    """One entry in `scripts/sync-targets.yml`.

    Either a copy target (requires `source` + `destination`) or a delete
    target (requires `destination` + `delete: True`). `substitutions` and
    `mode` apply to copy targets only.

    `create_if_missing: True` on a copy target makes the engine bootstrap
    the destination on first sync and then leave it alone — preserving any
    consumer customization on subsequent syncs. Mutually exclusive with
    `delete`.

    The schema is documented here for readers; the engine still validates
    each field at runtime since YAML provides no type guarantees.
    """

    source: str
    destination: Required[str]
    substitutions: list[str]
    mode: str | int
    delete: bool
    create_if_missing: bool


class ConsumerConfig(TypedDict, total=False):
    """Top-level shape of a consumer's `.platform-config.yml`."""

    substitutions: dict[str, str]
    skip_targets: list[str]
    allowed_destinations: list[str]


PLACEHOLDER_RE = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a gitignore-flavored glob pattern to an anchored regex.

    Cases handled:
        `**/`  (start or mid-pattern) → zero or more leading/intermediate
               path segments. So `**/foo.md` matches `foo.md`, `a/foo.md`,
               and `a/b/c/foo.md`; `a/**/b` matches `a/b` and `a/x/y/b`.
        trailing `**` → matches the rest of the path (any characters,
               including `/`). So `.claude/skills/**` matches
               `.claude/skills/grill/SKILL.md` but NOT `.claude/skills`
               itself (the trailing `/` in the pattern is required).
        bare `**` without an adjacent `/` → treated as `.*` (any-chars).
        `*` → any run of characters except `/`.
        `?` → a single character except `/`.

    Everything else is escaped as literal — regex metachars like `.` and
    `+` are not honored. The returned pattern is anchored at both ends —
    partial matches do not count. Used by both the consumer-side
    `allowed_destinations` allowlist and the engine-level
    `SENSITIVE_DELETE_PATTERNS` constant.
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            # `**/` — zero or more leading path segments (or none at all).
            # Without the trailing `/`, treat `**` as "match the rest of
            # the path including any separators."
            if i + 2 < len(pattern) and pattern[i + 2] == "/":
                parts.append("(?:.*/)?")
                i += 3
            else:
                parts.append(".*")
                i += 2
        elif c == "*":
            parts.append("[^/]*")
            i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile(r"\A" + "".join(parts) + r"\Z")


def path_matches_any(path: str, compiled_patterns: Sequence[re.Pattern[str]]) -> bool:
    """Return True if `path` matches any of the pre-compiled glob patterns."""
    return any(p.match(path) is not None for p in compiled_patterns)


# Paths the engine refuses to `delete:` from a consumer tree, regardless of
# whether they appear in the consumer's `allowed_destinations` list. A
# compromised upstream-authored manifest entry could otherwise enumerate
# every CI workflow under `.github/workflows/**` for deletion, leaving the
# consumer with no CI gate before the next sync lands. The refusal applies
# to delete entries only — these paths are still legitimate copy targets
# (a consumer may want their workflows synced from upstream).
#
# Extension criterion: include a path here when its *absence* would weaken
# a runtime, build, or review invariant more than wrong content would —
# wrong content tends to fail loudly at the next CI run; absence is silent.
# That's why workflows + composite actions + CODEOWNERS + lockfiles + schema
# + Dockerfile are in; tsconfig.json (loud compile failure) is not.
SENSITIVE_DELETE_PATTERNS: Final[tuple[str, ...]] = (
    ".github/workflows/**",
    ".github/actions/**",
    ".github/CODEOWNERS",
    "package.json",
    "pnpm-lock.yaml",
    "prisma/schema.prisma",
    "Dockerfile",
    "Dockerfile.*",
)
def _compile_case_insensitive(pattern: str) -> re.Pattern[str]:
    """Recompile a glob pattern with `re.IGNORECASE`.

    Sensitive paths must be blocked even on case-insensitive filesystems
    (macOS APFS, NTFS) where `dockerfile` resolves to the same on-disk
    file as `Dockerfile`. Fleet sync runs on `ubuntu-latest` (case-
    sensitive ext4), so this is defense-in-depth for self-hosted or
    macos-/windows-latest runners.
    """
    return re.compile(glob_to_regex(pattern).pattern, re.IGNORECASE)


SENSITIVE_DELETE_REGEXES: Final[tuple[re.Pattern[str], ...]] = tuple(
    _compile_case_insensitive(p) for p in SENSITIVE_DELETE_PATTERNS
)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        sys.stderr.write(f"missing required file: {path}\n")
        sys.exit(2)
    with path.open() as fp:
        return yaml.safe_load(fp) or {}


def substitute(text: str, values: dict[str, str], target_keys: list[str], source: str) -> str:
    """Replace `<<KEY>>` tokens in text with values from `values`.

    Only keys listed in `target_keys` are substituted — unknown placeholders
    in the source are left intact (and a warning is printed) so that a
    template change doesn't silently swallow content the consumer hadn't
    configured for yet.
    """
    seen = set(PLACEHOLDER_RE.findall(text))
    declared = set(target_keys)

    missing_in_source = declared - seen
    if missing_in_source:
        sys.stderr.write(
            f"  ⚠️  declared substitutions not found in {source}: "
            f"{', '.join(sorted(missing_in_source))}\n"
        )

    undeclared_in_source = seen - declared
    if undeclared_in_source:
        sys.stderr.write(
            f"  ⚠️  placeholders in {source} not declared in sync-targets.yml: "
            f"{', '.join(sorted(undeclared_in_source))} (left intact)\n"
        )

    missing_in_config = declared - set(values.keys())
    if missing_in_config:
        sys.stderr.write(
            f"  ❌ {source} requires placeholders missing from .platform-config.yml: "
            f"{', '.join(sorted(missing_in_config))}\n"
        )
        sys.exit(1)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in declared:
            # YAML `|` block scalars carry a trailing newline that, combined
            # with the template's explicit blank line after each placeholder,
            # produces double-blank-line drift in rendered output. Strip
            # trailing newlines so the template alone controls inter-section
            # spacing.
            return str(values[key]).rstrip("\n")
        return match.group(0)

    return PLACEHOLDER_RE.sub(replace, text)


def write_if_changed(path: Path, content: str, mode: int | None) -> bool:
    """Write content to path only if it differs. Return True if a write happened."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    changed = existing != content
    if changed:
        path.write_text(content, encoding="utf-8")
    if mode is not None:
        # `stat.S_IMODE` keeps the full 12-bit permission set (setuid +
        # setgid + sticky + rwx*3). `& 0o777` would mask off the upper
        # 3 bits, so a manifest entry like `mode: 0o4755` would never
        # match the current mode and the file would be re-chmod'd on
        # every sync run.
        current = stat.S_IMODE(path.stat().st_mode)
        if current != mode:
            path.chmod(mode)
            changed = True
    return changed


def resolve_under(parent: Path, child_rel: str) -> Path | None:
    """Compute parent/child_rel and return it only if it lies under parent.

    Returns None if `child_rel` would escape `parent` via `..` segments or
    an absolute path. Uses lexical normalization (`os.path.normpath`) — not
    `Path.resolve()` — so that legitimate symlinks at the destination,
    including dangling ones, are not mis-flagged as escaping the parent.

    Limitation: lexical-only check does NOT prevent traversal via a
    symlink in an intermediate directory (e.g., a consumer-side symlink
    that points outside the consumer tree). The threat model assumes an
    upstream-controlled manifest and consumer trees free of malicious
    symlinks; defense against an attacker who can plant symlinks in the
    consumer working tree is out of scope.
    """
    candidate = Path(os.path.normpath(parent / child_rel))
    if candidate == parent:
        # `child_rel` normalized back to the parent itself (e.g., `foo/..`).
        # Targets must always resolve to a child path, never the root.
        return None
    try:
        candidate.relative_to(parent)
    except ValueError:
        return None
    return candidate


def prune_empty_parents(file_path: Path, root: Path) -> None:
    """Walk up from file_path's parent toward root, removing empty dirs.

    Stops at root (does not remove root itself) and at the first non-empty
    directory. ENOTEMPTY and ENOENT (concurrent remove) are benign stop
    conditions handled silently; other OSErrors are logged to stderr and
    also stop the walk. Pruning is best-effort — failures are surfaced for
    visibility but do not propagate, since the file unlink has already
    succeeded by the time the parent walk runs.
    """
    parent = file_path.parent.resolve()
    root = root.resolve()
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError as e:
            if e.errno not in (errno.ENOTEMPTY, errno.ENOENT):
                sys.stderr.write(f"  ⚠️  could not prune {parent}: {e}\n")
            return
        parent = parent.parent


def parse_mode(value: object) -> int | None:
    """Coerce a `mode` field from sync-targets.yml into a permission int.

    Accepts both a quoted string (`"0755"` — interpreted as octal) and an
    unquoted YAML int (`0755` — already parsed octal in YAML 1.1, decimal in 1.2).
    Returning `None` means "leave the file's current mode alone."
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # `bool` is a subclass of `int`; reject explicitly so a stringly-
        # typed `true` / `false` doesn't become `mode: 1` / `mode: 0`.
        raise TypeError(f"mode must be int or str, got bool: {value!r}")
    if isinstance(value, int):
        mode_int = value
    elif isinstance(value, str):
        mode_int = int(value, 8)
    else:
        raise TypeError(f"mode must be int, str, or None; got {type(value).__name__}")
    if not 0 <= mode_int <= 0o7777:
        # Negative or >12-bit values pass `int(_, 8)` but break `Path.chmod`
        # mid-loop with `OverflowError`, half-syncing the consumer tree.
        # Fail-closed before any write happens.
        raise ValueError(f"mode out of range [0, 0o7777]: {value!r}")
    return mode_int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream-repo", required=True, type=Path, help="path to a checkout of the upstream repo")
    parser.add_argument("--consumer-dir", required=True, type=Path, help="path to the consumer repo (dest)")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to .platform-config.yml (default: <consumer-dir>/.platform-config.yml)",
    )
    parser.add_argument("--dry-run", action="store_true", help="don't write files; report what would change")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    upstream_repo = args.upstream_repo.resolve()
    consumer_dir = args.consumer_dir.resolve()
    config_path = (args.config or consumer_dir / ".platform-config.yml").resolve()

    targets_path = upstream_repo / "scripts" / "sync-targets.yml"
    targets_doc = load_yaml(targets_path)
    config_doc = load_yaml(config_path)

    targets = targets_doc.get("targets") or []
    values = config_doc.get("substitutions") or {}
    skip = set(config_doc.get("skip_targets") or [])

    if not isinstance(targets, list):
        sys.stderr.write(f"{targets_path}: `targets` must be a list\n")
        return 1
    if not isinstance(values, dict):
        sys.stderr.write(f"{config_path}: `substitutions` must be a mapping\n")
        return 1

    # Consumer-side allowlist: shifts the trust boundary from "upstream
    # maintainer didn't get fooled" to "consumer explicitly opted in to
    # each destination path." Tri-state:
    #   absent key       → fail-open warn (migration mode; will flip to
    #                      fail-closed once all consumers ship allowlists)
    #   `allowed_destinations:` with no value → config error (almost certainly
    #                      a mid-edit accident; an empty list is the explicit
    #                      "deny everything" knob, written `[]`)
    #   non-empty list   → every write/delete must match at least one pattern
    #   `[]`             → deny everything (the "lock this consumer" knob)
    allowed_patterns: list[re.Pattern[str]] | None  # None = migration fail-open
    if "allowed_destinations" not in config_doc:
        # GitHub Actions annotation surfaces this in the PR UI instead of
        # being buried in a green-checkmark build's stderr.
        sys.stderr.write(
            f"::warning file={config_path}::`allowed_destinations` not set. "
            f"Upstream sync-targets are currently trusted to write anywhere "
            f"in the consumer tree. Add an `allowed_destinations:` list to "
            f"enforce the gate before the engine flips fail-closed.\n"
        )
        allowed_patterns = None
    else:
        allowed_raw = config_doc["allowed_destinations"]
        if allowed_raw is None:
            sys.stderr.write(
                f"{config_path}: `allowed_destinations:` is present but null. "
                f"Use `[]` to deny everything, or remove the key to opt into "
                f"phase-1 fail-open behavior.\n"
            )
            return 1
        if not isinstance(allowed_raw, list) or not all(
            isinstance(p, str) for p in allowed_raw
        ):
            sys.stderr.write(
                f"{config_path}: `allowed_destinations` must be a list of strings\n"
            )
            return 1
        allowed_patterns = [glob_to_regex(p) for p in allowed_raw]

    print(f"Syncing from {upstream_repo} → {consumer_dir}")
    if args.dry_run:
        print("(dry run — no files will be written)")

    written = 0
    removed = 0
    skipped = 0
    unchanged = 0

    for target in targets:
        # Each `targets:` entry must be a mapping. A bare scalar (string,
        # int) would raise AttributeError on `.get(...)` below; surface as
        # a clean malformed-entry error instead.
        if not isinstance(target, dict):
            sys.stderr.write(f"  ❌ malformed target entry: expected a mapping, got {target!r}\n")
            return 1
        source_rel = target.get("source")
        dest_rel = target.get("destination")
        subs = target.get("substitutions") or []

        # Require `delete` to be a real boolean if present. Strings like
        # "false" / "no" are truthy in Python, so a stringly-typed mistake
        # would silently arm a sync-wide unlink. Hard-fail instead.
        delete_raw = target.get("delete")
        if delete_raw is not None and not isinstance(delete_raw, bool):
            sys.stderr.write(
                f"  ❌ `delete` must be a boolean (true/false), got {delete_raw!r}: {target!r}\n"
            )
            return 1
        delete_flag = bool(delete_raw)

        # Same boolean-strictness for `create_if_missing` — a stringly-typed
        # value would silently disable the bootstrap-only semantics and
        # clobber consumer customization on every sync.
        cim_raw = target.get("create_if_missing")
        if cim_raw is not None and not isinstance(cim_raw, bool):
            sys.stderr.write(
                f"  ❌ `create_if_missing` must be a boolean (true/false), got {cim_raw!r}: {target!r}\n"
            )
            return 1
        create_if_missing_flag = bool(cim_raw)

        if delete_flag and create_if_missing_flag:
            sys.stderr.write(
                f"  ❌ `delete` and `create_if_missing` are mutually exclusive: {target!r}\n"
            )
            return 1

        # Type/shape validation. The manifest is upstream-authored, so
        # non-string paths or bare `.`/`..` here are bugs that warrant a
        # clean error rather than a downstream TypeError or write-the-cwd
        # surprise. `mode` only validates here for non-delete targets —
        # `parse_mode` raises on bad input, and a `mode` field on a
        # delete target is meaningless. Control characters are rejected
        # outright: `[^/]*` in the glob compiler matches newlines, so an
        # allowlist pattern like `.claude/skills/*` would otherwise accept
        # `.claude/skills/foo\nbar` as a valid destination. The on-disk
        # write would succeed; downstream tooling that ingests sync diffs
        # would see a weirdly-named file that human review could miss.
        if dest_rel is not None and (
            not isinstance(dest_rel, str)
            or not dest_rel
            or dest_rel in (".", "..")
            or not dest_rel.isprintable()
        ):
            sys.stderr.write(
                f"  ❌ `destination` must be a non-empty printable path string, "
                f"got {dest_rel!r}: {target!r}\n"
            )
            return 1
        if source_rel is not None and (
            not isinstance(source_rel, str)
            or not source_rel
            or source_rel in (".", "..")
            or not source_rel.isprintable()
        ):
            sys.stderr.write(
                f"  ❌ `source` must be a non-empty printable path string, "
                f"got {source_rel!r}: {target!r}\n"
            )
            return 1

        # `source` is required for copy entries but optional for delete entries
        # (the source file may no longer exist in the upstream — that's the
        # whole point of retiring it). `destination` is always required. The
        # manifest is upstream-authored and sync-propagating, so a malformed
        # entry is a bug that warrants surfacing loudly rather than silently
        # dropping.
        if not dest_rel or (not delete_flag and not source_rel):
            sys.stderr.write(f"  ❌ malformed entry: {target!r}\n")
            return 1

        # Parse `mode` only for copy targets. `parse_mode` raises on
        # non-octal input; running it before the delete-branch short-circuit
        # would crash on a typoed `mode` field that delete entries shouldn't
        # carry anyway.
        if delete_flag:
            if target.get("mode") is not None:
                sys.stderr.write(f"  ❌ `mode` is not valid on a delete target: {target!r}\n")
                return 1
            mode = None
        else:
            try:
                mode = parse_mode(target.get("mode"))
            except (ValueError, TypeError) as e:
                sys.stderr.write(f"  ❌ invalid `mode` ({e}): {target!r}\n")
                return 1

        if (source_rel and source_rel in skip) or dest_rel in skip:
            label = source_rel or dest_rel
            print(f"  ⏭️  skip {label} (opted out via .platform-config.yml)")
            skipped += 1
            continue

        # Destination paths come from an upstream-controlled manifest today,
        # but this guards against a typo (`../shared/foo`) becoming a
        # cross-tree write/delete primitive outside the consumer.
        dest_path = resolve_under(consumer_dir, dest_rel)
        if dest_path is None:
            sys.stderr.write(f"  ❌ destination escapes consumer root: {dest_rel}\n")
            return 1

        # Canonicalize for policy matching. `resolve_under` collapses `./`,
        # `//`, and `foo/../` segments via `os.path.normpath`, so the on-disk
        # write target is `dest_path` — but the allowlist and
        # `SENSITIVE_DELETE_REGEXES` match by string against the manifest's
        # `destination` field. Without normalization, a manifest entry like
        # `./.github/workflows/release.yml` resolves to the guarded file on
        # disk while bypassing the anchored `.github/workflows/**` pattern.
        # Reject non-canonical strings outright: every fleet manifest entry
        # uses canonical posix-relative paths, so a mismatch is either a
        # typo (clean error beats silent rewrite) or an attack attempt.
        dest_rel_canonical = dest_path.relative_to(consumer_dir).as_posix()
        if dest_rel_canonical != dest_rel:
            sys.stderr.write(
                f"  ❌ destination must be in canonical posix form (no `./`, "
                f"`//`, or `..` segments): got {dest_rel!r}, normalized form "
                f"is {dest_rel_canonical!r}\n"
            )
            return 1

        # Consumer-side allowlist enforcement. Applies uniformly to copy,
        # delete, and create_if_missing targets — the threat model is
        # "upstream manifest can write/delete consumer files" and all three
        # actions touch the destination.
        if allowed_patterns is not None and not path_matches_any(
            dest_rel_canonical, allowed_patterns
        ):
            sys.stderr.write(
                f"  ❌ destination not in consumer's `allowed_destinations`: "
                f"{dest_rel_canonical}\n"
            )
            return 1

        if delete_flag:
            # Engine-level refusal for paths whose deletion would remove
            # consumer-side guardrails (CI workflows, composite actions,
            # CODEOWNERS, lockfiles, schema, container build). Applies
            # regardless of allowlist — a consumer that legitimately syncs
            # CI workflows still must not have those workflows deletable
            # by manifest entry.
            if path_matches_any(dest_rel_canonical, SENSITIVE_DELETE_REGEXES):
                sys.stderr.write(
                    f"  ❌ refusing to delete sensitive path (engine-level "
                    f"block, applies regardless of allowed_destinations): "
                    f"{dest_rel_canonical}\n"
                )
                return 1
            # Refuse to unlink a real directory at the destination —
            # `unlink()` would raise `IsADirectoryError` and abort the
            # whole sync. Symlinks-to-directories are still removable
            # (unlink removes the link, not the target), so guard on
            # `is_dir() and not is_symlink()`.
            if dest_path.is_dir() and not dest_path.is_symlink():
                sys.stderr.write(
                    f"  ❌ destination is a directory, refusing to unlink: {dest_rel}\n"
                )
                return 1
            # `exists()` follows symlinks and returns False on a dangling
            # link; pair with `is_symlink()` so broken symlinks still get
            # unlinked instead of leaving as silent residue.
            existed = dest_path.exists() or dest_path.is_symlink()
            if args.dry_run:
                if existed:
                    print(f"  🗑️  would remove {dest_rel}")
                    removed += 1
                else:
                    print(f"  ✓  already absent {dest_rel}")
                    unchanged += 1
                continue
            if not existed:
                print(f"  ✓  already absent {dest_rel}")
                unchanged += 1
                continue
            dest_path.unlink(missing_ok=True)
            prune_empty_parents(dest_path, consumer_dir)
            print(f"  🗑️  removed {dest_rel}")
            removed += 1
            continue

        # `create_if_missing: True` bootstraps the destination on first
        # sync and leaves it alone thereafter, so consumer customization
        # of the file survives subsequent syncs. Short-circuit before
        # source read + substitution — when the file already exists,
        # missing substitution values in the consumer's config must NOT
        # fail the sync (the file's content is no longer the upstream's
        # concern). `exists() or is_symlink()` mirrors the delete branch's
        # treatment of dangling symlinks as "present."
        #
        # Refuse a directory at the destination — the manifest entry
        # describes a file, and silently treating a directory as
        # "preserved" would mask consumer-side bad state and leave the
        # bootstrap target permanently uncreated. Mirrors the delete
        # branch's directory-refusal pattern.
        if create_if_missing_flag:
            if dest_path.is_dir() and not dest_path.is_symlink():
                sys.stderr.write(
                    f"  ❌ destination is a directory, refusing to bootstrap a file there: {dest_rel}\n"
                )
                return 1
            if dest_path.exists() or dest_path.is_symlink():
                print(f"  ✓  preserved {dest_rel} (create_if_missing)")
                unchanged += 1
                continue

        # Same path-bound check on `source` as `destination` — a manifest
        # typo with `..` segments would otherwise read arbitrary files
        # from the runner filesystem rather than from the upstream repo.
        # Explicit guard (not `assert`) so this still narrows under
        # `python -O` — the malformed-entry check above also rejects None
        # for copy targets, so this branch is defense-in-depth.
        if source_rel is None:
            sys.stderr.write(
                f"  ❌ internal invariant violated: copy target reached "
                f"source-resolve with `source` unset: {target!r}\n"
            )
            return 1
        source_path = resolve_under(upstream_repo, source_rel)
        if source_path is None:
            sys.stderr.write(f"  ❌ source escapes upstream repo: {source_rel}\n")
            return 1

        if not source_path.is_file():
            sys.stderr.write(f"  ❌ source missing in upstream: {source_rel}\n")
            return 1

        text = source_path.read_text(encoding="utf-8")
        # Always run substitution — even when subs=[] — so that the
        # "undeclared placeholder in source" warning fires when a developer
        # adds a `<<KEY>>` token to a source file but forgets to declare
        # it in sync-targets.yml.
        substituted = substitute(text, values, subs, source_rel)

        if args.dry_run:
            existing = dest_path.read_text(encoding="utf-8") if dest_path.is_file() else None
            current_mode = stat.S_IMODE(dest_path.stat().st_mode) if dest_path.is_file() else None
            content_diverged = existing != substituted
            mode_diverged = mode is not None and current_mode is not None and current_mode != mode
            if content_diverged or mode_diverged:
                reason = "content" if content_diverged else "mode"
                print(f"  📝 would write {dest_rel} ({reason})")
                written += 1
            else:
                unchanged += 1
            continue

        if write_if_changed(dest_path, substituted, mode):
            print(f"  ✅ wrote {dest_rel}")
            written += 1
        else:
            unchanged += 1

    print(f"\nDone: {written} written, {removed} removed, {unchanged} unchanged, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
