#!/usr/bin/env python3
"""
strip_notion_ids.py

Strips Notion's UUID suffixes from exported markdown filenames and folders,
rewrites all internal cross-page links, and updates sync_state.json so the
sync tool keeps its mappings intact.

Usage:
    python strip_notion_ids.py <docs_dir> [--dry-run]
"""

import argparse
import json
import os
import re
import urllib.parse

# Notion appends a space + 32 hex chars to every exported name.
UUID_RE = re.compile(r' [0-9a-f]{32}$', re.IGNORECASE)

# Notion truncates exported filenames to 50 characters. Prefix matching is only
# trusted when the matched prefix is at least this long (after normalization),
# to avoid false positives on short titles.
NOTION_FILENAME_TRUNCATION = 48


def strip_uuid(name: str) -> str:
    root, ext = os.path.splitext(name)
    cleaned = UUID_RE.sub('', root)
    return cleaned + ext


def collect_renames(docs_dir: str) -> list[tuple[str, str]]:
    """Walk bottom-up so deepest paths are renamed first."""
    renames = []
    for dirpath, dirnames, filenames in os.walk(docs_dir, topdown=False):
        for filename in filenames:
            if filename == "sync_state.json":
                continue
            new_name = strip_uuid(filename)
            if new_name != filename:
                renames.append((
                    os.path.join(dirpath, filename),
                    os.path.join(dirpath, new_name),
                ))
        dirname = os.path.basename(dirpath)
        new_dirname = strip_uuid(dirname)
        if new_dirname != dirname:
            renames.append((
                dirpath,
                os.path.join(os.path.dirname(dirpath), new_dirname),
            ))
    return renames


def _normalize(s: str) -> str:
    """Lowercase, strip non-alphanumeric chars, collapse whitespace."""
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r" +", " ", s).strip()


def build_uuid_to_path_map(docs_dir: str) -> dict[str, str]:
    """UUID → clean relative path. Only populated when files still carry UUID suffixes."""
    uuid_map = {}
    for dirpath, _, filenames in os.walk(docs_dir):
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            root, ext = os.path.splitext(filename)
            m = UUID_RE.search(root)
            if not m:
                continue
            uuid = m.group(0).strip()
            clean_name = UUID_RE.sub("", root) + ext
            clean_parts = [UUID_RE.sub("", p) for p in os.path.relpath(dirpath, docs_dir).split(os.sep)]
            clean_dir = os.path.join(docs_dir, *clean_parts) if clean_parts != ["."] else docs_dir
            uuid_map[uuid] = os.path.relpath(os.path.join(clean_dir, clean_name), docs_dir).replace(os.sep, "/")
    return uuid_map


def _prefix_match(url_title: str, title_map: dict[str, str]) -> str | None:
    """Return the path whose normalized title is the longest prefix of url_title,
    but only if that prefix is at least NOTION_FILENAME_TRUNCATION chars long
    (guarding against false positives on short titles)."""
    best_path, best_len = None, 0
    for key, path in title_map.items():
        if len(key) >= NOTION_FILENAME_TRUNCATION and url_title.startswith(key) and len(key) > best_len:
            best_path, best_len = path, len(key)
    return best_path


def build_title_to_path_map(docs_dir: str) -> dict[str, str]:
    """Normalized page title → relative path. Fallback for already-stripped files."""
    title_map = {}
    for dirpath, _, filenames in os.walk(docs_dir):
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            root, _ = os.path.splitext(filename)
            key = _normalize(root)
            if key:
                path = os.path.relpath(os.path.join(dirpath, filename), docs_dir).replace(os.sep, "/")
                title_map[key] = path
    return title_map


def rewrite_notion_urls(md_path: str, docs_dir: str, uuid_map: dict[str, str],
                        title_map: dict[str, str], dry_run: bool) -> bool:
    """Replace notion.so URLs in markdown link hrefs with relative file paths.
    Tries UUID match first; falls back to normalized title slug. Unresolved URLs stay."""
    with open(md_path, encoding="utf-8") as f:
        original = f.read()

    def replace(match):
        text, url = match.group(1), match.group(2)
        if "notion.so" not in url:
            return match.group(0)
        m = re.search(r"([0-9a-f]{32})(?:\?|$)", url)
        if not m:
            return match.group(0)
        uuid = m.group(1)

        # Primary: UUID found in filename (fresh export).
        target_rel = uuid_map.get(uuid)

        # Fallback: match by normalized slug title (files already stripped).
        if not target_rel:
            slug_m = re.search(r"notion\.so/(.+?)(?:\?|$)", url)
            if slug_m:
                slug = re.sub(r"-[0-9a-f]{32}$", "", slug_m.group(1), flags=re.IGNORECASE)
                title = _normalize(slug.replace("-", " "))
                target_rel = title_map.get(title) or _prefix_match(title, title_map)

        if not target_rel:
            return match.group(0)

        rel = os.path.relpath(os.path.join(docs_dir, target_rel),
                              os.path.dirname(md_path)).replace(os.sep, "/")
        return f"[{text}]({rel})"

    updated = re.sub(r'\[([^\]]*)\]\(([^)]+)\)', replace, original)
    if updated != original and not dry_run:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(updated)
    return updated != original


def build_component_map(renames: list[tuple[str, str]]) -> dict[str, str]:
    """Map each old filename/dirname component to its new name (decoded)."""
    mapping = {}
    for old, new in renames:
        mapping[os.path.basename(old)] = os.path.basename(new)
    return mapping


def rewrite_links(md_path: str, component_map: dict[str, str], dry_run: bool) -> bool:
    with open(md_path, encoding="utf-8") as f:
        original = f.read()

    def replace(match):
        text, url = match.group(1), match.group(2)
        decoded = urllib.parse.unquote(url)
        parts = decoded.split("/")
        new_parts = [component_map.get(p, p) for p in parts]
        new_decoded = "/".join(new_parts)
        if new_decoded == decoded:
            return match.group(0)
        new_url = urllib.parse.quote(new_decoded, safe="/") if url != decoded else new_decoded
        return f"[{text}]({new_url})"

    updated = re.sub(r'\[([^\]]*)\]\(([^)]+)\)', replace, original)
    if updated != original and not dry_run:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(updated)
    return updated != original


def update_sync_state(docs_dir: str, component_map: dict[str, str], dry_run: bool):
    state_path = os.path.join(docs_dir, "sync_state.json")
    if not os.path.exists(state_path):
        return
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    def remap(d: dict) -> tuple[dict, int]:
        result, changes = {}, 0
        for key, val in d.items():
            parts = key.replace("\\", "/").split("/")
            new_parts = [component_map.get(p, p) for p in parts]
            new_key = "/".join(new_parts)
            result[new_key] = val
            if new_key != key:
                changes += 1
        return result, changes

    total = 0
    for section in ("pages", "folders", "images"):
        if section in state:
            state[section], n = remap(state[section])
            total += n

    if total and not dry_run:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    return total


def main():
    parser = argparse.ArgumentParser(description="Strip Notion UUID suffixes from an exported docs folder.")
    parser.add_argument("docs_dir", help="Path to the exported docs folder.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying anything.")
    args = parser.parse_args()

    docs_dir = os.path.abspath(args.docs_dir)
    dry_run = args.dry_run

    if dry_run:
        print("DRY RUN — nothing will be modified.\n")

    renames = collect_renames(docs_dir)

    # Check for name collisions before doing anything.
    seen = {}
    for _, new_path in renames:
        if new_path in seen:
            print(f"ERROR: collision — two entries would rename to: {new_path}")
            return
        seen[new_path] = True

    uuid_map = build_uuid_to_path_map(docs_dir)
    title_map = build_title_to_path_map(docs_dir)
    component_map = build_component_map(renames)

    # 1. Rewrite relative links and notion.so URLs (files still at old paths).
    link_changes = 0
    notion_changes = 0
    for dirpath, _, filenames in os.walk(docs_dir):
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            md_path = os.path.join(dirpath, filename)
            if component_map and rewrite_links(md_path, component_map, dry_run):
                print(f"  {'[dry] ' if dry_run else ''}links updated:        {os.path.relpath(md_path, docs_dir)}")
                link_changes += 1
            if rewrite_notion_urls(md_path, docs_dir, uuid_map, title_map, dry_run):
                print(f"  {'[dry] ' if dry_run else ''}notion URLs resolved:  {os.path.relpath(md_path, docs_dir)}")
                notion_changes += 1

    # 2. Update sync_state.json.
    state_changes = update_sync_state(docs_dir, component_map, dry_run)
    if state_changes:
        print(f"  {'[dry] ' if dry_run else ''}sync_state.json — {state_changes} key(s) updated")

    # 3. Rename files and folders (bottom-up).
    for old, new in renames:
        print(f"  {'[dry] ' if dry_run else ''}{os.path.relpath(old, docs_dir)}  →  {os.path.relpath(new, docs_dir)}")
        if not dry_run:
            os.rename(old, new)

    if not renames and not notion_changes:
        print("Nothing to do.")
        return

    print(f"\n{'Would rename' if dry_run else 'Renamed'} {len(renames)} item(s), "
          f"updated relative links in {link_changes} file(s), "
          f"resolved notion.so URLs in {notion_changes} file(s).")


if __name__ == "__main__":
    main()

