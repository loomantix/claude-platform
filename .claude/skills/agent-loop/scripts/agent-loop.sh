#!/bin/bash
# /agent-loop — autonomous Claude relay on top of /issues.
#
# Usage: agent-loop.sh [iterations] [collection-branch] [--resume]
#
# Default: 10 iterations, auto-generated collection branch off the repo's
# default branch (main / staging / etc., auto-detected via origin/HEAD).
#
# Examples:
#   agent-loop.sh 5                       # 5 iterations, auto-generated branch
#   agent-loop.sh 5 wasm-plugins          # 5 iterations, named branch, ready-only
#   agent-loop.sh 5 wasm-plugins --resume # also pick up orphaned in-progress issues
#   agent-loop.sh                         # 10 iterations, auto-generated branch
#
# Each iteration:
#   1. Fast-forward sync with the collection branch (handles concurrent
#      merges and force-pushes). Genuine merge conflicts fail loud — the
#      eventual push surfaces persistent conflicts via PUSH_FAILURES.
#   2. Pick a work item — first `dev: agent`-labeled issue from the ready
#      queue, or (with --resume) an existing in-progress issue assigned to the
#      operator (also gated to `dev: agent`).
#   3. Claim by adding @me as assignee. If a race happens (>1 assignee after),
#      release and try the next.
#   4. Spawn a fresh Claude session with bypassPermissions, tasked with
#      reading `@agent-loop-instructions.md` and completing the assigned
#      issue. Stream output.
#   5. Push completed work to the collection branch with retry-and-merge logic.
#
# After the loop: opens an `agent-loop: <branch>` PR with a summary of
# closed issues and the commit log. Cleans up the worktree.
#
# Source of truth: upstream `.claude/skills/agent-loop/scripts/`. Synced to
# consumers via sync-targets.yml; edits in a consumer repo will be
# overwritten on next sync.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

MAX_ITERATIONS=10
COLLECTION_BRANCH=""
RESUME_IN_PROGRESS=false

for arg in "$@"; do
    case "$arg" in
        --resume) RESUME_IN_PROGRESS=true ;;
        --help|-h)
            awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 && !/^#/ {exit}' "$0"
            exit 0
            ;;
        *)
            if [ -z "$MAX_ITERATIONS_SET" ]; then
                MAX_ITERATIONS="$arg"
                MAX_ITERATIONS_SET=1
            elif [ -z "$COLLECTION_BRANCH" ]; then
                COLLECTION_BRANCH="$arg"
            fi
            ;;
    esac
done

if ! [[ "$MAX_ITERATIONS" =~ ^[0-9]+$ ]] || [ "$MAX_ITERATIONS" -lt 1 ]; then
    echo -e "${RED}✗${NC} iterations must be a positive integer, got: $MAX_ITERATIONS"
    exit 1
fi

if [ -z "$COLLECTION_BRANCH" ]; then
    COLLECTION_BRANCH="agent-loop-$(date +%Y%m%d-%H%M%S)-$(head -c2 /dev/urandom | xxd -p)"
fi

# Reject branch names with `..` segments or non-portable characters before
# the name is used to build a /tmp path. The realistic-escape risk is bounded
# by the trailing -$$ PID anchor, but cheap to defend properly.
if ! [[ "$COLLECTION_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "$COLLECTION_BRANCH" == *..* ]]; then
    echo -e "\033[0;31m✗\033[0m collection-branch contains illegal characters: $COLLECTION_BRANCH"
    echo "   allowed: [A-Za-z0-9._/-], no '..' segments"
    exit 1
fi

# Walk up from the script's directory to the repo root rather than assuming
# a fixed depth — the script lives under .claude/skills/agent-loop/scripts/
# when synced into a consumer, but a developer might also run it from an
# upstream clone where the layout differs.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# `|| true` prevents `set -e` from aborting on a non-zero `git rev-parse`
# (e.g., script run outside any git repo) before the friendly check below.
PROJECT_DIR="$(cd "$SCRIPT_DIR" && git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$PROJECT_DIR" ] || [ ! -d "$PROJECT_DIR/.git" ]; then
    echo -e "${RED}✗${NC} Could not find repo root from $SCRIPT_DIR"
    exit 1
fi

ITERATION=0
# Sanitize the branch name for use in a filesystem path — slashes (legal
# in git branch names like `agent-loop/foo`) would otherwise create nested
# directories under /tmp and break worktree add/remove.
WORKTREE_NAME=$(echo "$COLLECTION_BRANCH" | tr '/' '-')
WORKTREE_DIR="/tmp/agent-loop-${WORKTREE_NAME}-$$"
CLOSED_ISSUES=()

CLAUDE_PID=""

cleanup() {
    echo -e "\n${RED}✗${NC} Interrupted — cleaning up"
    [ -n "$CLAUDE_PID" ] && kill "$CLAUDE_PID" 2>/dev/null

    # Try to flush committed work to the collection branch. If the push
    # fails (auth, rejection, no upstream yet), DO NOT remove the worktree
    # — it's the only copy of the work and a human can recover from it.
    local push_ok=true
    if [ -d "$WORKTREE_DIR" ]; then
        if cd "$WORKTREE_DIR" 2>/dev/null; then
            if ! git push origin "HEAD:$COLLECTION_BRANCH" --quiet 2>/dev/null; then
                push_ok=false
            fi
        else
            push_ok=false
        fi
    fi

    cd "$PROJECT_DIR"
    if [ "$push_ok" = true ]; then
        git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || true
        # Drop the per-worker local branch ref. Skip when the worktree
        # was preserved — its branch is still checked out there.
        git branch -D "$LOCAL_BRANCH" 2>/dev/null || true
    else
        echo -e "${YELLOW}⚠${NC} Final push failed — preserving worktree at $WORKTREE_DIR for manual recovery"
    fi

    exit 130
}
trap cleanup INT TERM

cd "$PROJECT_DIR"

for cmd in gh jq xxd python3 claude; do
    if ! command -v "$cmd" &> /dev/null; then
        echo -e "${RED}✗${NC} required command not found: $cmd"
        exit 1
    fi
done

ISSUES_READY="$PROJECT_DIR/.claude/skills/issues/scripts/ready.py"
if [ ! -x "$ISSUES_READY" ]; then
    echo -e "${RED}✗${NC} /issues skill's ready.py not found or not executable: $ISSUES_READY"
    echo "   The /agent-loop skill depends on /issues being synced. Run the upstream sync workflow first."
    exit 1
fi

if [ ! -f "$PROJECT_DIR/agent-loop-instructions.md" ]; then
    echo -e "${RED}✗${NC} agent-loop-instructions.md not found in repo root"
    echo "   /agent-loop spawns Claude with the prompt 'Read @agent-loop-instructions.md and follow the instructions'."
    echo "   Each repo carries its own agent-loop-instructions.md describing how the agent should work the codebase."
    echo "   Add an agent-loop-instructions.md at the repo root before invoking /agent-loop."
    exit 1
fi

DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@')
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"

REPO_SLUG=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null || echo "unknown/unknown")

echo -e "${CYAN}→${NC} Starting /agent-loop in $PROJECT_DIR ($REPO_SLUG)"
echo "   Collection branch: $COLLECTION_BRANCH (off origin/$DEFAULT_BRANCH)"
echo "   Max iterations: $MAX_ITERATIONS"
echo "   Worktree: $WORKTREE_DIR"
echo "   Resume orphaned: $RESUME_IN_PROGRESS"
echo ""

echo -e "${BLUE}▸${NC} Current ready queue:"
"$ISSUES_READY" 2>/dev/null | head -10 || echo "   (queue empty or /issues not configured)"
echo ""

# --- Collection branch setup ---

git worktree prune 2>/dev/null || true

echo -e "${DIM}› Fetching latest from origin...${NC}"
git fetch origin --quiet

if git rev-parse "origin/$COLLECTION_BRANCH" &>/dev/null; then
    BASE="origin/$COLLECTION_BRANCH"
    echo -e "${GREEN}✓${NC} Joining existing collection branch: $COLLECTION_BRANCH"
else
    BASE="origin/$DEFAULT_BRANCH"
    echo -e "${GREEN}✓${NC} Creating new collection branch from $DEFAULT_BRANCH"
fi

if [ -d "$WORKTREE_DIR" ]; then
    echo -e "${YELLOW}› Removing stale worktree at $WORKTREE_DIR${NC}"
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
fi

LOCAL_BRANCH="agent-loop-worker-$$"
git branch -D "$LOCAL_BRANCH" 2>/dev/null || true
git worktree add "$WORKTREE_DIR" -b "$LOCAL_BRANCH" "$BASE" --quiet
cd "$WORKTREE_DIR"

echo -e "${GREEN}✓${NC} Worktree ready at $WORKTREE_DIR (local branch: $LOCAL_BRANCH)"
echo ""

# Push HEAD to the shared collection branch with retry-on-remote-moved.
# No auto-resolve heuristics: previous *.lock → theirs / *.md → ours rules
# silently overwrote intentional dependency-bumps and documentation
# updates from collaborating workers. Conflicts now fail loudly and the
# outer PUSH_FAILURES counter handles persistence.
push_to_collection() {
    for attempt in 1 2 3; do
        if git push origin "HEAD:$COLLECTION_BRANCH" --quiet 2>/dev/null; then
            return 0
        fi
        echo -e "${YELLOW}› Push conflict (attempt $attempt/3) — syncing with remote...${NC}"
        git fetch origin "$COLLECTION_BRANCH" --quiet

        REMOTE_HEAD=$(git rev-parse "origin/$COLLECTION_BRANCH" 2>/dev/null)
        MERGE_BASE=$(git merge-base HEAD "origin/$COLLECTION_BRANCH" 2>/dev/null || echo "none")

        if [ "$MERGE_BASE" = "none" ] || ! git merge-base --is-ancestor "$MERGE_BASE" "$REMOTE_HEAD" 2>/dev/null; then
            echo -e "${YELLOW}› Remote was force-pushed — cherry-picking local commits onto new tip...${NC}"
            OUR_COMMITS=$(git log --reverse --format=%H "$MERGE_BASE..HEAD" 2>/dev/null || git log --reverse --format=%H "origin/$COLLECTION_BRANCH..HEAD" 2>/dev/null)
            if [ -z "$OUR_COMMITS" ]; then
                echo -e "${DIM}› No local commits to preserve — resetting to remote${NC}"
                git reset --hard "origin/$COLLECTION_BRANCH" --quiet
                continue
            fi
            # Snapshot the local HEAD before resetting so a failed
            # cherry-pick can restore the original chain rather than
            # silently leaving a partial replay (which the next push
            # attempt would then ship as a wrong-content branch).
            PRE_RESET_SHA=$(git rev-parse HEAD)
            git reset --hard "origin/$COLLECTION_BRANCH" --quiet
            CHERRY_FAILED=false
            for commit in $OUR_COMMITS; do
                if ! git cherry-pick "$commit" --quiet 2>/dev/null; then
                    echo -e "${RED}✗${NC} Cherry-pick failed for $(git log --oneline -1 "$commit")"
                    git cherry-pick --abort 2>/dev/null || true
                    git reset --hard "$PRE_RESET_SHA" --quiet
                    echo -e "${YELLOW}› Restored original local chain at $(git rev-parse --short HEAD)${NC}"
                    CHERRY_FAILED=true
                    break
                fi
            done
            if [ "$CHERRY_FAILED" = true ]; then
                return 1
            fi
            echo -e "${GREEN}✓${NC} Rebased onto new remote tip"
            continue
        fi

        if git merge "origin/$COLLECTION_BRANCH" --no-edit --quiet 2>/dev/null; then
            echo -e "${GREEN}✓${NC} Merged remote changes"
        else
            echo -e "${RED}✗${NC} Merge conflict — aborting (no auto-resolve, to avoid silent overwrites)"
            git merge --abort 2>/dev/null || true
            return 1
        fi
    done
    echo -e "${RED}✗${NC} Push failed after 3 attempts"
    return 1
}

# Pick the next work item. Default: first dependency-free issue from the ready
# queue that carries the `dev: agent` label. With --resume: if any open
# `dev: agent` issue is already assigned to you, prefer it (orphaned
# in-progress recovery). The positive label gate is required — without it the
# operator must manually triage every backlog item to keep the loop from
# wandering into design / cross-repo / device-gated work.
pick_next_issue() {
    # Output format: <source>\t<number>\t<title>. The source field comes
    # first so the title (which may legally contain tab characters) sits
    # at the end and can be reconstructed via `cut -f3-`.
    if [ "$RESUME_IN_PROGRESS" = true ]; then
        # Capture stderr so a probe failure (rate-limit, auth) surfaces
        # rather than silently falling through to the ready queue and
        # claiming a NEW issue while the orphan stays orphaned.
        local resume_err resumed
        resume_err=$(mktemp /tmp/agent-loop-resume-err.XXXXXX)
        # `.[0] // empty` short-circuits the downstream pipeline on an
        # empty array so the script emits no output instead of a literal
        # "null" string concatenated with .title (which would then look
        # like a valid issue id to the caller).
        resumed=$(gh issue list --assignee @me --state open --label "dev: agent" --limit 100 \
                    --json number,title \
                    --jq '.[0] // empty | (.number|tostring) + "\t" + .title' \
                    2>"$resume_err")
        local resume_status=$?
        if [ $resume_status -ne 0 ]; then
            echo -e "${RED}✗${NC} Resume probe failed (gh exit $resume_status) — first stderr line:" >&2
            head -1 "$resume_err" | sed 's/^/    /' >&2
            rm -f "$resume_err"
            return 2
        fi
        rm -f "$resume_err"
        if [ -n "$resumed" ] && [ "$resumed" != "null" ] && [ "$resumed" != $'\t' ]; then
            local n title
            n=$(echo "$resumed" | cut -f1)
            title=$(echo "$resumed" | cut -f2-)
            echo "resume"$'\t'"$n"$'\t'"$title"
            return 0
        fi
    fi

    # ready.py --json prints `[{number, title, labels: [{name, ...}, ...], ...}, ...]`.
    # Capture stderr + exit so a `ready.py` failure (gh auth, rate limit)
    # surfaces as a hard error instead of being misread as "no issues".
    # `--unassigned` + `--agent` filters at the source so the queue only
    # contains positively-tagged candidates; `--limit 100` raises the
    # default 20 so longer queues don't false-report "no issues".
    local ready_err ready_json ready_status
    ready_err=$(mktemp /tmp/agent-loop-ready-err.XXXXXX)
    ready_json=$("$ISSUES_READY" --unassigned --agent --limit 100 --json 2>"$ready_err") || ready_status=$?
    ready_status=${ready_status:-0}
    if [ "$ready_status" -ne 0 ]; then
        echo -e "${RED}✗${NC} Ready probe failed (ready.py exit $ready_status) — first stderr line:" >&2
        head -1 "$ready_err" | sed 's/^/    /' >&2
        rm -f "$ready_err"
        return 2
    fi
    rm -f "$ready_err"

    # `.[0] // empty` ensures an empty queue produces no output instead of
    # a literal "null" that the caller might mistake for an issue id. The
    # `dev: agent` filter is applied upstream by `ready.py --agent`, so the
    # input here is already pre-filtered to agent-eligible candidates.
    local next
    next=$(echo "$ready_json" | jq -r '.[0] // empty | (.number|tostring) + "\t" + .title' 2>/dev/null)
    if [ -z "$next" ]; then
        return 1
    fi
    local n title
    n=$(echo "$next" | cut -f1)
    title=$(echo "$next" | cut -f2-)
    echo "ready"$'\t'"$n"$'\t'"$title"
    return 0
}

# Snapshot the closed-issue set before the loop starts. We compare against
# `gh issue list --state closed` after each iteration to record what got
# closed during the run (used in the final PR body).
LOOP_STARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Consecutive-failure counters. Permanent permission errors (claim) and
# upstream rejections (push) would otherwise spin the loop forever
# burning Claude tokens; bail after a few consecutive failures of the
# same kind. Reset to 0 on each successful step.
CLAIM_FAILURES=0
PUSH_FAILURES=0
MAX_CLAIM_FAILURES=3
MAX_PUSH_FAILURES=2

while [ $ITERATION -lt $MAX_ITERATIONS ]; do
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo -e "${YELLOW}› Dirty working tree — resetting to last commit...${NC}"
        git checkout -- . 2>/dev/null || true
        git clean -fd 2>/dev/null || true
    fi

    git fetch origin "$COLLECTION_BRANCH" --quiet 2>/dev/null || true
    if git rev-parse "origin/$COLLECTION_BRANCH" &>/dev/null; then
        LOCAL_HEAD=$(git rev-parse HEAD)
        REMOTE_HEAD=$(git rev-parse "origin/$COLLECTION_BRANCH")
        MERGE_BASE=$(git merge-base HEAD "origin/$COLLECTION_BRANCH" 2>/dev/null || echo "none")

        if [ "$MERGE_BASE" = "none" ] || ! git merge-base --is-ancestor "$MERGE_BASE" "$REMOTE_HEAD" 2>/dev/null; then
            echo -e "${YELLOW}› Remote branch was force-pushed — resetting to remote tip...${NC}"
            git reset --hard "origin/$COLLECTION_BRANCH" --quiet 2>/dev/null || true
        elif [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
            if ! git merge "origin/$COLLECTION_BRANCH" --no-edit --quiet 2>/dev/null; then
                git merge --abort 2>/dev/null || true
                echo -e "${YELLOW}⚠${NC} Pre-iteration merge conflict on origin/$COLLECTION_BRANCH — proceeding on stale base; the eventual push will surface this." >&2
            fi
        fi
    fi

    # `|| PICK_STATUS=$?` bypasses `set -e` for the substitution. Distinguish
    # status 1 (no issues, retry after sleep) from status 2 (resume probe
    # failed — already logged to stderr, abort).
    PICK_STATUS=0
    NEXT=$(pick_next_issue) || PICK_STATUS=$?
    if [ "$PICK_STATUS" -eq 2 ]; then
        exit 1
    fi
    if [ -z "$NEXT" ]; then
        echo -e "${DIM}○ No issues available. Waiting 20s for new work...${NC}"
        sleep 20
        ITERATION=$((ITERATION + 1))
        continue
    fi

    SOURCE=$(echo "$NEXT" | cut -f1)
    CLAIMED_ID=$(echo "$NEXT" | cut -f2)
    CLAIMED_TITLE=$(echo "$NEXT" | cut -f3-)

    bail_on_claim_failures() {
        CLAIM_FAILURES=$((CLAIM_FAILURES + 1))
        if [ "$CLAIM_FAILURES" -ge "$MAX_CLAIM_FAILURES" ]; then
            echo -e "${RED}✗${NC} $CLAIM_FAILURES consecutive claim failures — aborting (likely permanent: missing issues:write permission, locked issue, etc.)"
            exit 1
        fi
    }

    if [ "$SOURCE" = "ready" ]; then
        # `gh issue edit --add-assignee` is not atomic across parallel
        # callers; the script adds @me, then re-fetches to detect a race
        # (>1 assignee) and releases. A consistently-failing claim
        # (permission denied, locked issue) hits the consecutive-failure
        # bail at the top.
        if ! gh issue edit "$CLAIMED_ID" --add-assignee @me 2>/dev/null; then
            echo -e "${YELLOW}› Could not claim issue #$CLAIMED_ID${NC}"
            bail_on_claim_failures
            sleep 2
            continue
        fi

        # Distinguish "fetch failed" (transient gh error) from "0 assignees"
        # (race that nobody won) — both are non-progress signals; the
        # `|| echo "ERR"` fallback yields a non-numeric sentinel.
        assignee_count=$(gh issue view "$CLAIMED_ID" --json assignees --jq '[.assignees[].login] | length' 2>/dev/null || echo "ERR")
        if ! [[ "$assignee_count" =~ ^[0-9]+$ ]]; then
            echo -e "${YELLOW}› Could not verify claim on #$CLAIMED_ID (gh fetch failed) — releasing${NC}"
            gh issue edit "$CLAIMED_ID" --remove-assignee @me 2>/dev/null || true
            bail_on_claim_failures
            sleep 2
            continue
        fi
        if [ "$assignee_count" -gt 1 ]; then
            echo -e "${YELLOW}› Race detected on #$CLAIMED_ID (>1 assignee) — releasing${NC}"
            gh issue edit "$CLAIMED_ID" --remove-assignee @me 2>/dev/null || true
            bail_on_claim_failures
            sleep 2
            continue
        fi
        echo -e "${GREEN}✓${NC} Claimed issue: #$CLAIMED_ID $CLAIMED_TITLE"
    else
        echo -e "${YELLOW}› Resuming in-progress issue: ${NC}#$CLAIMED_ID $CLAIMED_TITLE"
    fi
    CLAIM_FAILURES=0

    ITERATION=$((ITERATION + 1))
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}▶${NC} /agent-loop iteration ${GREEN}$ITERATION${NC} of $MAX_ITERATIONS"
    echo "   Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "   Branch: $COLLECTION_BRANCH (worker $$)"
    echo "   Issue: #$CLAIMED_ID"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    echo -e "${BLUE}› Spawning Claude engineer for issue #$CLAIMED_ID...${NC}"
    echo ""

    # FIFO so claude runs as a tracked background process — the cleanup
    # trap can then kill it on Ctrl-C between read iterations. Use a
    # unique tempdir (`mktemp -d`) and place the FIFO inside; `mktemp -u`
    # is TOCTOU-racy because nothing reserves the path before mkfifo.
    ITER_TMPDIR=$(mktemp -d /tmp/agent-loop-iter.XXXXXX)
    FIFO="$ITER_TMPDIR/fifo"
    mkfifo "$FIFO"

    # Capture claude's stderr to a tempfile so auth/network/crash errors
    # surface on a nonzero exit. Suppressing stderr via `2>/dev/null`
    # would hide these and the loop would just see an empty FIFO.
    CLAUDE_ERR="$ITER_TMPDIR/claude.err"

    claude --chrome --permission-mode bypassPermissions --verbose --print \
        "Read @agent-loop-instructions.md and follow the instructions. Your assigned issue is #$CLAIMED_ID. Run 'gh issue view $CLAIMED_ID' to see the full description, then complete it." \
        --output-format stream-json > "$FIFO" 2> "$CLAUDE_ERR" &
    CLAUDE_PID=$!

    while read -r line; do
        type=$(echo "$line" | jq -r '.type // empty' 2>/dev/null)
        if [ "$type" = "assistant" ]; then
            echo "$line" | jq -r '.message.content[]? | select(.type == "text") | .text' 2>/dev/null | while IFS= read -r text; do
                [ -z "$text" ] && continue
                echo -e "${BLUE}▸${NC} $text"
            done
            echo "$line" | jq -c '.message.content[]? | select(.type == "tool_use")' 2>/dev/null | while read -r tool; do
                [ -z "$tool" ] && continue
                name=$(echo "$tool" | jq -r '.name' 2>/dev/null)
                input=$(echo "$tool" | jq -c '.input' 2>/dev/null)
                echo -e "${YELLOW}→${NC} ${CYAN}$name${NC} ${DIM}$input${NC}"
            done
        elif [ "$type" = "user" ]; then
            echo "$line" | jq -c '.message.content[]? | select(.type == "tool_result")' 2>/dev/null | while read -r result; do
                [ -z "$result" ] && continue
                is_error=$(echo "$result" | jq -r '.is_error // false' 2>/dev/null)
                content=$(echo "$result" | jq -r '
                    .content |
                    if type == "array" then
                        map(select(.type == "text") | .text) | join("\n")
                    elif type == "string" then
                        .
                    else
                        "..."
                    end
                ' 2>/dev/null | tr -d '\r' | head -n 20)
                if echo "$content" | grep -q '/9j/4AAQ\|data:image'; then
                    content="[image captured]"
                fi
                formatted=$(echo "$content" | sed -E "s/^([[:space:]]*[0-9]+)→/\x1b[2m\1\x1b[0m  /")
                if [ "$is_error" = "true" ]; then
                    echo ""
                    echo -e "${RED}✗${NC}"
                    echo -e "$formatted"
                else
                    echo ""
                    echo -e "${DIM}○${NC}"
                    echo -e "$formatted"
                fi
            done
        elif [ "$type" = "result" ]; then
            subtype=$(echo "$line" | jq -r '.subtype // empty' 2>/dev/null)
            result_text=$(echo "$line" | jq -r '.result // empty' 2>/dev/null)
            if [ "$subtype" = "success" ] && [ -n "$result_text" ]; then
                echo ""
                echo -e "${GREEN}✓${NC} $result_text"
            elif [ "$subtype" = "error" ]; then
                echo ""
                echo -e "${RED}✗${NC} $result_text"
            else
                echo -e "${DIM}? $line${NC}"
            fi
        elif [ "$type" != "system" ]; then
            echo -e "${DIM}? $line${NC}"
        fi
    done < "$FIFO"

    # `wait` on its own line under set -e would abort the script when
    # claude exited nonzero, before $? is captured. The `|| ...` form
    # keeps the assignment running.
    CLAUDE_EXIT=0
    wait "$CLAUDE_PID" || CLAUDE_EXIT=$?
    CLAUDE_PID=""
    if [ "$CLAUDE_EXIT" -ne 0 ] && [ -s "$CLAUDE_ERR" ]; then
        echo -e "${RED}✗${NC} claude exited $CLAUDE_EXIT — stderr (first 20 lines):"
        head -20 "$CLAUDE_ERR" | sed 's/^/    /'
    fi
    rm -rf "$ITER_TMPDIR"

    # Track newly closed issues since the loop started. `@tsv` escapes
    # embedded tabs/newlines in the title so a multi-line GitHub title
    # doesn't split one issue across two read iterations.
    while IFS=$'\t' read -r n title; do
        [ -z "$n" ] && continue
        ALREADY=false
        for existing in "${CLOSED_ISSUES[@]}"; do
            [ "$existing" = "#$n $title" ] && ALREADY=true && break
        done
        [ "$ALREADY" = false ] && CLOSED_ISSUES+=("#$n $title")
    done < <(gh issue list --state closed --search "closed:>=$LOOP_STARTED_AT" \
                --limit 100 --json number,title \
                --jq '.[] | [.number, .title] | @tsv' 2>/dev/null || true)

    echo -e "${DIM}› Pushing to collection branch...${NC}"
    if push_to_collection; then
        echo -e "${GREEN}✓${NC} Pushed to origin/$COLLECTION_BRANCH"
        PUSH_FAILURES=0
    else
        PUSH_FAILURES=$((PUSH_FAILURES + 1))
        echo -e "${YELLOW}⚠${NC} Push failed (#$PUSH_FAILURES of $MAX_PUSH_FAILURES) — work is committed locally"
        if [ "$PUSH_FAILURES" -ge "$MAX_PUSH_FAILURES" ]; then
            echo -e "${RED}✗${NC} $PUSH_FAILURES consecutive push failures — aborting (likely auth revoked, branch-protection rejection, or upstream-permanent issue)"
            exit 1
        fi
    fi

    echo ""
    echo -e "${GREEN}✓${NC} Iteration $ITERATION complete"
    echo ""
    sleep 2
done

# --- Post-loop: auto-PR ---
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}■${NC} /agent-loop finished"
echo "   Total iterations: $ITERATION"
echo "   Collection branch: $COLLECTION_BRANCH"
echo "   Ended: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Don't suppress stderr or swallow exit — opening a PR off a stale
# remote because the final push silently failed is worse than skipping
# the PR and surfacing the failure.
FINAL_PUSH_OK=true
if ! push_to_collection; then
    FINAL_PUSH_OK=false
    echo -e "${YELLOW}⚠${NC} Final push failed — skipping PR creation. Worktree preserved at $WORKTREE_DIR for manual recovery."
fi

COMMIT_COUNT=$(git rev-list --count "origin/$DEFAULT_BRANCH..origin/$COLLECTION_BRANCH" 2>/dev/null || echo "0")

if [ "$FINAL_PUSH_OK" = true ] && [ "$COMMIT_COUNT" -gt 0 ]; then
    echo -e "${BLUE}▸${NC} $COMMIT_COUNT commit(s) on $COLLECTION_BRANCH — creating PR"

    PR_BODY="## Summary\n\nAutonomous /agent-loop run — $ITERATION iteration(s), $COMMIT_COUNT commit(s).\n"

    if [ ${#CLOSED_ISSUES[@]} -gt 0 ]; then
        PR_BODY+="\n### Closed Issues\n"
        for issue in "${CLOSED_ISSUES[@]}"; do
            PR_BODY+="\n- $issue"
        done
        PR_BODY+="\n"
    fi

    PR_BODY+="\n### Commit Log\n\n\`\`\`\n"
    PR_BODY+=$(git log --oneline "origin/$DEFAULT_BRANCH..origin/$COLLECTION_BRANCH" 2>/dev/null || echo "(no commits)")
    PR_BODY+="\n\`\`\`\n"

    EXISTING_PR=$(gh pr list --head "$COLLECTION_BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")

    if [ -n "$EXISTING_PR" ] && [ "$EXISTING_PR" != "null" ]; then
        PR_URL=$(gh pr view "$EXISTING_PR" --json url --jq '.url' 2>/dev/null)
        echo -e "${GREEN}✓${NC} PR already exists: $PR_URL"
    else
        PR_ERR=$(mktemp /tmp/agent-loop-pr-err.XXXXXX)
        PR_URL=$(gh pr create \
            --base "$DEFAULT_BRANCH" \
            --head "$COLLECTION_BRANCH" \
            --title "agent-loop: $COLLECTION_BRANCH" \
            --body "$(echo -e "$PR_BODY")" \
            2>"$PR_ERR") || true

        if [ -n "$PR_URL" ]; then
            echo -e "${GREEN}✓${NC} PR created: $PR_URL"
        else
            echo -e "${YELLOW}⚠${NC} Could not create PR — first stderr line:"
            head -1 "$PR_ERR" | sed 's/^/    /'
        fi
        rm -f "$PR_ERR"
    fi
elif [ "$FINAL_PUSH_OK" = true ]; then
    echo -e "${DIM}○ No commits on $COLLECTION_BRANCH — skipping PR${NC}"
fi

echo ""
echo -e "${BLUE}▸${NC} Final ready queue:"
"$ISSUES_READY" 2>/dev/null | head -10 || echo "   (queue empty)"

cd "$PROJECT_DIR"
if [ "$FINAL_PUSH_OK" = true ]; then
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || true
    git branch -D "$LOCAL_BRANCH" 2>/dev/null || true
    echo -e "${GREEN}✓${NC} Worktree cleaned up"
fi

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
