"""
notion_to_md.py — Download a Notion page tree and convert it to local Markdown files.

Entry point: pull_from_notion(target_dir, root_page_id)
"""

import hashlib
import os
import re
import urllib.parse
import requests

from config import HEADERS, RED, YELLOW, GREEN, RESET
from notion_api import session, REQUEST_TIMEOUT
from sync_state import state

# ---------------------------------------------------------------------------
# Inline rich_text → Markdown
# ---------------------------------------------------------------------------

def rich_text_to_md(rich_text: list) -> str:
    """Convert a Notion rich_text array to a Markdown inline string."""
    result = ""
    for token in rich_text:
        content = token.get("text", {}).get("content", "")
        link = token.get("text", {}).get("link")
        ann = token.get("annotations", {})

        bold = ann.get("bold", False)
        italic = ann.get("italic", False)
        code = ann.get("code", False)
        strike = ann.get("strikethrough", False)

        if code:
            content = f"`{content}`"
        else:
            if strike:
                content = f"~~{content}~~"
            if bold and italic:
                content = f"***{content}***"
            elif bold:
                content = f"**{content}**"
            elif italic:
                content = f"*{content}*"

        if link:
            content = f"[{content}]({link['url']})"

        result += content
    return result


# ---------------------------------------------------------------------------
# Block children fetching (recursive, handles pagination + nested children)
# ---------------------------------------------------------------------------

# Block types whose children are fetched during block retrieval.
_RECURSE_TYPES = {
    "toggle",
    "column_list",
    "column",
    "callout",              # callout body children
    "table",                # needs table_row children
    "bulleted_list_item",   # nested sub-bullets
    "numbered_list_item",   # nested sub-items
    "to_do",                # nested sub-tasks
}

# Maximum nesting depth for list item recursion (prevents runaway API calls
# on pathologically deep pages).
_MAX_RECURSE_DEPTH = 4


def fetch_blocks_recursive(block_id: str, depth: int = 0) -> list:
    """Fetch all blocks under block_id, recursively expanding children only
    for block types that require nested content for Markdown rendering."""
    blocks = []
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    next_cursor = None

    while True:
        params = {"page_size": 100}
        if next_cursor:
            params["start_cursor"] = next_cursor
        resp = session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"{RED}Error fetching blocks for {block_id}: {resp.status_code}{RESET}")
            break
        data = resp.json()
        for block in data.get("results", []):
            btype = block.get("type")
            if block.get("has_children") and btype in _RECURSE_TYPES and depth < _MAX_RECURSE_DEPTH:
                block["_children"] = fetch_blocks_recursive(block["id"], depth + 1)
            blocks.append(block)
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break

    return blocks


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------

def _sanitise_filename(name: str) -> str:
    """Replace characters that are invalid in filenames."""
    return re.sub(r'[<>:"/\\|?*]', "-", name).strip()


def download_image(url: str, dest_dir: str, hint: str = "image") -> str | None:
    """Download an image from url into dest_dir/assets/. Returns relative path or None."""
    assets_dir = os.path.join(dest_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # Prefer the original filename embedded in the URL (present in Notion S3 URLs).
    # URL-decode so the saved file and the markdown reference both use plain text names
    # (e.g. "white_Fraud_Overview_(1).png" not "white_Fraud_Overview_%281%29.png").
    url_path = url.split("?")[0]
    url_basename = _sanitise_filename(urllib.parse.unquote(os.path.basename(url_path)))
    ext = os.path.splitext(urllib.parse.unquote(url_path))[1] or ".png"
    base_filename = url_basename if url_basename else (_sanitise_filename(hint) + ext)

    # Avoid overwriting an existing file with the same name (e.g. multiple
    # Notion screenshots all called "image.png"). Append _2, _3, … as needed.
    filename = base_filename
    stem, suffix = os.path.splitext(base_filename)
    counter = 2
    while os.path.exists(os.path.join(assets_dir, filename)):
        filename = f"{stem}_{counter}{suffix}"
        counter += 1

    dest_path = os.path.join(assets_dir, filename)

    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return os.path.join("assets", filename)
        else:
            print(f"{YELLOW}Failed to download image ({resp.status_code}): {url}{RESET}")
    except Exception as e:
        print(f"{YELLOW}Exception downloading image: {e}{RESET}")
    return None




# ---------------------------------------------------------------------------
# Blocks → Markdown
# ---------------------------------------------------------------------------

def blocks_to_md(blocks: list, page_dir: str, indent: int = 0, page_title: str = "") -> str:
    """Convert a list of Notion blocks to a Markdown string.

    page_dir   – directory where the page's .md file is written (for image paths).
    indent     – nesting level for list items.
    page_title – title of the current page; child_page links are placed in a
                 sub-folder of this name so we need it to build correct relative paths.
    """
    lines = []
    prefix = "  " * indent

    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})
        children = block.get("_children", [])

        if btype == "paragraph":
            text = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"{prefix}{text}" if text else "")

        elif btype in ("heading_1", "heading_2", "heading_3"):
            level = int(btype[-1])
            text = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"{'#' * level} {text}")

        elif btype == "bulleted_list_item":
            text = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"{prefix}- {text}")
            if children:
                lines.append(blocks_to_md(children, page_dir, indent + 1))

        elif btype == "numbered_list_item":
            text = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"{prefix}1. {text}")
            if children:
                lines.append(blocks_to_md(children, page_dir, indent + 1))

        elif btype == "to_do":
            checked = "x" if data.get("checked") else " "
            text = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"{prefix}- [{checked}] {text}")
            if children:
                lines.append(blocks_to_md(children, page_dir, indent + 1))

        elif btype == "code":
            lang = data.get("language", "plain text")
            code = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"```{lang}\n{code}\n```")

        elif btype == "callout":
            text = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"> [callout]: {text}")
            if children:
                lines.append(blocks_to_md(children, page_dir, indent + 1, page_title=page_title))

        elif btype == "bookmark":
            url = data.get("url", "")
            if url:
                lines.append(f"> [bookmark]: {url}")
            # else: empty bookmark, skip silently

        elif btype == "quote":
            text = rich_text_to_md(data.get("rich_text", []))
            lines.append(f"> {text}")

        elif btype == "divider":
            lines.append("---")

        elif btype == "image":
            img = data
            img_type = img.get("type")
            url = img.get(img_type, {}).get("url", "") if img_type else ""
            caption_list = img.get("caption", [])
            alt = rich_text_to_md(caption_list) if caption_list else "image"
            hint = _sanitise_filename(alt[:40]) or "image"
            local_path = download_image(url, page_dir, hint)
            if local_path:
                lines.append(f"![{alt}]({local_path})")
            elif url:
                lines.append(f"![{alt}]({url})")

        elif btype == "table":
            rows = [b for b in children if b.get("type") == "table_row"]
            if rows:
                table_lines = []
                for i, row in enumerate(rows):
                    cells = row.get("table_row", {}).get("cells", [])
                    cell_texts = [rich_text_to_md(c) for c in cells]
                    table_lines.append("| " + " | ".join(cell_texts) + " |")
                    if i == 0:
                        table_lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
                lines.append("\n".join(table_lines))

        elif btype == "child_page":
            title = data.get("title", "")
            safe_title = _sanitise_filename(title)
            # Child pages live in a sub-folder named after the current page.
            if page_title:
                subfolder = _sanitise_filename(page_title)
                lines.append(f"[{title}]({subfolder}/{safe_title}.md)")
            else:
                lines.append(f"[{title}]({safe_title}.md)")

        else:
            # Best-effort fallback for callouts, toggles, columns, etc.
            rich = data.get("rich_text", [])
            text = rich_text_to_md(rich) if rich else f"[{btype}]"
            lines.append(f"> [{btype}]: {text}")
            if children:
                lines.append(blocks_to_md(children, page_dir, indent + 1))

    return "\n\n".join(line for line in lines if line is not None)


# ---------------------------------------------------------------------------
# Pull orchestration
# ---------------------------------------------------------------------------

def _normalize_id(notion_id: str) -> str:
    """Normalize a Notion ID by removing hyphens for reliable comparison."""
    return notion_id.replace("-", "")


def _get_page_parent_id(page_id: str) -> str | None:
    """Return the normalized parent page ID of a Notion page.

    Returns None if the parent is not a page (e.g. a database or workspace root),
    or if the request fails.
    """
    resp = session.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    parent = resp.json().get("parent", {})
    if parent.get("type") == "page_id":
        return _normalize_id(parent["page_id"])
    return None


def _page_title(page_id: str) -> str:
    """Retrieve the title of a Notion page via the Pages API."""
    resp = session.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        return page_id
    props = resp.json().get("properties", {})
    title_prop = props.get("title", {})
    rich = title_prop.get("title", [])
    return rich_text_to_md(rich) or page_id


def _pull_page(page_id: str, page_title: str, dest_dir: str, base_dir: str):
    """Fetch a single Notion page, convert to Markdown, and write to disk."""
    os.makedirs(dest_dir, exist_ok=True)
    filename = _sanitise_filename(page_title) + ".md"
    filepath = os.path.join(dest_dir, filename)

    blocks = fetch_blocks_recursive(page_id)
    md_content = f"# {page_title}\n\n" + blocks_to_md(blocks, dest_dir, page_title=page_title)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md_content)

    content_hash = hashlib.sha256(md_content.encode()).hexdigest()
    state_key = os.path.relpath(filepath, base_dir)
    state.set_page_id(state_key, page_id)
    state.set_page_hash(state_key, content_hash)

    print(f"{GREEN}Downloaded: {os.path.relpath(filepath, base_dir)}{RESET}")


def _pull_children(page_id: str, page_title: str, dest_dir: str, base_dir: str,
                   seen_ids: set | None = None):
    """Recursively pull child pages of page_id into dest_dir/<page_title>/.

    seen_ids  – set of already-downloaded page IDs (normalized, no hyphens).
                Shared across the entire recursive traversal to prevent a page
                that appears as a child_page block in multiple parents from
                being downloaded more than once.
    """
    if seen_ids is None:
        seen_ids = set()

    norm_page_id = _normalize_id(page_id)
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    next_cursor = None
    while True:
        params = {"page_size": 100}
        if next_cursor:
            params["start_cursor"] = next_cursor
        resp = session.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"{RED}Error fetching children of '{page_title}': {resp.status_code}{RESET}")
            break
        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") == "child_page":
                child_id = block["id"]
                norm_child_id = _normalize_id(child_id)

                # Safety net: skip pages we've already downloaded.
                if norm_child_id in seen_ids:
                    continue

                # Verify this page is truly a direct child of the current page.
                # Notion can leave stale child_page blocks when pages are moved,
                # causing the same page to appear under multiple parents.
                actual_parent_id = _get_page_parent_id(child_id)
                if actual_parent_id is not None and actual_parent_id != norm_page_id:
                    print(f"{YELLOW}Skipping '{block['child_page']['title']}' under "
                          f"'{page_title}' — its actual parent differs.{RESET}")
                    continue

                seen_ids.add(norm_child_id)
                child_title = block["child_page"]["title"].strip()
                child_dir = os.path.join(dest_dir, _sanitise_filename(page_title))
                _pull_page(child_id, child_title, child_dir, base_dir)
                _pull_children(child_id, child_title, child_dir, base_dir, seen_ids)
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break


def _pull_database(database_id: str, db_title: str, target_dir: str):
    """Pull every page in a Notion database into target_dir.

    Also pulls the content blocks of the database page itself (the text that
    sits above the table view in Notion) and saves it as <db_title>.md.
    """
    print(f"Database: {db_title}")

    # Pull the database page's own content blocks as the root file.
    _pull_page(database_id, db_title, target_dir, target_dir)

    # Rows (database pages) go into a subfolder named after the database,
    # consistent with how page-tree children are placed under their parent.
    rows_dir = os.path.join(target_dir, _sanitise_filename(db_title))

    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    next_cursor = None
    while True:
        body = {"page_size": 100}
        if next_cursor:
            body["start_cursor"] = next_cursor
        resp = session.post(url, headers=HEADERS, json=body, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"{RED}Error querying database {database_id}: {resp.status_code}{RESET}")
            break
        data = resp.json()
        for row in data.get("results", []):
            page_id = row["id"]
            # Find the title property (type=="title")
            title = ""
            for prop in row.get("properties", {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                    break
            if not title:
                title = page_id
            _pull_page(page_id, title, rows_dir, target_dir)
            _pull_children(page_id, title, rows_dir, target_dir)
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break


def pull_from_notion(target_dir: str, root_id: str):
    """Entry point: pull a full Notion page tree or database into target_dir.

    Auto-detects whether root_id is a regular page or a database and
    handles each accordingly.
    """
    os.makedirs(target_dir, exist_ok=True)
    state.load()
    state.set_notion_root_page_id(root_id)

    print(f"Pulling from Notion {root_id} into {target_dir} ...")

    # Try as a page first, fall back to database.
    resp = session.get(f"https://api.notion.com/v1/pages/{root_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 200:
        root_title = _page_title(root_id)
        print(f"Root page: {root_title}")
        _pull_page(root_id, root_title, target_dir, target_dir)
        _pull_children(root_id, root_title, target_dir, target_dir)
    else:
        resp2 = session.get(f"https://api.notion.com/v1/databases/{root_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp2.status_code == 200:
            db = resp2.json()
            db_title = "".join(t.get("plain_text", "") for t in db.get("title", []))
            _pull_database(root_id, db_title, target_dir)
        else:
            print(f"{RED}Could not resolve {root_id} as a page or database.{RESET}")
            return

    state.save()
    total = len(state.all_pages())
    print(f"\n{GREEN}Pull complete — {total} page(s) downloaded to {target_dir}{RESET}")
