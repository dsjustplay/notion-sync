# Notion Sync — Team Workflow

This document describes the **intended day-to-day procedure** for keeping documentation in sync with Notion. For CLI options and technical details see [README.md](README.md).

---

## Core principle

Documentation lives in the same repository as the code it describes. Markdown files are edited alongside code changes, committed in the same pull request, and pushed to Notion when the PR lands. `sync_state.json` travels with the repo and is committed like any other file — it is the persistent bridge between the local files and the Notion workspace.

---

## Setup (first time, per repo)

1. Add `notion-sync` as a dependency of your CI pipeline (e.g. as a submodule, a pinned install, or a shared action).
2. Store the Notion integration token as a CI secret (`NOTION_TOKEN`).
3. Run a first `pull` to bootstrap the docs folder and `sync_state.json`:
   ```sh
   python main.py pull <docs_dir> --root-page-id <PAGE_ID> --apply
   ```
4. Commit both the downloaded Markdown files and `sync_state.json` to the repo.

From this point on, `sync_state.json` is always committed. Do **not** add it to `.gitignore`.

---

## Day-to-day: editing documentation

Always follow a **pull → edit → sync** discipline, mirroring the `git pull` before commit habit:

```
1. git pull origin main          # get the latest state
2. Edit .md files alongside code changes
3. Commit everything (code + docs) in your PR
4. Sync to Notion (see below)
```

If you skip the pull and edit on a stale branch, the next sync may overwrite Notion edits made by a merged PR between your checkout and your push. The [drift detection](#drift-detection) feature will catch this and warn you before overwriting.

---

## Syncing to Notion — two approaches

### A. Post-merge CI (recommended)

The sync runs automatically on `main` after every PR is merged. CI then commits the updated `sync_state.json` back to `main`.

**Pros:** always runs on the latest merged state; no risk of staleness from concurrent PRs.  
**Cons:** produces a follow-up commit on `main` outside the PR.

Example CI step (after merge to `main`):
```sh
python main.py sync <docs_dir> --apply [--root-is-file]
git config user.email "ci-bot@yourorg.com"
git config user.name "CI Bot"
git add <docs_dir>/sync_state.json
git commit -m "ci: sync docs to Notion" || echo "Nothing to commit"
git push
```

---

### B. Pre-merge, inside the PR (alternative)

Sync right before squash-merging, so the sync commit is part of the PR itself. This works cleanly with a rebase-before-squash workflow: rebase onto `main`, run the sync, commit, then squash-merge.

**Pros:** the PR is self-contained — sync state and code land together.  
**Cons:** if another PR merges between your sync and your merge, `sync_state.json` may be slightly stale (missing the other PR's new page IDs/hashes). This is usually harmless since concurrent PRs rarely touch the same pages, but worth being aware of.

```sh
git fetch origin && git rebase origin/main
python main.py sync <docs_dir> --apply [--root-is-file]
git add <docs_dir>/sync_state.json
git commit -m "sync: update Notion state"
# squash-merge via GitHub UI
```

---

## Drift detection

The tool stores a fingerprint of Notion's content after each sync. Before overwriting a page, it fetches the current Notion blocks and compares them to the stored fingerprint:

- **Match** — Notion has not been touched since the last sync. Safe to proceed.
- **Mismatch** — someone edited the page directly in Notion. The page is **skipped** and a warning is printed.

```
Remote drift detected on 'User verification.md': Notion was edited directly since last sync.
Run with --force to overwrite Notion, or pull first to merge the changes.
```

**Resolution options:**

| Option | When to use |
|---|---|
| `pull` first, review changes, then sync | The Notion edit contains useful content you want to keep |
| Re-run with `--force` | The Notion edit is stale or intentionally being replaced by the local version |

```sh
# Accept local version, overwrite Notion
python main.py sync <docs_dir> --apply --force
```

Pull also reseeds the drift baseline, so after a pull the next sync starts clean regardless of what was in Notion before.

---

## Handling Notion-side edits

**If only Notion changes** (nobody touched the local file): the local content hash matches, the page is skipped, and Notion edits are preserved. No action needed.

**If both sides change** (local file edited and someone also edited Notion directly): drift detection catches it on the next sync and skips the affected page with a warning. Resolve by pulling first or using `--force`.

The safest policy for teams: **treat Notion as read-only for humans**. Use Notion for reading and commenting; all edits go through the repo. Direct Notion edits are fine for quick fixes but should be followed by a `pull` to bring local back in sync.

---

## Quick reference

```sh
# Bootstrap a new repo (actually write files to disk)
python main.py pull <docs_dir> --root-page-id <PAGE_ID> --apply

# Preview what would be downloaded (dry run — default, nothing written)
python main.py pull <docs_dir> --root-page-id <PAGE_ID>

# Preview what would be synced (dry run — default, no Notion changes)
python main.py sync <docs_dir> [--root-is-file]

# Everyday sync (CI or pre-merge) — actually push to Notion
python main.py sync <docs_dir> --apply [--root-is-file]

# Overwrite Notion even if drift is detected
python main.py sync <docs_dir> --apply --force

# Recover after someone edited Notion directly
python main.py pull <docs_dir> --root-page-id <PAGE_ID> --apply
# review diffs, then sync
python main.py sync <docs_dir> --apply
```
