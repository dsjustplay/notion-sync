# Notion Sync

Syncs Markdown (`.md`) files from a local directory to Notion, creating and updating pages while preserving your folder hierarchy.

## Features

**Sync (local → Notion)**
- Recursively finds Markdown files in any directory
- Converts Markdown syntax to Notion blocks (headings, lists, code, tables, images, links)
- Creates or updates Notion pages, preserving folder structure as nested pages
- Incremental sync — skips unchanged files using SHA-256 content hashes
- Block-level diffing — only changed blocks are updated, preserving comments and reactions
- Auto-archives Notion pages when the corresponding local file is deleted
- Dry-run mode — preview changes without writing to Notion
- `--root-is-file` — writes a root `.md` file's content directly to the target page, avoiding an extra nesting level

**Pull (Notion → local)**
- Downloads a Notion page tree or database to local Markdown files
- Auto-detects whether the root ID is a regular page or a database
- Preserves folder hierarchy matching the Notion page tree
- Downloads and saves embedded images locally under `assets/` subfolders
- Writes a `sync_state.json` ready for subsequent `sync` runs

## Requirements

- Python 3.13+
- pip (Comes pre-installed with Python, but can be updated using `python -m ensurepip --default-pip`)
- Notion API Token

## Installation

1. **Clone the repository**
   ```sh
   git clone git@github.com:dsjustplay/notion-sync.git
   cd notion-sync
   ```

2. **Create and activate a virtual environment**
   ```sh
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. **Install dependencies**
   ```sh
   pip install -r requirements.txt
   ```

## Configuration

### 1. Notion Integration Token

- Go to [Notion Integrations](https://www.notion.so/my-integrations) and create a new integration.
- Copy the `Internal Integration Token`.
- Copy `.env.example` to `.env` and fill in your token:
  ```
  NOTION_TOKEN=your_notion_token_here
  ```

### 2. Root Page ID

- Open the Notion page you want to sync under.
- Click **Share → Copy link**. The URL looks like:
  `https://www.notion.so/PageName-YOUR_PAGE_ID`
- The `YOUR_PAGE_ID` part is your root page ID.
- Make sure the integration has access to this page (**Share → Connect to**).

The root page ID is passed via CLI on the **first run** and stored automatically in `sync_state.json` for all subsequent runs:

```sh
python main.py sync <docs_dir> --root-page-id YOUR_PAGE_ID
```

Alternatively, copy the provided example into `<docs_dir>` and fill in your page ID before the first run:

```sh
cp sync_state.json.example <docs_dir>/sync_state.json
# then edit sync_state.json and replace "your-root-page-id-here"
```

### 3. Developer / safety settings (`config.py`)

`config.py` contains a small set of constants you can adjust without touching the core logic:

| Setting | Default | Description |
|---|---|---|
| `WRITES_DISABLED` | `False` | **Kill switch.** When `True`, every write operation (create, update, delete, archive, image upload) is silently skipped, regardless of `--dry-run`. Useful when developing or testing the tool against a live Notion workspace — flip to `False` only when you're ready for real changes. Unlike `--dry-run`, this affects all code paths uniformly and requires no CLI flag. |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout in seconds for all Notion API calls (connect + read). |
| `BLOCK_LIMIT` | `100` | Maximum number of blocks sent in a single `PATCH /blocks/{id}/children` request (Notion's API limit). |
| `MAX_BLOCK_TEXT_LENGTH` | `2000` | Maximum characters in a single rich-text run before it is split. |

## Usage

The tool has two subcommands:

```sh
python main.py sync <docs_dir> [--root-page-id PAGE_ID] [--dry-run] [--root-is-file]
python main.py pull <target_dir> --root-page-id PAGE_ID
```

### `sync` — push local Markdown to Notion

| Argument | Required | Description |
|---|---|---|
| `docs_dir` | Always | Path to the folder containing Markdown files to sync |
| `--root-page-id` | First run only | Notion page ID to sync under; saved to `sync_state.json` for subsequent runs |
| `--dry-run` | Optional | Preview what would be created, updated, or archived — no changes made to Notion |
| `--root-is-file` | Optional | Write the single root `.md` file's content directly to the target page instead of creating a child page for it (see [Root-is-file](#root-is-file)) |

```sh
# First run — provide the root page ID once
python main.py sync <docs_dir> --root-page-id YOUR_PAGE_ID

# Subsequent runs — root page ID is read from sync_state.json
python main.py sync <docs_dir>

# Preview changes without touching Notion
python main.py sync <docs_dir> --dry-run

# Sync with root file written directly to the target page
python main.py sync <docs_dir> --root-page-id YOUR_PAGE_ID --root-is-file
```

### `pull` — download Notion pages to local Markdown

| Argument | Required | Description |
|---|---|---|
| `target_dir` | Always | Local directory to write downloaded Markdown files into |
| `--root-page-id` | Always | Notion page or database ID to pull from |

```sh
python main.py pull <target_dir> --root-page-id YOUR_PAGE_ID
```

The pull command auto-detects whether the root ID is a **regular page** or a **database**:

- **Page**: downloads the page and its full child-page tree.
- **Database**: downloads the database's own content blocks as the root file, then each database row and its child-page subtree, placed in a subfolder named after the database.

Images are saved locally under `assets/` subfolders. A `sync_state.json` is written and is ready for subsequent `sync` runs.

## How it works

### Two-phase sync

Every run processes files in two phases to ensure internal links always resolve correctly.

**Phase 1 — Create page structure.**
For every `.md` file, the tool ensures a Notion page exists (title only, no content yet) using [`POST /v1/pages`](https://developers.notion.com/reference/post-page). At the end of Phase 1 it has a complete `filename → Notion URL` map covering all pages.

**Phase 2 — Sync content.**
Each file is re-read, its internal `.md` links are rewritten to Notion URLs (using the Phase 1 map), and the content is uploaded. The split is necessary because a link to page B can only be resolved once page B already exists.

### Content hashing

A SHA-256 of the full markdown text is stored in `sync_state.json` for each file. On subsequent runs:
- Hash **unchanged** → file is skipped, no Notion API call.
- Hash **changed or absent** → content is uploaded.

### Block-level diffing

For pages with existing content, the tool:
1. Fetches current blocks via [`GET /v1/blocks/{page_id}/children`](https://developers.notion.com/reference/get-block-children).
2. Computes a fingerprint for each block (type + text content).
3. Runs Python's `SequenceMatcher` to produce a minimal diff (keep / delete / insert / replace).
4. Executes only what changed: [`DELETE /v1/blocks/{block_id}`](https://developers.notion.com/reference/delete-a-block) for removed blocks, [`PATCH /v1/blocks/{page_id}/children`](https://developers.notion.com/reference/patch-block-children) with an `after` cursor for insertions.

If new content must be inserted before the very first block (Notion has no `before` parameter), the tool falls back to deleting all blocks and re-uploading the full page.

Brand-new pages (just created in Phase 1) skip the fetch and diff entirely — content is uploaded directly.

### Child page handling

Notion pages can have **child pages** (sub-pages) that always appear as `child_page` blocks at the bottom of the parent page. The tool treats these as Notion-owned structural elements and never touches them:

- **Pull**: `child_page` blocks are **not** converted to inline markdown links. Child pages are downloaded as separate `.md` files; no redundant links are emitted in the parent file.
- **Sync**: `child_page` blocks are **excluded from the diff** on both sides — they are invisible to the diff engine and are never deleted, replaced, or re-inserted as paragraph blocks.

### External archive / delete recovery

If a Notion page is archived or deleted directly in Notion (outside of this tool), the tool detects the missing page on the next sync, drops the stale entry from `sync_state.json`, and re-creates the page from scratch. No manual state cleanup is required.

### Image upload

Images are uploaded via Notion's [File Upload API](https://developers.notion.com/reference/create-a-file-upload) in two steps:
1. **`POST /v1/file_uploads`** — creates an upload object and returns an `upload_url` and `file_upload_id`.
2. **`POST {upload_url}`** — sends the image bytes as `multipart/form-data`.

The `file_upload_id` and a SHA-256 of the image file are cached in `sync_state.json`. Unchanged images reuse the cached ID — no re-upload. Changed images repeat the two-step process and replace the old block.

### Internal link resolution

When rewriting `.md` links to Notion URLs, three passes are tried in order (first match wins):
1. **Direct match** — filename matches exactly (after URL-decoding).
2. **UUID strip** — removes the 32-character hex suffix Notion appends to exported filenames (e.g. `Page 3a9f...abcd.md` → `Page.md`).
3. **Slug match** — normalises both sides to lowercase + hyphens (e.g. `fraud-score-system.md` matches `Fraud Score System.md`).

Unresolved links are left as plain text.

### Root-is-file

By default every `.md` file in `docs_dir` becomes a **child page** of the target Notion page. This works well for flat collections but creates an unwanted extra level of nesting when your directory looks like:

```
docs/
  Fraud Control.md        ← overview / intro content
  Fraud Control/
    Earnings reductions.md
    User verification.md
    ...
```

Without `--root-is-file`, pushing this creates:
```
Target page
  └── Fraud Control       ← extra child page, target page itself is empty
        ├── Earnings reductions
        └── ...
```

With `--root-is-file`, the root `.md` file's content is written **directly to the target page** and the subfolder's pages become its direct children:
```
Target page               ← content of Fraud Control.md lives here
  ├── Earnings reductions
  └── ...
```

**Requirements for `--root-is-file`:**
- Exactly **one** `.md` file at the root of `docs_dir`.
- A subfolder with the **same stem** as that file (e.g. `Fraud Control.md` + `Fraud Control/`).
- The target root page must be a **regular Notion page**, not a standalone database. The Notion API does not allow appending content blocks directly to a database object — only rows can be created under it.

If the first two conditions are not met the flag is ignored with a warning and normal behaviour applies. If the third condition is not met (database root), the tool exits immediately with an error (see [Expected output — incompatible root type](#sync----root-is-file-with-a-database-root)).

> **Tip:** `pull` always produces this paired structure when the source is a Notion database, so `--root-is-file` is the natural companion flag for a pull → sync round-trip. Point the subsequent sync at a regular Notion **page** (not the database itself) as the root.

### Markdown elements supported

Headings (H1–H3), paragraphs, bullet and numbered lists (up to 3 levels of nesting), to-do items, blockquotes, code blocks (with syntax highlighting), tables (chunked at 100 rows per Notion limit), horizontal rules, inline formatting (bold, italic, bold-italic, strikethrough, inline code, hyperlinks), and images.

---

## Expected output

### `sync` — first run (cold start — no `sync_state.json`)

```sh
python main.py sync <docs_dir> --root-page-id YOUR_PAGE_ID
```
```
No local state found. Discovering existing Notion pages (one-time)...
Fetching All Notion Pages to compare with local...
Reconciled 0 page(s) and 0 folder(s) from Notion.
Found 21 Markdown file(s). Starting sync...
Creating new Notion page: Fraud Control
Mapped Fraud Control.md -> https://www.notion.so/Fraud-Control-<page_id>
...
Page 'Fraud Control.md' content has changed. Syncing...
Successfully updated Notion page (ID: <page_id>)
...

======Sync Summary======
Total Files: 21
Create: 21
Update: 21
All files synced successfully!

Total time taken: 0h 4m 19.00s
```

### `sync` — subsequent run, nothing changed

```sh
python main.py sync <docs_dir>
```
```
Found 21 Markdown file(s). Starting sync...
Mapped Fraud Control.md -> https://www.notion.so/Fraud-Control-<page_id>
...
Page 'Fraud Control.md' unchanged. Skipping.
...

======Sync Summary======
Total Files: 21
Skipped: 21 (Already up to date)
All files synced successfully!

Total time taken: 0h 0m 8.00s
```

### `sync` — subsequent run, some files changed

```sh
python main.py sync <docs_dir>
```
```
Found 21 Markdown file(s). Starting sync...
Mapped Fraud Control.md -> https://www.notion.so/Fraud-Control-<page_id>
...
Page 'Fraud Control.md' unchanged. Skipping.
Page 'User verification.md' content has changed. Syncing...
Successfully updated Notion page (ID: <page_id>)
...

======Sync Summary======
Total Files: 21
Update: 3
Skipped: 18 (Already up to date)
All files synced successfully!

Total time taken: 0h 0m 35.00s
```

### `sync` — dry run

```sh
python main.py sync <docs_dir> --dry-run
```
```
DRY RUN — no changes will be made to Notion.

Found 21 Markdown file(s). Starting sync...
[dry] Would create: 'New Page'
  [dry] diff — keep: 45, delete: 2, insert: 3
...

======Dry Run Sync Summary======
Total Files: 21
[dry] Would Create: 1
[dry] Would Update: 3
Skipped: 17 (Already up to date)
Dry run complete — no changes made to Notion.

Total time taken: 0h 0m 6.00s
```

### `sync` — a local file was deleted

```sh
python main.py sync <docs_dir>
```
```
Detected missing file: Fraud Control/old-page.md | Archiving Notion page...
Successfully archived Notion page (ID: <page_id>)
Archived 1 missing page(s).
Found 20 Markdown file(s). Starting sync...
...
```

### `sync` — with `--root-is-file`

```sh
python main.py sync <docs_dir> --root-page-id YOUR_PAGE_ID --root-is-file
```
```
Found 21 Markdown file(s). Starting sync...
Page 'Fraud Control.md' content has changed. Syncing to root page...
Creating new Notion page: Earnings reductions
...

======Sync Summary======
Total Files: 21
Create: 20
Update: 1
All files synced successfully!
```

### `sync` — `--root-is-file` with a database root

If the target root ID points to a **standalone Notion database** (one that has no surrounding page layer), the tool cannot write the root `.md` file's content there and exits immediately:

```sh
python main.py sync <docs_dir> --root-page-id <database_id> --root-is-file
```
```
Root type detected: database
Error: --root-is-file is not compatible with a standalone Notion database.
A standalone database has no page layer to write the root .md content to.
Use a database that is embedded inside a Notion page (open the database, click '···' → 'Open as page', then share that page), or omit --root-is-file and let each .md file become a database row.
```

No pages are created or modified. To resolve:
- **Option A:** Point the sync at a regular Notion **page** instead of the database.
- **Option B:** Drop `--root-is-file` — the root `.md` file will become a database row like all other files.

### `pull` — from a page tree

```sh
python main.py pull <target_dir> --root-page-id YOUR_PAGE_ID
```
```
Pulling from Notion YOUR_PAGE_ID into <target_dir> ...
Root page: Fraud Control
Downloaded: Fraud Control.md
Downloaded: Fraud Control/Fraud Score System.md
Downloaded: Fraud Control/User verification.md
...

Pull complete — 21 page(s) downloaded to <target_dir>
```

### `pull` — from a database

```sh
python main.py pull <target_dir> --root-page-id bb865e41690d4489b3e928aefa49cace
```
```
Pulling from Notion bb865e41690d4489b3e928aefa49cace into <target_dir> ...
Database: Fraud Control
Downloaded: Fraud Control.md
Downloaded: Fraud Control/Earnings reductions.md
Downloaded: Fraud Control/User verification.md
Downloaded: Fraud Control/User verification/Face check (facetec).md
...

Pull complete — 28 page(s) downloaded to <target_dir>
```

## File Structure

```
notion-sync/
├── main.py                    # Entry point; routes sync and pull subcommands
├── notion_api.py              # Notion API calls (create, update, diff, archive)
├── notion_to_md.py            # Notion blocks → Markdown conversion and pull orchestration
├── markdown_parser.py         # Markdown → Notion block conversion (sync direction)
├── sync_state.py              # Local state management (sync_state.json)
├── image_uploader.py          # Image upload via Notion file upload API
├── config.py                  # Constants, environment variable loading, kill switch
├── utils.py                   # File discovery helpers
├── strip_notion_ids.py        # One-off utility: strips Notion UUID suffixes from exported files
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
└── sync_state.json.example    # State file template (copy into <docs_dir>)
```

### `strip_notion_ids.py` — clean up Notion exports

When you export pages from the Notion UI, filenames and folder names contain a 32-character UUID suffix (e.g. `Fraud Score System 3139c23278f580b1bfe0d97b2eb12a60.md`). This one-off utility strips those suffixes, renames the files and folders, rewrites all internal cross-page links, and updates `sync_state.json` so the sync tool keeps its mappings intact.

```sh
# Preview renames without touching the filesystem
python strip_notion_ids.py <docs_dir> --dry-run

# Apply
python strip_notion_ids.py <docs_dir>
```

Run this **once** after a Notion export, before the first `sync`. It is not needed when using the `pull` command, which never adds UUID suffixes.

## Notes

- Ensure your Notion integration has **edit access** to the root page (**Share → Connect to**).
- `sync_state.json` is auto-generated inside `<docs_dir>` and never lives in the project folder. If `<docs_dir>` is version-controlled, commit `sync_state.json` — it tracks the mapping between local files and Notion page IDs and should be shared across machines.
- **Forcing a full re-sync:** if you change the markdown-to-Notion rendering logic or suspect Notion's content is out of sync with the local files, clear the content hashes to force a re-upload: set all `content_hash` values in `sync_state.json` to `null`, or delete the file entirely (page ID mappings will be re-discovered automatically on the next run).
- **Kill switch during development:** set `WRITES_DISABLED = True` in `config.py` to block all Notion writes globally. Unlike `--dry-run` this requires no CLI flag and guards against accidental writes even when calling internal APIs directly. Remember to set it back to `False` before a real sync run.