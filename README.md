# Notion Sync

Syncs Markdown (`.md`) files from a local directory to Notion, creating and updating pages while preserving your folder hierarchy.

## Features

- Recursively finds Markdown files in any directory
- Converts Markdown syntax to Notion blocks (headings, lists, code, tables, images, links)
- Creates or updates Notion pages, preserving folder structure as nested pages
- Incremental sync — skips unchanged files using SHA-256 content hashes
- Block-level diffing — only changed blocks are updated, preserving comments and reactions
- Auto-archives Notion pages when the corresponding local file is deleted
- Dry-run mode — preview changes without writing to Notion

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
  `https://www.notion.so/PageName-19632a12f848273458356deccd685c23b`
- The `19632a12f848273458356deccd685c23b` part is your root page ID.
- Make sure the integration has access to this page (**Share → Connect to**).

The root page ID is passed via CLI on the **first run** and stored automatically in `sync_state.json` for all subsequent runs:

```sh
python main.py <docs_dir> --root-page-id 19632a12f848273458356deccd685c23b
```

Alternatively, copy the provided example into `<docs_dir>` and fill in your page ID before the first run:

```sh
cp sync_state.json.example <docs_dir>/sync_state.json
# then edit sync_state.json and replace "your-root-page-id-here"
```

## Usage

The tool has two subcommands:

```sh
python main.py sync <docs_dir> [--root-page-id PAGE_ID] [--dry-run]
python main.py pull <target_dir> --root-page-id PAGE_ID
```

### `sync` — push local Markdown to Notion

| Argument | Required | Description |
|---|---|---|
| `docs_dir` | Always | Path to the folder containing Markdown files to sync |
| `--root-page-id` | First run only | Notion page ID to sync under; saved to `sync_state.json` for subsequent runs |
| `--dry-run` | Optional | Preview what would be created, updated, or archived — no changes made to Notion |

```sh
# First run — provide the root page ID once
python main.py sync <docs_dir> --root-page-id 19632a12f848273458356deccd685c23b

# Subsequent runs — root page ID is read from sync_state.json
python main.py sync <docs_dir>

# Preview changes without touching Notion
python main.py sync <docs_dir> --dry-run
```

### `pull` — download Notion pages to local Markdown

| Argument | Required | Description |
|---|---|---|
| `target_dir` | Always | Local directory to write downloaded Markdown files into |
| `--root-page-id` | Always | Notion page ID to pull from |

```sh
python main.py pull <target_dir> --root-page-id 19632a12f848273458356deccd685c23b
```

Downloads the full page tree, saves images locally under `assets/` subfolders, and writes a `sync_state.json` ready for subsequent `sync` runs.

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

### Markdown elements supported

Headings (H1–H3), paragraphs, bullet and numbered lists (up to 3 levels of nesting), to-do items, blockquotes, code blocks (with syntax highlighting), tables (chunked at 100 rows per Notion limit), horizontal rules, inline formatting (bold, italic, bold-italic, strikethrough, inline code, hyperlinks), and images.

---

## Expected output

### `sync` — first run (cold start — no `sync_state.json`)

```sh
python main.py sync <docs_dir> --root-page-id 19632a12f848273458356deccd685c23b
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

### `pull`

```sh
python main.py pull <target_dir> --root-page-id 19632a12f848273458356deccd685c23b
```
```
Pulling from Notion page 19632a12f848273458356deccd685c23b into <target_dir> ...
Root page: Fraud Control
Downloaded: Fraud Control.md
Downloaded: Fraud Control/Fraud Score System.md
Downloaded: Fraud Control/User verification.md
...

Pull complete — 21 page(s) downloaded to <target_dir>
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
├── config.py                  # Constants and environment variable loading
├── utils.py                   # File discovery helpers
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
└── sync_state.json.example    # State file template (copy into <docs_dir>)
```

## Notes

- Ensure your Notion integration has **edit access** to the root page (**Share → Connect to**).
- `sync_state.json` is auto-generated inside `<docs_dir>` and never lives in the project folder. If `<docs_dir>` is version-controlled, commit `sync_state.json` — it tracks the mapping between local files and Notion page IDs and should be shared across machines.
- If you change the markdown-to-Notion rendering logic, clear the content hashes in `sync_state.json` to force a full re-upload (set all `content_hash` values to `null` or delete the file).