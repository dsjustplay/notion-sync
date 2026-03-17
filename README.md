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
python main.py docs --root-page-id 19632a12f848273458356deccd685c23b
```

Alternatively, copy the provided example and fill in your page ID before the first run:

```sh
cp sync_state.json.example docs/sync_state.json
# then edit docs/sync_state.json and replace "your-root-page-id-here"
```

## Usage

```sh
python main.py <docs_dir> [--root-page-id PAGE_ID] [--dry-run]
```

| Argument | Required | Description |
|---|---|---|
| `docs_dir` | Always | Path to the folder containing Markdown files |
| `--root-page-id` | First run only | Notion page ID to sync under; saved to `sync_state.json` for subsequent runs |
| `--dry-run` | Optional | Preview what would be created, updated, or archived — no changes made to Notion |

### Examples

```sh
# First run — provide the root page ID once
python main.py docs --root-page-id 19632a12f848273458356deccd685c23b

# Subsequent runs — root page ID is read from sync_state.json
python main.py docs

# Preview changes without touching Notion
python main.py docs --dry-run
```

## How it works

1. **Cold start**: If `sync_state.json` is missing or empty, the tool walks the live Notion tree under the root page and reconciles any already-existing pages with your local files — nothing gets duplicated.
2. **Incremental sync**: Files are compared by SHA-256 hash. Unchanged files are skipped. Changed files are diffed block-by-block; only modified blocks are updated.
3. **Deletion**: Delete a local `.md` file and the next sync archives the corresponding Notion page automatically.
4. **Re-creation**: If a Notion page is manually deleted or archived, the tool detects the 404 and re-creates it on the next run.

## File Structure

```
notion-sync/
├── main.py                    # Entry point and sync orchestration
├── notion_api.py              # Notion API calls (create, update, diff, archive)
├── markdown_parser.py         # Markdown → Notion block conversion
├── sync_state.py              # Local state management (sync_state.json)
├── image_uploader.py          # Image upload via Notion file upload API
├── config.py                  # Constants and environment variable loading
├── utils.py                   # File discovery helpers
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
├── sync_state.json.example    # State file template (copy into your docs dir)
└── docs/                      # Your docs directory (not committed)
    └── sync_state.json        # Auto-generated; tracks page IDs and content hashes
```

## Notes

- Ensure your Notion integration has **edit access** to the root page (**Share → Connect to**).
- `sync_state.json` lives inside your docs directory. If that directory is inside a version-controlled repo, add `sync_state.json` to its `.gitignore`.
- If you change the markdown-to-Notion rendering logic, clear the content hashes in `sync_state.json` to force a full re-upload (set all `content_hash` values to `null` or delete the file).