"""
notion_to_md.py — Download a Notion page tree and convert it to local Markdown files.

Entry point: pull_from_notion(target_dir, root_page_id)
"""

import difflib
import hashlib
import os
import re
import urllib.parse
import requests

from config import HEADERS, RED, YELLOW, GREEN, RESET
from notion_api import session, REQUEST_TIMEOUT, _compute_notion_blocks_hash
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
    """Download an image from url into dest_dir/assets/. Returns relative path or None.

    Content-aware deduplication: if a file with the same bytes already exists in
    the assets folder (identified by SHA-256), that file is reused and nothing is
    written to disk. This prevents _2, _3, … duplicates accumulating across pulls.
    If two genuinely different images share the same base filename, the second one
    still gets a _2 suffix as before.
    """
    assets_dir = os.path.join(dest_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # Prefer the original filename embedded in the URL (present in Notion S3 URLs).
    # URL-decode so the saved file and the markdown reference both use plain text names.
    # Strip parentheses so "white_Fraud_Overview_(1).png" becomes "white_Fraud_Overview_1.png"
    # — parentheses are awkward to type and unnecessary in local filenames.
    url_path = url.split("?")[0]
    url_basename = _sanitise_filename(urllib.parse.unquote(os.path.basename(url_path)))
    url_basename = url_basename.replace("(", "").replace(")", "")
    ext = os.path.splitext(urllib.parse.unquote(url_path))[1] or ".png"
    base_filename = url_basename if url_basename else (_sanitise_filename(hint) + ext)
    stem, suffix = os.path.splitext(base_filename)

    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"{YELLOW}Failed to download image ({resp.status_code}): {url}{RESET}")
            return None
    except Exception as e:
        print(f"{YELLOW}Exception downloading image: {e}{RESET}")
        return None

    data = resp.content
    incoming_hash = hashlib.sha256(data).hexdigest()

    # Content-addressed deduplication: scan every existing file in assets.
    # If any file has identical bytes, reuse it regardless of its name.
    # This handles cases where the URL-embedded filename differs from the
    # locally cleaned-up name (e.g. Notion stored "image_2_2.png" but we
    # renamed it to "image.png" after a previous pull cleanup).
    # Sort so shorter (cleaner) names are preferred when multiple files share
    # the same content (e.g. "image.png" beats "image_2_2.png").
    for existing in sorted(os.listdir(assets_dir), key=lambda n: (len(n), n)):
        if not existing.lower().endswith(suffix.lower()):
            continue
        existing_path = os.path.join(assets_dir, existing)
        with open(existing_path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() == incoming_hash:
                return os.path.join("assets", existing)

    # No content match — pick the next available name and write.
    filename = base_filename
    counter = 2
    while os.path.exists(os.path.join(assets_dir, filename)):
        filename = f"{stem}_{counter}{suffix}"
        counter += 1

    with open(os.path.join(assets_dir, filename), "wb") as f:
        f.write(data)
    return os.path.join("assets", filename)




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
            # Skip — child pages are pulled as separate .md files by _pull_children.
            # Emitting inline links here causes duplicates alongside Notion's own
            # child_page blocks, and those links are filtered out during sync anyway.
            pass

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


def _get_page_meta(page_id: str) -> tuple[str | None, str | None]:
    """Return (normalized_parent_page_id, last_edited_time) for a Notion page.

    parent_page_id is None if the parent is not a page (e.g. a database or
    workspace root), or if the request fails.
    last_edited_time is the ISO-8601 string from Notion, or None on failure.
    """
    resp = session.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        return None, None
    data = resp.json()
    parent = data.get("parent", {})
    parent_id = _normalize_id(parent["page_id"]) if parent.get("type") == "page_id" else None
    return parent_id, data.get("last_edited_time")


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


_BOLD  = "\033[1m"
_CYAN  = "\033[36m"

def _format_diff(old_text: str, new_text: str, filepath: str) -> str:
    """Return a coloured unified diff string (git-style) comparing old to new.

    Header lines (--- / +++) are bold, @@ hunk lines are cyan,
    removed lines are red, added lines are green.
    Returns an empty string when there are no differences.
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        lineterm="",
    ))
    if not diff:
        return ""
    coloured = []
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            coloured.append(f"{_BOLD}{line}{RESET}")
        elif line.startswith("@@"):
            coloured.append(f"{_CYAN}{line}{RESET}")
        elif line.startswith("-"):
            coloured.append(f"{RED}{line}{RESET}")
        elif line.startswith("+"):
            coloured.append(f"{GREEN}{line}{RESET}")
        else:
            coloured.append(line)
    return "\n".join(coloured)


def _pull_page(page_id: str, page_title: str, dest_dir: str, base_dir: str,
               dry_run: bool = False, stats: dict | None = None,
               last_edited_time: str | None = None, show_diff: bool = False):
    """Fetch a single Notion page, convert to Markdown, and write to disk.

    last_edited_time — ISO-8601 timestamp from Notion's page object.  When
        provided the dry-run path uses it as a fast pre-check: if it matches
        the stored value the page is classified as unchanged immediately,
        without fetching any blocks.  Apply mode stores it so that future
        dry-runs benefit from the optimisation.
    show_diff — when True and the page is classified as "update", print a
        coloured unified diff (old = local file, new = Notion content).

    In dry_run mode, classifies each page as:
      create    — file does not exist locally yet
      update    — Notion content changed since last pull/sync
      unchanged — Notion content matches stored fingerprint; apply would be a no-op
    """
    filename = _sanitise_filename(page_title) + ".md"
    filepath = os.path.join(dest_dir, filename)
    rel_path = os.path.relpath(filepath, base_dir)

    if dry_run:
        # Fast path: if the file exists and Notion's edit timestamp hasn't moved,
        # we can skip the expensive block fetch entirely.
        if os.path.exists(filepath) and last_edited_time is not None:
            stored_last_edited = state.get_notion_last_edited(rel_path)
            if stored_last_edited == last_edited_time:
                if stats is not None:
                    stats["unchanged"] = stats.get("unchanged", 0) + 1
                print(f"  Unchanged: {rel_path}")
                return

        # Full check: fetch blocks and compare content fingerprint.
        blocks = fetch_blocks_recursive(page_id)
        current_notion_hash = _compute_notion_blocks_hash(blocks)
        stored_notion_hash = state.get_notion_hash(rel_path)

        if not os.path.exists(filepath):
            label, color = "create", GREEN
        elif current_notion_hash == stored_notion_hash:
            label, color = "unchanged", RESET
        else:
            label, color = "update", YELLOW

        if stats is not None:
            stats[label] = stats.get(label, 0) + 1

        if label == "unchanged":
            print(f"  Unchanged: {rel_path}")
            # Seed the timestamp so the next dry-run can skip the block fetch.
            if last_edited_time is not None:
                state.set_notion_last_edited(rel_path, last_edited_time)
        else:
            print(f"{color}[dry] Would {label}: {rel_path}{RESET}")
            if show_diff and label == "update":
                new_md = f"# {page_title}\n\n" + blocks_to_md(blocks, dest_dir, page_title=page_title)
                with open(filepath, encoding="utf-8") as fh:
                    old_md = fh.read()
                diff_output = _format_diff(old_md, new_md, rel_path)
                if diff_output:
                    print(diff_output)
                else:
                    print(f"  (content identical — only block metadata changed; hash will be reseeded on --apply)")
        return

    os.makedirs(dest_dir, exist_ok=True)
    blocks = fetch_blocks_recursive(page_id)
    md_content = f"# {page_title}\n\n" + blocks_to_md(blocks, dest_dir, page_title=page_title)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md_content)

    content_hash = hashlib.sha256(md_content.encode()).hexdigest()
    state_key = os.path.relpath(filepath, base_dir)
    state.set_page_id(state_key, page_id)
    state.set_page_hash(state_key, content_hash)
    # Seed the Notion-side fingerprint from the blocks just fetched.
    # This establishes the baseline for remote drift detection: the next sync
    # of this file can detect if Notion was edited between the pull and the push.
    state.set_notion_hash(state_key, _compute_notion_blocks_hash(blocks))
    if last_edited_time is not None:
        state.set_notion_last_edited(state_key, last_edited_time)

    print(f"{GREEN}Downloaded: {rel_path}{RESET}")


def _pull_children(page_id: str, page_title: str, dest_dir: str, base_dir: str,
                   seen_ids: set | None = None, dry_run: bool = False, stats: dict | None = None,
                   show_diff: bool = False):
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
                # _get_page_meta returns (parent_id, last_edited_time) in one call.
                actual_parent_id, child_last_edited = _get_page_meta(child_id)
                if actual_parent_id is not None and actual_parent_id != norm_page_id:
                    print(f"{YELLOW}Skipping '{block['child_page']['title']}' under "
                          f"'{page_title}' — its actual parent differs.{RESET}")
                    continue

                seen_ids.add(norm_child_id)
                child_title = block["child_page"]["title"].strip()
                child_dir = os.path.join(dest_dir, _sanitise_filename(page_title))
                _pull_page(child_id, child_title, child_dir, base_dir, dry_run=dry_run, stats=stats,
                           last_edited_time=child_last_edited, show_diff=show_diff)
                _pull_children(child_id, child_title, child_dir, base_dir, seen_ids, dry_run=dry_run, stats=stats,
                               show_diff=show_diff)
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break


def _pull_database(database_id: str, db_title: str, target_dir: str,
                   dry_run: bool = False, stats: dict | None = None,
                   db_last_edited: str | None = None, show_diff: bool = False):
    """Pull every page in a Notion database into target_dir.

    Also pulls the content blocks of the database page itself (the text that
    sits above the table view in Notion) and saves it as <db_title>.md.
    db_last_edited is the last_edited_time from the /databases/{id} API response;
    passed through to enable the timestamp fast-skip for the cover page.
    """
    print(f"Database: {db_title}")

    # Pull the database page's own content blocks as the root file.
    _pull_page(database_id, db_title, target_dir, target_dir, dry_run=dry_run, stats=stats,
               last_edited_time=db_last_edited, show_diff=show_diff)

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
            row_last_edited = row.get("last_edited_time")
            # Find the title property (type=="title")
            title = ""
            for prop in row.get("properties", {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                    break
            if not title:
                title = page_id
            _pull_page(page_id, title, rows_dir, target_dir, dry_run=dry_run, stats=stats,
                       last_edited_time=row_last_edited, show_diff=show_diff)
            _pull_children(page_id, title, rows_dir, target_dir, dry_run=dry_run, stats=stats,
                           show_diff=show_diff)
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break


def pull_from_notion(target_dir: str, root_id: str, dry_run: bool = False, show_diff: bool = False):
    """Entry point: pull a full Notion page tree or database into target_dir.

    Auto-detects whether root_id is a regular page or a database and
    handles each accordingly.
    In dry_run mode, prints what would be downloaded without writing any
    files, downloading images, or updating sync_state.json.
    """
    if dry_run:
        print(f"{YELLOW}DRY RUN — no files will be written.{RESET}\n")
    else:
        os.makedirs(target_dir, exist_ok=True)
    state.load()
    if not dry_run:
        state.set_notion_root_page_id(root_id)

    print(f"Pulling from Notion {root_id} into {target_dir} ...")

    stats: dict = {}

    # Try as a page first, fall back to database.
    resp = session.get(f"https://api.notion.com/v1/pages/{root_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 200:
        root_data = resp.json()
        root_last_edited = root_data.get("last_edited_time")
        props = root_data.get("properties", {})
        title_prop = props.get("title", {})
        rich = title_prop.get("title", [])
        root_title = rich_text_to_md(rich) or root_id
        print(f"Root page: {root_title}")
        _pull_page(root_id, root_title, target_dir, target_dir, dry_run=dry_run, stats=stats,
                   last_edited_time=root_last_edited, show_diff=show_diff)
        _pull_children(root_id, root_title, target_dir, target_dir, dry_run=dry_run, stats=stats,
                       show_diff=show_diff)
    else:
        resp2 = session.get(f"https://api.notion.com/v1/databases/{root_id}", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp2.status_code == 200:
            db = resp2.json()
            db_title = "".join(t.get("plain_text", "") for t in db.get("title", []))
            db_last_edited = db.get("last_edited_time")
            _pull_database(root_id, db_title, target_dir, dry_run=dry_run, stats=stats,
                           db_last_edited=db_last_edited, show_diff=show_diff)
        else:
            print(f"{RED}Could not resolve {root_id} as a page or database.{RESET}")
            return

    # Always persist state: apply mode saves everything; dry-run mode saves only
    # the notion_last_edited timestamps that were seeded during the run so that
    # the next dry-run can use the fast timestamp-based skip.
    state.save()

    total = stats.get("create", 0) + stats.get("update", 0) + stats.get("unchanged", 0) if dry_run else len(state.all_pages())
    if dry_run:
        print(f"\n======Dry Run Pull Summary======")
        print(f"Total pages: {total}")
        if stats.get("create"):
            print(f"{GREEN}Would create:  {stats['create']}{RESET}")
        if stats.get("update"):
            print(f"{YELLOW}Would update:  {stats['update']}{RESET}")
        if stats.get("unchanged"):
            print(f"Unchanged:     {stats['unchanged']}")
    else:
        print(f"\n{GREEN}Pull complete — {total} page(s) downloaded to {target_dir}{RESET}")
