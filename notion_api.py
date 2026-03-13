import hashlib
import os
import re
import requests
from difflib import SequenceMatcher
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from config import HEADERS, BLOCK_LIMIT, BASE_DIR, RED, YELLOW, GREEN, RESET
from markdown_parser import md_to_notion_blocks
from sync_state import state


def _extract_title(file_name: str, md_content: str) -> str:
    """Return a clean page title.

    Priority:
    1. First H1 heading found in the markdown content.
    2. Filename with Notion's export UUID suffix and .md extension stripped.
    """
    match = re.search(r"^#\s+(.+)$", md_content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    # Strip trailing Notion UUID (space + 32 hex chars) and .md extension.
    name = os.path.splitext(file_name)[0]
    name = re.sub(r"\s+[a-f0-9]{32}$", "", name)
    return name.strip()

# Global session with retries
# Default timeout for all Notion API calls (connect, read) in seconds.
REQUEST_TIMEOUT = 30

session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PATCH", "DELETE"]
)
adapter = HTTPAdapter(max_retries=retries)
session.mount("https://", adapter)
session.mount("http://", adapter)

# ---------------------------------------------------------------------------
# Block-level diff helpers
# ---------------------------------------------------------------------------

def _md_hash(content: str) -> str:
    """SHA-256 of markdown text, used to detect content changes without hitting the Notion API."""
    return hashlib.sha256(content.encode()).hexdigest()


def _block_fingerprint(block: dict) -> str:
    """Stable string key for comparing a block in the diff.

    Image blocks from Notion (existing) carry no _from_cache tag and are always
    fingerprinted as 'image:cached'.  New image blocks served from our upload
    cache (_from_cache=True) are also 'image:cached', so they match and the
    existing Notion block is preserved.  Newly-uploaded images (_from_cache=False)
    get a unique fingerprint so they trigger a replacement.
    """
    btype = block.get("type", "unknown")

    if btype == "image":
        from_cache = block.get("_from_cache")
        if from_cache is False:
            # Freshly uploaded — use the upload ID so it won't match the old block.
            upload_id = block.get("image", {}).get("file_upload", {}).get("id", "")
            return f"image:new:{upload_id}"
        # Cached upload OR existing Notion block (no tag) → treat as unchanged.
        return "image:cached"

    if btype == "divider":
        return "divider"

    block_data = block.get(btype, {})
    rich_text = block_data.get("rich_text", [])
    text = "".join(rt.get("text", {}).get("content", "") for rt in rich_text)

    if btype == "code":
        lang = block_data.get("language", "plain text")
        return f"code:{lang}:{text}"

    return f"{btype}:{text}"


def _strip_block_metadata(blocks: list) -> list:
    """Remove internal _* metadata keys before sending blocks to the Notion API."""
    return [{k: v for k, v in b.items() if not k.startswith("_")} for b in blocks]


def _delete_block(block_id: str):
    """Delete a single Notion block by ID."""
    resp = session.delete(f"https://api.notion.com/v1/blocks/{block_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        print(f"{RED}Failed to delete block {block_id}: {resp.status_code} - {resp.text}{RESET}")


NOTION_BLOCK_LIMIT = 100

def _insert_blocks_after(page_id: str, blocks: list, after_id: str | None) -> bool:
    """Append blocks to a page, optionally after a specific block ID.

    Splits large payloads into sequential chunks of at most NOTION_BLOCK_LIMIT blocks
    to satisfy Notion's API limit. Each subsequent chunk is inserted after the last
    block of the previous chunk.
    """
    current_after_id = after_id

    for i in range(0, len(blocks), NOTION_BLOCK_LIMIT):
        chunk = blocks[i:i + NOTION_BLOCK_LIMIT]
        payload: dict = {"children": chunk}
        if current_after_id:
            payload["after"] = current_after_id
        resp = session.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            json=payload,
            timeout=REQUEST_TIMEOUT,
            headers=HEADERS,
        )
        if resp.status_code != 200:
            print(f"{RED}Failed to insert blocks: {resp.status_code} - {resp.text}{RESET}")
            return False
        results = resp.json().get("results", [])
        if results:
            current_after_id = results[-1]["id"]

    return True


def sync_page_blocks(page_id: str, existing_blocks: list, new_blocks: list,
                     dry_run: bool = False) -> str:
    """Apply a minimal diff between existing Notion blocks and new blocks.

    Unchanged blocks keep their Notion IDs (preserving comments/reactions).
    Only changed, inserted, or deleted blocks are touched.
    Returns 'updated' or 'failed'.
    In dry_run mode, computes and prints the diff but makes no API calls.
    """
    old_fps = [_block_fingerprint(b) for b in existing_blocks]
    new_fps = [_block_fingerprint(b) for b in new_blocks]
    ops = list(SequenceMatcher(None, old_fps, new_fps, autojunk=False).get_opcodes())

    # The Notion API cannot insert before the first existing block (no 'before' parameter).
    # Fall back to full page rewrite only in that edge case.
    needs_insert_at_start = any(
        tag in ("insert", "replace") and i1 == 0 and existing_blocks
        for tag, i1, i2, j1, j2 in ops
    )

    if dry_run:
        if needs_insert_at_start:
            print(f"{YELLOW}  [dry] Would rewrite full page ({len(new_blocks)} blocks){RESET}")
        else:
            keep   = sum(i2 - i1 for tag, i1, i2, j1, j2 in ops if tag == "equal")
            delete = sum(i2 - i1 for tag, i1, i2, j1, j2 in ops if tag in ("delete", "replace"))
            insert = sum(j2 - j1 for tag, i1, i2, j1, j2 in ops if tag in ("insert", "replace"))
            print(f"{YELLOW}  [dry] diff — keep: {keep}, delete: {delete}, insert: {insert}{RESET}")
        return "updated"

    if needs_insert_at_start:
        print(f"{YELLOW}Insertion before first block detected; falling back to full page rewrite.{RESET}")
        delete_existing_content(page_id)
        result = upload_blocks_to_notion(page_id, _strip_block_metadata(new_blocks))
        return result or "updated"

    last_kept_id: str | None = None

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            last_kept_id = existing_blocks[i2 - 1]["id"]

        elif tag == "delete":
            for block in existing_blocks[i1:i2]:
                _delete_block(block["id"])

        elif tag == "insert":
            ok = _insert_blocks_after(page_id, _strip_block_metadata(new_blocks[j1:j2]), last_kept_id)
            if not ok:
                return "failed"

        elif tag == "replace":
            for block in existing_blocks[i1:i2]:
                _delete_block(block["id"])
            ok = _insert_blocks_after(page_id, _strip_block_metadata(new_blocks[j1:j2]), last_kept_id)
            if not ok:
                return "failed"

    return "updated"


def get_all_notion_pages(parent_id, parent_path=""):
    """Recursively fetch all Notion pages under a given parent page and store full paths."""
    print("Fetching All Notion Pages to compare with local...")

    def fetch_pages(parent, current_path):
        """Fetch all child pages for a given parent page."""
        url = f"https://api.notion.com/v1/blocks/{parent}/children"
        notion_pages = {}  # Store pages with full relative paths
        next_cursor = None

        while True:
            params = {"page_size": 100}
            if next_cursor:
                params["start_cursor"] = next_cursor

            response = session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                print(f"{RED}Error fetching pages: {response.status_code}, {response.text}{RESET}")
                return notion_pages

            data = response.json()
            results = data.get("results", [])
            next_cursor = data.get("next_cursor")

            for page in results:
                if page["object"] == "block" and page["type"] == "child_page":
                    sub_page_id = page["id"]
                    page_title = page["child_page"]["title"].strip()

                    # Construct full relative path
                    full_path = os.path.join(current_path, page_title)

                    # Store in dictionary
                    notion_pages[full_path] = sub_page_id

                    # Recursively fetch subpages
                    notion_pages.update(fetch_pages(sub_page_id, full_path))

            if not next_cursor:
                break

        return notion_pages

    return fetch_pages(parent_id, parent_path)

def get_existing_child_pages(parent_id):
    """Fetch all child pages under a parent to check for existing pages."""
    url = f"https://api.notion.com/v1/blocks/{parent_id}/children"
    response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)

    if response.status_code == 200:
        return response.json().get("results", [])
    else:
        print(f"{RED}Error fetching existing pages: {response.status_code}, {response.text}{RESET}")
        return []

def search_existing_page(title, parent_id):
    """Search recursively for an existing Notion page matching the title."""
    child_pages = get_existing_child_pages(parent_id)

    for page in child_pages:
        if page["object"] == "block" and page["type"] == "child_page":
            page_title = page["child_page"]["title"]
            if page_title == title:
                return page["id"]  # Return existing page ID

            # Recursively check inside this child page
            sub_page_id = page["id"]
            found_page_id = search_existing_page(title, sub_page_id)
            if found_page_id:
                return found_page_id

    return None  # No existing page found

def get_existing_page_content(page_id):
    """Fetch the current content of a Notion page."""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)

    if response.status_code == 200:
        return response.json().get("results", [])
    else:
        print(f"{RED}Error fetching page content: {response.status_code}, {response.text}{RESET}")
        return []

def archive_page_in_notion(page_id):
    """Archives a Notion page instead of deleting it."""

    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {"archived": True}

    response = session.patch(url, headers=HEADERS, json=data, timeout=REQUEST_TIMEOUT)

    if response.status_code == 200:
        print(f"{GREEN}Successfully archived Notion page (ID: {page_id}){RESET}")
        return "deleted"
    else:
        print(f"{RED}Failed to archive Notion page (ID: {page_id}) | Status Code: {response.status_code}{RESET}")
        print(f"Response: {response.text}")

def reconcile_state(local_md_files):
    """Populate state from the live Notion tree on the very first run (empty state).

    Subsequent runs skip this and trust the state file entirely.
    """
    if not state.is_empty:
        return

    print(f"{YELLOW}No local state found. Discovering existing Notion pages (one-time)...{RESET}")
    notion_pages = get_all_notion_pages(state.get_notion_root_page_id())

    local_relative = {os.path.relpath(f, BASE_DIR) for f in local_md_files}

    for notion_path, page_id in notion_pages.items():
        if notion_path.endswith(".md"):
            if notion_path in local_relative:
                state.set_page_id(notion_path, page_id)
        else:
            state.set_folder_id(notion_path, page_id)

    state.save()
    print(f"{GREEN}Reconciled {len(state.all_pages())} page(s) and {len(state.all_folders())} folder(s) from Notion.{RESET}")


def delete_notion_page_if_missing(local_md_files, dry_run: bool = False):
    """Archives Notion pages that correspond to missing local Markdown files."""
    local_file_names = {os.path.relpath(f, BASE_DIR) for f in local_md_files}

    # Derive which folder paths are still needed locally.
    local_folders = set()
    for f in local_md_files:
        folder_path = os.path.dirname(f)
        while folder_path and folder_path != BASE_DIR:
            local_folders.add(os.path.relpath(folder_path, BASE_DIR))
            folder_path = os.path.dirname(folder_path)

    deleted_pages = 0

    # Archive pages tracked in state that no longer exist locally.
    for local_path, page_id in list(state.all_pages().items()):
        if local_path not in local_file_names:
            if dry_run:
                print(f"{YELLOW}[dry] Would archive missing page: {local_path}{RESET}")
                deleted_pages += 1
            else:
                print(f"{RED}Detected missing file: {local_path} | Archiving Notion page...{RESET}")
                result = archive_page_in_notion(page_id)
                if result == "deleted":
                    state.remove_page(local_path)
                    deleted_pages += 1

    # Archive folder pages tracked in state that are no longer needed.
    for folder_path, folder_page_id in list(state.all_folders().items()):
        if folder_path not in local_folders:
            child_pages = get_existing_child_pages(folder_page_id)
            if not child_pages:
                if dry_run:
                    print(f"{YELLOW}[dry] Would archive empty folder: {folder_path}{RESET}")
                else:
                    print(f"{RED}Folder {folder_path} is now empty. Archiving Notion folder page...{RESET}")
                    archive_page_in_notion(folder_page_id)
                    state.remove_folder(folder_path)

    if not dry_run:
        state.save()
    return deleted_pages


def delete_existing_content(page_id):
    """Delete all content blocks from an existing Notion page before updating."""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    params = {"page_size": 100}

    while True:
        response = session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"{RED}Error fetching content for deletion: {response.status_code}, {response.text}{RESET}")
            break

        data = response.json()
        blocks = data.get("results", [])

        # Delete each block in the current batch.
        for block in blocks:
            if block["object"] == "block" and block.get("id"):
                block_id = block.get("id")
                del_response = session.delete(f"https://api.notion.com/v1/blocks/{block_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if del_response.status_code != 200:
                    print(f"{RED}Failed to delete block {block_id}: {del_response.status_code} - {del_response.text}{RESET}")

        # Check if there are more blocks.
        if data.get("has_more"):
            params["start_cursor"] = data.get("next_cursor")
        else:
            break

    print(f"{YELLOW}Cleared old content from page (ID: {page_id}){RESET}")

def create_or_update_notion_page(title, parent_id, blocks, is_folder=False):
    """Create a new Notion page or update an existing one if found."""
    existing_page_id = search_existing_page(title, parent_id)

    if existing_page_id:
        # Folder pages carry no content — nothing to update if the page already exists.
        return existing_page_id

    payload = {
        "parent": {"page_id": parent_id},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
    }
    if is_folder:
        payload["icon"] = {"emoji": "🗂️"}

    print(f"{GREEN}Creating new Notion page: {title}{RESET}")
    response = session.post("https://api.notion.com/v1/pages", json=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if response.status_code == 200:
        page_id = response.json().get("id")
        upload_blocks_to_notion(page_id, blocks)  # Upload content immediately
        return page_id
    else:
        print(f"{RED}Failed to create Notion page: {title} | Error: {response.status_code} - {response.text}{RESET}")
        return None

def get_or_create_folder_page(folder_path, dry_run: bool = False):
    """Ensure Notion folder pages match directory structure recursively.

    Uses state for lookup (keyed by full relative path) to avoid name collisions
    between folders at different nesting levels.
    In dry_run mode, skips creation and returns the root page ID as a placeholder.
    """
    parent_id = state.get_notion_root_page_id()
    folders = folder_path.split(os.sep)
    accumulated = ""

    for folder in folders:
        accumulated = os.path.join(accumulated, folder) if accumulated else folder
        cached_id = state.get_folder_id(accumulated)
        if cached_id:
            parent_id = cached_id
        else:
            if dry_run:
                print(f"{YELLOW}  [dry] Would create folder: {accumulated}{RESET}")
                continue
            folder_id = search_existing_page(folder, parent_id)
            if not folder_id:
                folder_id = create_or_update_notion_page(folder, parent_id, [], is_folder=True)
            if folder_id:
                state.set_folder_id(accumulated, folder_id)
                parent_id = folder_id

    return parent_id

def upload_blocks_to_notion(page_id, blocks):
    """Upload content blocks to Notion in chunks, ensuring no empty pages."""
    if not blocks:
        print(f"Skipping empty block upload for page (ID: {page_id})")
        return

    for i in range(0, len(blocks), BLOCK_LIMIT):
        response = session.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            json={"children": blocks[i:i + BLOCK_LIMIT]},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )
        if response.status_code == 200:
            print(f"{GREEN}Successfully updated Notion page (ID: {page_id}){RESET}")
        else:
            print(f"{RED}Error updating blocks: {response.status_code}, {response.text}{RESET}")
            return "failed"

    return "updated"

def upload_markdown_file_to_notion(file_path, update_content=False, new_content=None,
                                   dry_run: bool = False):
    """Upload a Markdown file as a Notion page inside its folder structure.

    If update_content is False, a minimal content is uploaded (or the page is created if missing).
    If update_content is True, then the file’s content is used, converted to Notion blocks, and the page is updated if
    changes are detected.
    In dry_run mode, computes all diffs and prints what would change, but makes no writes.
    """
    file_name = os.path.basename(file_path)
    base_path = os.path.dirname(file_path)
    relative_path = os.path.relpath(os.path.dirname(file_path), BASE_DIR)

    parent_id = get_or_create_folder_page(relative_path, dry_run=dry_run) if relative_path != "." else state.get_notion_root_page_id()

    # Read markdown content.
    try:
        if update_content:
            # For updates, use new_content if provided, else read file.
            if new_content is not None:
                md_content = new_content
            else:
                with open(file_path, "r", encoding="utf-8") as f:
                    md_content = f.read()
        else:
            # For phase 1 (creation), we can use the file content but upload minimal blocks.
            with open(file_path, "r", encoding="utf-8") as f:
                md_content = f.read()
    except Exception as e:
        print(f"{RED}Error reading {file_path}: {e}{RESET}")
        return ("failed", None)

    page_title = _extract_title(file_name, md_content)

    # Generate blocks.
    if update_content:
        blocks = md_to_notion_blocks(md_content, base_path=base_path, dry_run=dry_run)
        # Drop the first block if it's the H1 that was promoted to the page title,
        # so it doesn't appear as a duplicate heading inside the page.
        if (blocks and blocks[0].get("type") == "heading_1"):
            first_text = blocks[0].get("heading_1", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")
            if first_text.strip() == page_title:
                blocks = blocks[1:]
    else:
        # Minimal content (an empty paragraph) for initial creation.
        blocks = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": ""}}]
            }
        }]

    if not blocks:
        print(f"{YELLOW}Warning: No content to upload for {file_name}{RESET}")
        return ("skipped", None)

    # Look up the page ID from local state (O(1), no Notion API call).
    state_key = os.path.relpath(file_path, BASE_DIR)
    existing_page_id = state.get_page_id(state_key)

    if existing_page_id:
        if update_content:
            # Fast path: skip if the markdown content hasn't changed since last sync.
            current_hash = _md_hash(md_content)
            if state.get_page_hash(state_key) == current_hash:
                print(f"Page '{file_name}' unchanged. Skipping.")
                return ("skipped", existing_page_id)

            action = "[dry] Would sync" if dry_run else "Syncing"
            print(f"{YELLOW}Page '{file_name}' content has changed. {action}...{RESET}")
            existing_blocks = get_existing_page_content(existing_page_id)
            status = sync_page_blocks(existing_page_id, existing_blocks, blocks, dry_run=dry_run)

            if status == "failed":
                return ("failed", existing_page_id)

            if not dry_run:
                state.set_page_hash(state_key, current_hash)
                state.save()
            return ("updated", existing_page_id)
        else:
            # Phase 1: page already known, nothing to do.
            return ("skipped", existing_page_id)
    else:
        # Create a new page.
        if dry_run:
            print(f"{YELLOW}[dry] Would create: '{page_title}' ({len(blocks)} blocks){RESET}")
            return ("created", None)

        print(f"{GREEN}Creating new Notion page: {page_title}{RESET}")
        response = session.post(
            "https://api.notion.com/v1/pages",
            json={
                "parent": {"page_id": parent_id},
                "properties": {"title": {"title": [{"text": {"content": page_title}}]}},
            },
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            new_page_id = response.json().get("id")
            state.set_page_id(state_key, new_page_id)
            state.save()
            upload_blocks_to_notion(new_page_id, blocks)
            return ("created", new_page_id)
        else:
            print(f"{RED}Failed to create Notion page: {file_name} | Error: {response.status_code} - {response.text}{RESET}")
            return ("failed", None)