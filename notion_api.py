import hashlib
import os
import re
import requests
from difflib import SequenceMatcher
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from config import HEADERS, BLOCK_LIMIT, BASE_DIR, ROOT_IS_FILE, RED, YELLOW, GREEN, RESET
from markdown_parser import md_to_notion_blocks
from sync_state import state
from image_uploader import evict_by_upload_id


def _root_stem() -> str | None:
    """Return the stem of the root .md file when --root-is-file is active.

    Requires exactly one .md file at BASE_DIR root with a matching subfolder
    (e.g. 'Fraud Control.md' + 'Fraud Control/').  Returns None in all other
    cases so normal child-page behaviour is preserved.
    """
    if not ROOT_IS_FILE:
        return None
    try:
        md_files = [
            e for e in os.scandir(BASE_DIR)
            if e.is_file() and e.name.endswith(".md")
        ]
        if len(md_files) != 1:
            print(f"{YELLOW}--root-is-file ignored: expected exactly 1 .md file at root, found {len(md_files)}.{RESET}")
            return None
        stem = os.path.splitext(md_files[0].name)[0]
        if not os.path.isdir(os.path.join(BASE_DIR, stem)):
            print(f"{YELLOW}--root-is-file ignored: no matching subfolder '{stem}/' found.{RESET}")
            return None
        return stem
    except Exception:
        return None


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
# Root context — detects and caches whether the sync target is a page tree
# or a Notion database, providing type-aware API helpers for page creation,
# searching, and state reconciliation.
# ---------------------------------------------------------------------------

# Module-level singleton set once at startup by init_root_context().
_root_context: "NotionRootContext | None" = None


def _search_db_row(title: str, database_id: str) -> str | None:
    """Query a Notion database to find a row whose title matches `title`."""
    resp = session.post(
        f"https://api.notion.com/v1/databases/{database_id}/query",
        headers=HEADERS,
        json={"filter": {"property": "title", "title": {"equals": title}}, "page_size": 10},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    for row in resp.json().get("results", []):
        for prop in row.get("properties", {}).values():
            if prop.get("type") == "title":
                row_title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                if row_title == title:
                    return row["id"]
    return None


def _fetch_child_pages_recursive(parent_id: str, current_path: str) -> dict:
    """Walk child_page blocks under parent_id, returning {path: page_id} recursively."""
    pages = {}
    url = f"https://api.notion.com/v1/blocks/{parent_id}/children"
    next_cursor = None
    while True:
        params = {"page_size": 100}
        if next_cursor:
            params["start_cursor"] = next_cursor
        resp = session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            break
        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") == "child_page":
                child_id = block["id"]
                child_title = block["child_page"]["title"].strip()
                child_path = os.path.join(current_path, child_title)
                pages[child_path] = child_id
                pages.update(_fetch_child_pages_recursive(child_id, child_path))
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break
    return pages


class NotionRootContext:
    """Encapsulates whether the sync target is a page tree or a Notion database.

    Direct children of a database root are created as database rows
    (parent: {database_id: ...}).  Deeper pages and all page-tree pages
    are regular child pages (parent: {page_id: ...}).
    """

    def __init__(self, root_id: str, root_type: str, root_accepts_blocks: bool = True):
        self.root_id = root_id
        self.root_type = root_type  # "page" | "database"
        self.root_accepts_blocks = root_accepts_blocks  # False for standalone databases

    @staticmethod
    def _norm(page_id: str) -> str:
        return page_id.replace("-", "")

    def is_database(self) -> bool:
        return self.root_type == "database"

    def _is_root(self, page_id: str) -> bool:
        return self._norm(page_id) == self._norm(self.root_id)

    def parent_dict(self, parent_id: str) -> dict:
        """Return the correct Notion parent object for creating a page under parent_id.

        When the root is a database and parent_id IS the root, returns
        {"database_id": root_id} so the new page becomes a DB row.
        All other cases return {"page_id": parent_id}.
        """
        if self.is_database() and self._is_root(parent_id):
            return {"database_id": self.root_id}
        return {"page_id": parent_id}

    def search_direct_child(self, title: str, parent_id: str) -> str | None:
        """Find an existing child page or DB row by title under parent_id.

        Uses a database query when parent_id is the database root;
        falls back to the block-children search otherwise.
        """
        if self.is_database() and self._is_root(parent_id):
            return _search_db_row(title, self.root_id)
        return search_existing_page(title, parent_id)

    def discover_pages(self) -> dict:
        """Return {relative_path: page_id} for all existing pages under the root.

        Used by reconcile_state on the first run to populate local state
        without making destructive Notion API calls.
        """
        if self.is_database():
            return self._discover_db_pages()
        return get_all_notion_pages(self.root_id)

    def _discover_db_pages(self) -> dict:
        """Query all DB rows and recursively collect their child pages."""
        pages = {}
        url = f"https://api.notion.com/v1/databases/{self.root_id}/query"
        next_cursor = None
        while True:
            body = {"page_size": 100}
            if next_cursor:
                body["start_cursor"] = next_cursor
            resp = session.post(url, headers=HEADERS, json=body, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200:
                print(f"{RED}Error querying database for page discovery: {resp.status_code}{RESET}")
                break
            data = resp.json()
            for row in data.get("results", []):
                row_id = row["id"]
                title = ""
                for prop in row.get("properties", {}).values():
                    if prop.get("type") == "title":
                        title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                        break
                if not title:
                    continue
                pages[title] = row_id
                pages.update(_fetch_child_pages_recursive(row_id, title))
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break
        return pages


def init_root_context(root_id: str, dry_run: bool = False) -> NotionRootContext:
    """Detect whether root_id is a page or database, cache the result, return context.

    On subsequent runs the type is read from sync_state.json to avoid extra
    API calls.  Re-detects automatically when root_id changes.
    """
    global _root_context

    # Use cached type if root hasn't changed since last run.
    cached_type = state.get_root_type()
    cached_root = state.get_notion_root_page_id()
    if cached_type and cached_root and cached_root.replace("-", "") == root_id.replace("-", ""):
        _root_context = NotionRootContext(root_id, cached_type)
        return _root_context

    # Auto-detect: try page endpoint first, fall back to database.
    resp = session.get(f"https://api.notion.com/v1/pages/{root_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 200:
        root_type = "page"
    else:
        resp2 = session.get(f"https://api.notion.com/v1/databases/{root_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp2.status_code == 200:
            root_type = "database"
        else:
            print(f"{YELLOW}Could not confirm root type for {root_id}; defaulting to 'page'.{RESET}")
            root_type = "page"

    print(f"{GREEN}Root type detected: {root_type}{RESET}")
    state.set_root_type(root_type)
    state.save()

    # For database roots, probe whether the page layer accepts content blocks by
    # attempting a real PATCH write.  Some databases (e.g. full-page databases created
    # before Notion changed their structure) accept blocks; plain standalone databases
    # return 400.  Result is cached in sync_state so the probe only runs once.
    root_accepts_blocks = True
    if root_type == "database":
        cached_accepts = state.get_root_accepts_blocks()
        if cached_accepts is not None:
            root_accepts_blocks = cached_accepts
        elif dry_run:
            # Can't probe without writing — assume blocks are supported.
            # Will be probed and cached on the first real (non-dry-run) run.
            pass
        else:
            probe = session.patch(
                f"https://api.notion.com/v1/blocks/{root_id}/children",
                headers=HEADERS,
                json={"children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}]},
                timeout=REQUEST_TIMEOUT,
            )
            if probe.status_code == 400 and "does not support children" in probe.text:
                root_accepts_blocks = False
            elif probe.status_code == 200:
                root_accepts_blocks = True
                probe_results = probe.json().get("results", [])
                if probe_results:
                    session.delete(
                        f"https://api.notion.com/v1/blocks/{probe_results[0]['id']}",
                        headers=HEADERS,
                        timeout=REQUEST_TIMEOUT,
                    )
            state.set_root_accepts_blocks(root_accepts_blocks)
            state.save()

    _root_context = NotionRootContext(root_id, root_type, root_accepts_blocks)
    return _root_context


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
            # In dry-run mode images are not uploaded; the placeholder ID must not
            # cause a spurious replacement, so treat it the same as a cache hit.
            if upload_id == "dry-run-placeholder":
                return "image:cached"
            return f"image:new:{upload_id}"
        # Cached upload OR existing Notion block (no tag) → treat as unchanged.
        return "image:cached"

    if btype == "divider":
        return "divider"

    if btype == "bookmark":
        url = block.get("bookmark", {}).get("url", "")
        return f"bookmark:{url}"

    if btype == "table":
        # Fingerprint based on cell content so the diff detects table changes.
        # Both new blocks (children embedded by parser) and existing Notion blocks
        # (children fetched by get_existing_page_content) carry children here.
        children = block.get("table", {}).get("children", [])
        parts = []
        for row in children:
            for cell in row.get("table_row", {}).get("cells", []):
                for rt in cell:
                    parts.append(rt.get("text", {}).get("content", ""))
        digest = hashlib.md5("|".join(parts).encode()).hexdigest()[:8]
        return f"table:{digest}"

    # child_page blocks store their title differently — no rich_text array.
    if btype == "child_page":
        title = block.get("child_page", {}).get("title", "")
        # Fingerprint as a paragraph so it matches the link-paragraph that the
        # pull generates for child pages (e.g. "[Title](path.md)").
        return f"paragraph:{title}"

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


def _fetch_notion_last_edited(page_id: str) -> str | None:
    """Return the current last_edited_time for a page, or None on failure."""
    resp = session.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 200:
        return resp.json().get("last_edited_time")
    return None


def check_notion_drift(md_files: list, dry_run: bool = False) -> list:
    """Pre-flight drift check: detect pages edited in Notion since the last push/pull.

    For every file whose local content has changed (would be pushed in Phase 2),
    fetches Notion's current last_edited_time and compares it with the stored value.
    Pages with no stored timestamp (first run or legacy state) are skipped — we
    cannot know whether they drifted.

    In dry-run mode:  prints a warning for each drifted page but does not abort.
    In apply mode:    the caller should abort if the returned list is non-empty.

    Returns a list of dicts {file, state_key, stored_ts, notion_ts} for drifted pages.
    """
    drifted = []
    for file_path in md_files:
        state_key = os.path.relpath(file_path, BASE_DIR)

        # Only check files that would actually be pushed (content changed).
        stored_hash = state.get_page_hash(state_key)
        try:
            with open(file_path, encoding="utf-8") as fh:
                current_hash = _md_hash(fh.read())
        except OSError:
            continue
        if stored_hash == current_hash:
            continue  # unchanged — won't be pushed, no point checking

        page_id = state.get_page_id(state_key)
        stored_ts = state.get_notion_last_edited(state_key)
        if not page_id or not stored_ts:
            continue  # new page or no baseline — skip

        notion_ts = _fetch_notion_last_edited(page_id)
        if notion_ts and notion_ts != stored_ts:
            drifted.append({
                "file": file_path,
                "state_key": state_key,
                "stored_ts": stored_ts,
                "notion_ts": notion_ts,
            })

    if drifted:
        label = "Warning" if dry_run else "Aborting"
        print(f"{RED}{label}: the following page(s) were edited in Notion since your last pull:{RESET}")
        for d in drifted:
            print(f"  {d['state_key']}")
            print(f"    last known: {d['stored_ts']}")
            print(f"    notion now: {d['notion_ts']}")
        suggestion = "Run 'pull' to get the latest changes before syncing."
        if not dry_run:
            suggestion += " Use --force to overwrite Notion anyway."
        print(f"{RED}{suggestion}{RESET}")

    return drifted


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


def _is_child_page_link_paragraph(block: dict, child_page_titles: set) -> bool:
    """Return True if block is a sole-link paragraph whose text matches a child_page title.

    These blocks are generated by the pull step to represent Notion sub-pages as
    markdown links.  They should never be pushed back to Notion as content blocks
    because Notion already owns those sub-pages as child_page blocks.
    """
    if block.get("type") != "paragraph":
        return False
    rt = block.get("paragraph", {}).get("rich_text", [])
    if len(rt) != 1:
        return False
    text_obj = rt[0].get("text", {})
    content = text_obj.get("content", "")
    link = text_obj.get("link")
    return link is not None and content in child_page_titles


def sync_page_blocks(page_id: str, existing_blocks: list, new_blocks: list,
                     dry_run: bool = False) -> str:
    """Apply a minimal diff between existing Notion blocks and new blocks.

    Unchanged blocks keep their Notion IDs (preserving comments/reactions).
    Only changed, inserted, or deleted blocks are touched.
    Returns 'updated' or 'failed'.
    In dry_run mode, computes and prints the diff but makes no API calls.

    child_page blocks are always excluded from both sides of the diff: they are
    Notion-owned structural elements that we must never delete or replace.
    Their corresponding link-only paragraphs on the local side are also stripped
    so the sync tool does not re-insert them as plain paragraph blocks.
    """
    # Collect titles of all child_page blocks so we can filter their local mirrors.
    child_page_titles = {
        b["child_page"]["title"]
        for b in existing_blocks
        if b.get("type") == "child_page"
    }

    # Strip child_page blocks from existing — they live outside our content area.
    existing_blocks = [b for b in existing_blocks if b.get("type") != "child_page"]

    # Strip sole-link paragraphs whose text matches a child_page title from local —
    # these are the markdown links the pull generates for child pages.
    new_blocks = [b for b in new_blocks if not _is_child_page_link_paragraph(b, child_page_titles)]

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
        # Propagate image_expired tuple so the caller can retry.
        if isinstance(result, tuple):
            return result
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
    """Fetch the current content of a Notion page (all blocks, paginated).

    Also fetches table row children for any table blocks encountered, so that
    _block_fingerprint can compute content-aware hashes for tables.

    Returns a list of blocks on success, or None when the page cannot be found
    (404) or is archived — callers should treat None as "page needs recreating".
    """
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    all_blocks: list = []
    next_cursor: str | None = None

    while True:
        params: dict = {"page_size": 100}
        if next_cursor:
            params["start_cursor"] = next_cursor
        response = session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            print(f"{RED}Error fetching page content: {response.status_code}, {response.text}{RESET}")
            # Return whatever we managed to collect before the error, or None if nothing.
            return all_blocks if all_blocks else None

        data = response.json()
        all_blocks.extend(data.get("results", []))
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break

    # Enrich table blocks with their row children so fingerprinting is content-aware.
    for block in all_blocks:
        if block.get("type") == "table":
            rows_resp = session.get(
                f"https://api.notion.com/v1/blocks/{block['id']}/children",
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            if rows_resp.status_code == 200:
                block.setdefault("table", {})["children"] = rows_resp.json().get("results", [])

    return all_blocks

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
    notion_pages = (
        _root_context.discover_pages()
        if _root_context
        else get_all_notion_pages(state.get_notion_root_page_id())
    )

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

    # Pass 1: collect all deletable block IDs so we know the total upfront.
    block_ids = []
    params = {"page_size": 100}
    while True:
        response = session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"{RED}Error fetching content for deletion: {response.status_code}, {response.text}{RESET}")
            return
        data = response.json()
        for block in data.get("results", []):
            if block["object"] == "block" and block.get("id") and block.get("type") != "child_page":
                block_ids.append(block["id"])
        if data.get("has_more"):
            params["start_cursor"] = data.get("next_cursor")
        else:
            break

    total = len(block_ids)
    if total == 0:
        return

    # Pass 2: delete with progress printed every 10 blocks.
    print(f"  Clearing page… (0/{total} blocks)", end="", flush=True)
    for i, block_id in enumerate(block_ids, start=1):
        del_response = session.delete(
            f"https://api.notion.com/v1/blocks/{block_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT
        )
        if del_response.status_code != 200:
            print(f"\n{RED}Failed to delete block {block_id}: {del_response.status_code} - {del_response.text}{RESET}")
        if i % 10 == 0 or i == total:
            print(f"\r  Clearing page… ({i}/{total} blocks)", end="", flush=True)
    print()  # newline after progress line
    print(f"{YELLOW}  Cleared {total} block(s).{RESET}")

def create_or_update_notion_page(title, parent_id, blocks, is_folder=False):
    """Create a new Notion page or update an existing one if found."""
    ctx = _root_context
    existing_page_id = (
        ctx.search_direct_child(title, parent_id)
        if ctx
        else search_existing_page(title, parent_id)
    )

    if existing_page_id:
        # Folder pages carry no content — nothing to update if the page already exists.
        return existing_page_id

    parent = ctx.parent_dict(parent_id) if ctx else {"page_id": parent_id}
    payload = {
        "parent": parent,
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
    }
    # Only add the folder emoji for page-tree folders, not for database rows.
    if is_folder and "page_id" in parent:
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

    If the first segment of folder_path matches the root stem (the folder that
    pairs with the root .md file), it is skipped — that level maps directly to
    the target Notion page rather than creating an intermediate folder page.
    """
    parent_id = state.get_notion_root_page_id()
    folders = folder_path.split(os.sep)

    # Strip the root-stem segment so its children land directly under the target page.
    root_stem = _root_stem()
    if root_stem and folders and folders[0] == root_stem:
        folders = folders[1:]
    if not folders or folders == [""]:
        return parent_id

    accumulated = ""

    for folder in folders:
        accumulated = os.path.join(accumulated, folder) if accumulated else folder
        cached_id = state.get_folder_id(accumulated)
        if cached_id:
            parent_id = cached_id
        else:
            # Always check Notion first (even in dry-run) so we don't falsely
            # report folders as missing when they already exist in Notion but
            # haven't been cached locally yet (e.g. after a fresh pull).
            ctx = _root_context
            folder_id = (
                ctx.search_direct_child(folder, parent_id)
                if ctx
                else search_existing_page(folder, parent_id)
            )
            if folder_id:
                state.set_folder_id(accumulated, folder_id)
                parent_id = folder_id
            else:
                if dry_run:
                    print(f"{GREEN}  [dry] Would create folder: {accumulated}{RESET}")
                    continue
                folder_id = create_or_update_notion_page(folder, parent_id, [], is_folder=True)
                if folder_id:
                    state.set_folder_id(accumulated, folder_id)
                    parent_id = folder_id

    return parent_id

def upload_blocks_to_notion(page_id, blocks):
    """Upload content blocks to Notion in chunks, ensuring no empty pages.

    Returns "updated" on success, "failed" on a non-recoverable error, or
    ("image_expired", upload_id) when Notion rejects a stale file-upload ID so
    the caller can evict the cache entry and retry.
    """
    if not blocks:
        print(f"Skipping empty block upload for page (ID: {page_id})")
        return

    total = len(blocks)
    num_chunks = (total + BLOCK_LIMIT - 1) // BLOCK_LIMIT

    for chunk_idx, i in enumerate(range(0, total, BLOCK_LIMIT), start=1):
        chunk = blocks[i:i + BLOCK_LIMIT]
        end = min(i + BLOCK_LIMIT, total)
        print(f"  Uploading chunk {chunk_idx}/{num_chunks} (blocks {i + 1}–{end} of {total})…")
        response = session.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            json={"children": chunk},
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            # Detect an expired file-upload ID so the caller can recover.
            if response.status_code == 400:
                body = response.json()
                if body.get("code") == "validation_error" and "expired" in body.get("message", ""):
                    match = re.search(r"File upload ([0-9a-f-]{36})", body.get("message", ""))
                    if match:
                        expired_id = match.group(1)
                        evict_by_upload_id(expired_id)
                        print(f"{YELLOW}  Expired image upload evicted ({expired_id}). Will re-upload.{RESET}")
                        return "image_expired", expired_id
            print(f"{RED}Error updating blocks: {response.status_code}, {response.text}{RESET}")
            return "failed"

    print(f"{GREEN}Successfully updated Notion page (ID: {page_id}){RESET}")
    return "updated"

def upload_markdown_file_to_notion(file_path, update_content=False, new_content=None,
                                   dry_run: bool = False, raw_content: str | None = None,
                                   force: bool = False, _retry: bool = False):
    """Upload a Markdown file as a Notion page inside its folder structure.

    If update_content is False, a minimal content is uploaded (or the page is created if missing).
    If update_content is True, then the file’s content is used, converted to Notion blocks, and the page is updated if
    changes are detected.
    In dry_run mode, computes all diffs and prints what would change, but makes no writes.
    If force is True, remote drift detection is skipped and the local version always wins.
    _retry is set internally when recovering from an expired image upload; not for external callers.
    """
    file_name = os.path.basename(file_path)
    base_path = os.path.dirname(file_path)
    relative_path = os.path.relpath(os.path.dirname(file_path), BASE_DIR)
    state_key = os.path.relpath(file_path, BASE_DIR)

    # Detect whether this is the root overview file (e.g. 'Fraud Control.md'
    # paired with 'Fraud Control/' in the same directory).  Its content is
    # written directly to the target Notion page instead of creating a child.
    root_stem = _root_stem()
    is_root_file = (
        relative_path == "."
        and root_stem is not None
        and os.path.splitext(file_name)[0] == root_stem
    )

    if is_root_file:
        root_page_id = state.get_notion_root_page_id()
        # Phase 1: register target page as this file's page (no new page created).
        if not update_content:
            if not state.get_page_id(state_key):
                state.set_page_id(state_key, root_page_id)
                state.save()
            return ("skipped", root_page_id)

        # Phase 2: sync content directly onto the target Notion page.
        try:
            md_content = new_content if new_content is not None else open(file_path, encoding="utf-8").read()
        except Exception as e:
            print(f"{RED}Error reading {file_path}: {e}{RESET}")
            return ("failed", None)

        # Hash the raw file content (before link substitution) so the stored hash
        # stays consistent with what the pull command writes.
        current_hash = _md_hash(raw_content if raw_content is not None else md_content)
        if state.get_page_hash(state_key) == current_hash:
            print(f"Page '{file_name}' unchanged. Skipping.")
            return ("skipped", root_page_id)

        blocks = md_to_notion_blocks(md_content, base_path=base_path, dry_run=dry_run)
        # Drop leading H1 if it duplicates the page title.
        if blocks and blocks[0].get("type") == "heading_1":
            first_text = blocks[0].get("heading_1", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")
            if first_text.strip() == root_stem:
                blocks = blocks[1:]

        print(f"{YELLOW}Page '{file_name}' content has changed. Syncing to root page...{RESET}")
        existing_blocks = get_existing_page_content(root_page_id)
        if existing_blocks is None:
            if not dry_run:
                upload_blocks_to_notion(root_page_id, _strip_block_metadata(blocks))
        else:
            # Pass the full block list — sync_page_blocks now handles child_page
            # filtering internally (it extracts titles and strips the matching
            # link-only paragraphs from the local side too).
            sync_page_blocks(root_page_id, existing_blocks, blocks, dry_run=dry_run)
        if not dry_run:
            state.set_page_hash(state_key, current_hash)
            new_ts = _fetch_notion_last_edited(root_page_id)
            if new_ts:
                state.set_notion_last_edited(state_key, new_ts)
            state.save()
        return ("updated", root_page_id)

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

    # Generate blocks (Phase 2 / update only — Phase 1 just needs page structure).
    if update_content:
        blocks = md_to_notion_blocks(md_content, base_path=base_path, dry_run=dry_run)
        # Drop the first block if it's the H1 that was promoted to the page title,
        # so it doesn't appear as a duplicate heading inside the page.
        if (blocks and blocks[0].get("type") == "heading_1"):
            first_text = blocks[0].get("heading_1", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")
            if first_text.strip() == page_title:
                blocks = blocks[1:]
        if not blocks:
            print(f"{YELLOW}Warning: No content to upload for {file_name}{RESET}")
            return ("skipped", None)
    else:
        blocks = []

    # Look up the page ID from local state (O(1), no Notion API call).
    existing_page_id = state.get_page_id(state_key)

    if existing_page_id:
        if update_content:
            # Fast path: skip if the markdown content hasn't changed since last sync.
            # Always hash the raw file content (before link substitution) for consistency
            # with what the pull command stores.
            current_hash = _md_hash(raw_content if raw_content is not None else md_content)
            if state.get_page_hash(state_key) == current_hash:
                print(f"Page '{file_name}' unchanged. Skipping.")
                return ("skipped", existing_page_id)

            action = "[dry] Would sync" if dry_run else "Syncing"
            print(f"{YELLOW}Page '{file_name}' content has changed. {action}...{RESET}")

            # Fresh page (just created in Phase 1, no content yet) — upload directly,
            # no need to fetch or diff. Avoids 404s on newly created pages.
            if state.get_page_hash(state_key) is None and not dry_run:
                result = upload_blocks_to_notion(existing_page_id, _strip_block_metadata(blocks))
                if isinstance(result, tuple) and result[0] == "image_expired":
                    if not _retry:
                        print(f"{YELLOW}Retrying '{file_name}' after image re-upload…{RESET}")
                        return upload_markdown_file_to_notion(
                            file_path, update_content=True, new_content=new_content,
                            dry_run=dry_run, raw_content=raw_content, force=force, _retry=True,
                        )
                    return ("failed", existing_page_id)
                state.set_page_hash(state_key, current_hash)
                state.save()
                return ("updated", existing_page_id)

            existing_blocks = get_existing_page_content(existing_page_id)

            # None means the page is gone or archived on Notion's side.
            # Drop the stale state entry and fall through to re-creation.
            if existing_blocks is None:
                print(f"{YELLOW}Page '{file_name}' not found in Notion (archived or deleted). Re-creating...{RESET}")
                state.remove_page(state_key)
                state.save()
                existing_page_id = None
                # fall through to the creation block below
            else:
                content_blocks = [b for b in existing_blocks if b.get("type") != "child_page"]
                result = sync_page_blocks(existing_page_id, content_blocks, blocks, dry_run=dry_run)
                if isinstance(result, tuple) and result[0] == "image_expired":
                    if not _retry:
                        print(f"{YELLOW}Retrying '{file_name}' after image re-upload…{RESET}")
                        return upload_markdown_file_to_notion(
                            file_path, update_content=True, new_content=new_content,
                            dry_run=dry_run, raw_content=raw_content, force=force, _retry=True,
                        )
                    return ("failed", existing_page_id)
                if result == "failed":
                    return ("failed", existing_page_id)
                if not dry_run:
                    state.set_page_hash(state_key, current_hash)
                    new_ts = _fetch_notion_last_edited(existing_page_id)
                    if new_ts:
                        state.set_notion_last_edited(state_key, new_ts)
                    state.save()
                return ("updated", existing_page_id)
        else:
            # Phase 1: page already known, nothing to do.
            return ("skipped", existing_page_id)

    if not existing_page_id:
        # Create a new page (title only — Phase 2 uploads the content).
        if dry_run:
            print(f"{GREEN}[dry] Would create: '{page_title}'{RESET}")
            return ("created", None)

        print(f"{GREEN}Creating new Notion page: {page_title}{RESET}")
        ctx = _root_context
        response = session.post(
            "https://api.notion.com/v1/pages",
            json={
                "parent": ctx.parent_dict(parent_id) if ctx else {"page_id": parent_id},
                "properties": {"title": {"title": [{"text": {"content": page_title}}]}},
            },
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            new_page_id = response.json().get("id")
            state.set_page_id(state_key, new_page_id)
            state.save()
            return ("created", new_page_id)
        else:
            print(f"{RED}Failed to create Notion page: {file_name} | Error: {response.status_code} - {response.text}{RESET}")
            return ("failed", None)