#!/usr/bin/env bash
# Check whether SHA-pinned Actions in the sync workflow template match the
# commit each commented version tag currently resolves to upstream.
#
# Exists because Dependabot's github-actions ecosystem can't see this file
# (it has a `.yml.template` extension) — see .github/dependabot.yml for the
# coverage rationale.
#
# Drift detection only checks "does the pinned SHA still match its
# commented version?" — not "is there a newer version available?". For
# the latter, glance at the upstream Releases tab when running this
# manually.
#
# Exit codes:
#   0 = every pin verified, all match the commented versions
#   1 = at least one pin failed verification (drift OR could-not-parse OR
#       upstream-tag-could-not-resolve). Script output names which.
#   2 = environment problem (missing bash 4.0+ / gh / jq)

set -euo pipefail

# Pre-flight: bash 4.0+ for associative arrays, plus the CLI tools the
# script shells out to. Friendly errors beat opaque "declare: -A: invalid
# option" or "command not found" mid-loop.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  echo "ERROR: bash 4.0+ required (associative arrays); have ${BASH_VERSION:-unknown}." >&2
  echo "       macOS ships 3.2 by default — install via 'brew install bash'." >&2
  exit 2
fi
for cmd in gh jq git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: '$cmd' not found in PATH (install: brew install $cmd / apt install $cmd)." >&2
    exit 2
  fi
done

# Resolve repo root so FILES paths work from any cwd.
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "ERROR: not in a git working tree (cannot resolve repo root for relative FILES paths)." >&2
  exit 2
}
cd "$repo_root"

FILES=(
  .github/workflows/sync-from-upstream.yml.template
)

declare -A upstream_cache  # spans both files — same Action+version → one API roundtrip
drift=0
unverified=0
for f in "${FILES[@]}"; do
  if [ ! -f "$f" ]; then
    printf '  ?  %s — file not found (FILES list out of sync with the repo?)\n' "$f"
    unverified=$((unverified + 1))
    continue
  fi
  printf '→ %s\n' "$f"
  while IFS= read -r line; do
    # One regex captures all three fields. A line that doesn't match this
    # shape (e.g. SHA pin without a version comment) is treated as a
    # verification failure — the script can't confirm the pin is current.
    if ! [[ "$line" =~ uses:[[:space:]]+([^@[:space:]]+)@([0-9a-f]{40})[[:space:]]+#[[:space:]]*(v[0-9]+(\.[0-9]+)*) ]]; then
      printf '  ?  %s — could not parse (missing version comment?)\n' "${line## }"
      unverified=$((unverified + 1))
      continue
    fi
    action="${BASH_REMATCH[1]}"
    sha="${BASH_REMATCH[2]}"
    version="${BASH_REMATCH[3]}"

    key="${action}@${version}"
    if [ -n "${upstream_cache[$key]:-}" ]; then
      upstream_sha="${upstream_cache[$key]}"
    else
      ref=$(gh api "repos/${action}/git/refs/tags/${version}" 2>/dev/null) || {
        printf '  ?  %-40s @ %s — cannot resolve upstream tag\n' "$action" "$version"
        unverified=$((unverified + 1))
        continue
      }
      # `git/refs/tags/X` returns an OBJECT on exact match, an ARRAY on
      # prefix match (e.g. `v6.0` matching `v6.0.0`/`v6.0.1`/`v6.0.2`).
      # An array means the comment is ambiguous — there's no canonical
      # upstream commit to verify against, so flag as unverified.
      if [ "$(printf '%s' "$ref" | jq -r 'type')" != "object" ]; then
        printf '  ?  %-40s @ %s — tag matched as prefix (use exact tag name in the version comment)\n' "$action" "$version"
        unverified=$((unverified + 1))
        continue
      fi
      # Annotated tags wrap a tag object that points at the commit;
      # lightweight tags point straight at the commit.
      obj_type=$(printf '%s' "$ref" | jq -r '.object.type')
      obj_sha=$(printf '%s' "$ref" | jq -r '.object.sha')
      if [ "$obj_type" = "tag" ]; then
        upstream_sha=$(gh api "repos/${action}/git/tags/${obj_sha}" --jq '.object.sha' 2>/dev/null) || {
          printf '  ?  %-40s @ %s — annotated-tag dereference failed\n' "$action" "$version"
          unverified=$((unverified + 1))
          continue
        }
      else
        upstream_sha="$obj_sha"
      fi
      upstream_cache[$key]="$upstream_sha"
    fi

    if [ "$sha" = "$upstream_sha" ]; then
      printf '  ✓  %-40s @ %s\n' "$action" "$version"
    else
      printf '  ✗  %-40s @ %s — pinned=%s upstream=%s\n' "$action" "$version" "$sha" "$upstream_sha"
      drift=$((drift + 1))
    fi
  # Match every `uses: <action>@<ref>` line, not just SHA-pinned ones —
  # a regression that re-introduces a floating tag (`@v6`) will fail the
  # strict parser regex inside the loop and be counted as `unverified`,
  # so the script can't silently miss un-pinned references.
  done < <(grep -E '^[[:space:]]+(-[[:space:]]+)?uses: [^@[:space:]]+@[^[:space:]]+' "$f")
done

exit_code=0
if [ "$drift" -gt 0 ]; then
  printf '\n⚠️  %d drift(s) found. Update the SHA (and comment if the version moved) in the affected file(s).\n' "$drift"
  exit_code=1
fi
if [ "$unverified" -gt 0 ]; then
  printf '\n⚠️  %d pin(s) could not be verified (parse or upstream-lookup failure).\n' "$unverified"
  exit_code=1
fi
if [ "$exit_code" -eq 0 ]; then
  printf '\n✅ All sync-template Action pins match upstream.\n'
fi
exit "$exit_code"
