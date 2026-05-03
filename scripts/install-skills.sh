#!/usr/bin/env bash
# Install the upstream Claude Code skills into the user's global skills
# directory by symlinking. Updates then flow via `git pull` in this checkout
# rather than per-repo sync PRs — no install-skills re-run needed unless new
# skills are added.
#
# Usage:
#   ./scripts/install-skills.sh           # safe: only install missing skills
#   ./scripts/install-skills.sh --force   # replace existing entries (backed up)
#   ./scripts/install-skills.sh --dry-run # report what would happen, write nothing
#
# Re-run after `git pull` in this repo only if you see "would install" output
# for a new skill — existing symlinks pick up upstream edits automatically.
#
# Source root override: by default the script uses the parent of its own
# directory (so `clone-root/scripts/install-skills.sh` finds skills at
# `clone-root/.claude/skills`). Set `UPSTREAM_ROOT_OVERRIDE` to point at a
# different checkout — useful when this script has been copied or vendored
# into a consumer that wants to install skills from a sibling clone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM_ROOT="${UPSTREAM_ROOT_OVERRIDE:-$(dirname "$SCRIPT_DIR")}"
SKILLS_SRC="$UPSTREAM_ROOT/.claude/skills"
SKILLS_DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

FORCE=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [ ! -d "$SKILLS_SRC" ]; then
  echo "❌ no skills found at $SKILLS_SRC — is this checkout complete?" >&2
  exit 2
fi

if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$SKILLS_DEST"
fi

installed=0
skipped=0
replaced=0

for src in "$SKILLS_SRC"/*/; do
  # Guard against an unexpanded glob if SKILLS_SRC is empty — without this,
  # `basename`/`cd` would run on the literal `*/` pattern and fail under
  # `set -e`. We compose absolute paths via `cd "$src" && pwd` rather than
  # `readlink -f`, since `-f` is GNU-only and breaks on default macOS.
  [ -d "$src" ] || continue
  name="$(basename "$src")"
  target="$SKILLS_DEST/$name"
  src_resolved="$(cd "$src" && pwd)"

  if [ -L "$target" ]; then
    # `readlink "$target"` returns the literal symlink content (portable
    # across GNU and BSD). We always create symlinks with absolute paths,
    # so a literal-vs-resolved comparison against `src_resolved` is
    # exact for our own links.
    current="$(readlink "$target")"
    if [ "$current" = "$src_resolved" ]; then
      echo "  ✓ $name (already linked)"
      skipped=$((skipped + 1))
      continue
    fi
    if [ "$FORCE" -eq 0 ]; then
      echo "  ⚠️  $name links elsewhere ($current) — re-run with --force to replace"
      skipped=$((skipped + 1))
      continue
    fi
    if [ "$DRY_RUN" -eq 0 ]; then
      rm "$target"
    fi
    echo "  🔗 $name (replacing existing symlink)"
    if [ "$DRY_RUN" -eq 0 ]; then
      ln -s "$src_resolved" "$target"
    fi
    replaced=$((replaced + 1))
    continue
  fi

  if [ -e "$target" ]; then
    if [ "$FORCE" -eq 0 ]; then
      echo "  ⚠️  $name exists as a regular file/dir — re-run with --force to replace (will be backed up)"
      skipped=$((skipped + 1))
      continue
    fi
    backup="$target.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    echo "  📦 $name → backing up existing to $(basename "$backup")"
    if [ "$DRY_RUN" -eq 0 ]; then
      mv "$target" "$backup"
      ln -s "$src_resolved" "$target"
    fi
    replaced=$((replaced + 1))
    continue
  fi

  echo "  ➕ $name (would install)"
  if [ "$DRY_RUN" -eq 0 ]; then
    ln -s "$src_resolved" "$target"
  fi
  installed=$((installed + 1))
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "Dry run: $installed would install, $replaced would replace, $skipped left alone."
  echo "(no changes written)"
else
  echo ""
  echo "Done: $installed installed, $replaced replaced, $skipped left alone."
  echo "Skills now resolve from $SKILLS_DEST → $SKILLS_SRC."
  echo "Run \`git pull\` in $UPSTREAM_ROOT to update — no re-install needed."
fi
