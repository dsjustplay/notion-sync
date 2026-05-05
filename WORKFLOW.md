# Notion Sync — Team Workflow

This document describes the **intended day-to-day procedure** for keeping documentation in sync with Notion. For CLI options and technical details see [README.md](README.md).

---

## Core principle

Documentation lives in the same repository as the code it describes. Markdown files are edited alongside code changes, committed in the same pull request, and pushed to Notion when the PR lands. `sync_state.json` travels with the repo and is committed like any other file — it is the persistent bridge between the local files and the Notion workspace.

---

## Setup (first time, per repo)

1. Choose how you will run the tool: **Docker** (recommended — no Python required) or **directly** (requires Python 3.11+).
2. Store the Notion integration token as a CI secret (`NOTION_TOKEN`).
3. Run a first `pull` to bootstrap the docs folder and `sync_state.json` (see the [Running with Docker](#running-with-docker) or [Running directly](#running-directly) sections below for the exact command).
4. Commit both the downloaded Markdown files and `sync_state.json` to the repo.

From this point on, `sync_state.json` is always committed. Do **not** add it to `.gitignore`.

---

## Running with Docker

The published image `ghcr.io/dsjustplay/notion-sync` bundles Python and all dependencies — no local install required.

**How it works**: mount your docs folder to the fixed container path `/workspace`, pass your Notion token as an environment variable, then append the normal CLI subcommand and flags:

```sh
docker run --rm \
  -e NOTION_TOKEN=<your_token> \
  -v /full/path/to/your/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  <subcommand> /workspace [flags]
```

### Common commands (Docker)

```sh
# Bootstrap a new repo (first pull — writes files to disk)
docker run --rm \
  -e NOTION_TOKEN=<your_token> \
  -v /full/path/to/your/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  pull /workspace --root-page-id <PAGE_ID> --apply

# Subsequent pulls (root page ID read from sync_state.json)
docker run --rm \
  -e NOTION_TOKEN=<your_token> \
  -v /full/path/to/your/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  pull /workspace --apply

# Dry-run push (default — no changes written to Notion)
docker run --rm \
  -e NOTION_TOKEN=<your_token> \
  -v /full/path/to/your/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  push /workspace

# Apply push
docker run --rm \
  -e NOTION_TOKEN=<your_token> \
  -v /full/path/to/your/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  push /workspace --apply
```

### Using the image in a GitHub Actions CI job

```yaml
- name: Push docs to Notion
  run: |
    docker run --rm \
      -e NOTION_TOKEN=${{ secrets.NOTION_TOKEN }} \
      -v ${{ github.workspace }}/docs:/workspace \
      ghcr.io/dsjustplay/notion-sync \
      push /workspace --apply
```

---

## Running directly

Requires Python 3.11+ and the dependencies from `requirements.txt`.

```sh
# Install dependencies (first time)
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Set the Notion token
echo "NOTION_TOKEN=<your_token>" > .env
```

Then use `python main.py <subcommand> …` as shown in the [Quick reference](#quick-reference) section below.

---

## Day-to-day: editing documentation

Always follow a **pull → edit → push** discipline, mirroring the `git pull` before commit habit:

```
1. git pull origin main          # get the latest state
2. Edit .md files alongside code changes
3. Commit everything (code + docs) in your PR
4. Sync to Notion (see below)
```

If you skip the pull and edit on a stale branch, the next push may overwrite Notion edits made by a merged PR between your checkout and your push. The [drift detection](#drift-detection) feature will catch this and warn you before overwriting.

---

## Syncing to Notion — two approaches

### A. Post-merge CI (recommended)

The push runs automatically on `main` after every PR is merged. CI then commits the updated `sync_state.json` back to `main`.

**Pros:** always runs on the latest merged state; no risk of staleness from concurrent PRs.  
**Cons:** produces a follow-up commit on `main` outside the PR.

Example CI step (after merge to `main`):
```sh
# With Docker (recommended):
docker run --rm \
  -e NOTION_TOKEN=$NOTION_TOKEN \
  -v $PWD/<docs_dir>:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  push /workspace --apply

# Or directly:
python main.py push <docs_dir> --apply [--root-is-file]

git config user.email "ci-bot@yourorg.com"
git config user.name "CI Bot"
git add <docs_dir>/sync_state.json
git commit -m "ci: push docs to Notion" || echo "Nothing to commit"
git push
```

---

### B. Pre-merge, inside the PR (alternative)

Push right before squash-merging, so the push commit is part of the PR itself. This works cleanly with a rebase-before-squash workflow: rebase onto `main`, run the push, commit, then squash-merge.

**Pros:** the PR is self-contained — sync state and code land together.
**Cons:** if another PR merges between your push and your merge, `sync_state.json` may be slightly stale (missing the other PR's new page IDs/hashes). This is usually harmless since concurrent PRs rarely touch the same pages, but worth being aware of.

```sh
git fetch origin && git rebase origin/main
python main.py push <docs_dir> --apply [--root-is-file]
git add <docs_dir>/sync_state.json
git commit -m "push: update Notion state"
# squash-merge via GitHub UI
```

---

## Drift detection

After every successful push, the tool fetches the page's new `last_edited_time` from Notion and stores it in `sync_state.json`. Before Phase 2 of a push run, every file whose local content has changed is checked: the stored timestamp is compared to Notion's current `last_edited_time`.

- **Match** — Notion has not been touched since the last push. Safe to proceed.
- **Mismatch** — someone edited the page directly in Notion since the last pull.

**Dry-run** (`push` without `--apply`): the check runs and prints a warning for each drifted page, but the rest of the dry-run output continues so you can see the full picture:

```
Warning: the following page(s) were edited in Notion since your last pull:
  Fraud Control/User verification.md
    last known: 2026-05-01T10:00:00.000Z
    notion now: 2026-05-04T08:26:00.000Z
Run 'pull' to get the latest changes before pushing. Use --force to overwrite Notion anyway.
```

**Apply** (`push --apply`): if any drift is detected, the tool **aborts before writing anything** and lists the affected files:

```
Aborting: the following page(s) were edited in Notion since your last pull:
  Fraud Control/User verification.md
    last known: 2026-05-01T10:00:00.000Z
    notion now: 2026-05-04T08:26:00.000Z
Run 'pull' to get the latest changes before pushing. Use --force to overwrite Notion anyway.
```

No pages are written. Resolve before retrying:

**Resolution options:**

| Option | When to use |
|---|---|
| `pull --apply` first, review, then push | The Notion edit contains useful content you want to keep |
| Re-run with `--force` | The Notion edit is stale or intentionally being replaced by the local version |

```sh
# Accept local version, overwrite Notion
python main.py push <docs_dir> --apply --force
```

`pull` reseeds the drift baseline — after a pull the next push starts clean regardless of what was in Notion before.

---

## Handling Notion-side edits

**If only Notion changes** (nobody touched the local file): the local content hash matches, the page is skipped, and Notion edits are preserved. No action needed.

**If both sides change** (local file edited and someone also edited Notion directly): drift detection catches it on the next push and skips the affected page with a warning. Resolve by pulling first or using `--force`.

The safest policy for teams: **treat Notion as read-only for humans**. Use Notion for reading and commenting; all edits go through the repo. Direct Notion edits are fine for quick fixes but should be followed by a `pull` to bring local back in sync.

---

## Quick reference

In all Docker examples below, replace `/full/path/to/docs` with the absolute path to your docs folder on the host. The container always uses `/workspace`.

### Docker

```sh
# Bootstrap a new repo (first pull — root-page-id only needed once)
docker run --rm -e NOTION_TOKEN=<token> \
  -v /full/path/to/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  pull /workspace --root-page-id <PAGE_ID> --apply

# Subsequent pulls
docker run --rm -e NOTION_TOKEN=<token> \
  -v /full/path/to/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  pull /workspace --apply

# Dry-run pull (default — nothing written)
docker run --rm -e NOTION_TOKEN=<token> \
  -v /full/path/to/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  pull /workspace --diff

# Dry-run push (default — no Notion changes)
docker run --rm -e NOTION_TOKEN=<token> \
  -v /full/path/to/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  push /workspace

# Apply push
docker run --rm -e NOTION_TOKEN=<token> \
  -v /full/path/to/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  push /workspace --apply

# Overwrite Notion even if drift detected
docker run --rm -e NOTION_TOKEN=<token> \
  -v /full/path/to/docs:/workspace \
  ghcr.io/dsjustplay/notion-sync \
  push /workspace --apply --force
```

### Directly (Python 3.11+)

```sh
# Bootstrap a new repo (actually write files to disk; --root-page-id only needed once)
python main.py pull <docs_dir> --root-page-id <PAGE_ID> --apply

# Subsequent pulls — root page ID is read from sync_state.json
python main.py pull <docs_dir> --apply

# Preview what would be downloaded (dry run — default, nothing written)
python main.py pull <docs_dir>

# Preview with line-level diffs for changed pages
python main.py pull <docs_dir> --diff

# Preview what would be pushed (dry run — default, no Notion changes)
# Also runs the drift pre-flight check and warns about any Notion-side edits.
python main.py push <docs_dir> [--root-is-file]

# Everyday push (CI or pre-merge) — actually push to Notion
# Aborts before any write if drift is detected; see Drift detection above.
python main.py push <docs_dir> --apply [--root-is-file]

# Overwrite Notion even if drift is detected
python main.py push <docs_dir> --apply --force

# Recover after someone edited Notion directly
python main.py pull <docs_dir> --apply
# review diffs, then push
python main.py push <docs_dir> --apply
```
