import argparse
import os

# ---------------------------------------------------------------------------
# Parse arguments and configure BASE_DIR *before* importing any module that
# captures it from config at import time (notion_api, image_uploader, …).
# ---------------------------------------------------------------------------
def _parse_args():
    parser = argparse.ArgumentParser(
        description="Sync local Markdown files to Notion.",
    )
    parser.add_argument(
        "docs_dir",
        help="Path to the folder containing Markdown files to sync (e.g. /path/to/docs).",
    )
    parser.add_argument(
        "--root-page-id",
        metavar="PAGE_ID",
        help="Notion page ID to sync under. Required on the first run; stored in sync_state.json afterwards.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created, updated, or deleted without making any changes to Notion.",
    )
    return parser.parse_args()

_args = _parse_args()

import config  # noqa: E402
config.BASE_DIR = os.path.abspath(_args.docs_dir)

import time  # noqa: E402
from utils import find_md_files  # noqa: E402
from notion_api import upload_markdown_file_to_notion, delete_notion_page_if_missing, reconcile_state  # noqa: E402
from markdown_parser import replace_md_links  # noqa: E402
from config import RED, YELLOW, GREEN, RESET  # noqa: E402
from sync_state import state  # noqa: E402

def sync_markdown_to_notion():
    """
    Finds local Markdown files, syncs them to Notion by creating or updating pages,
    and updates internal links in each file to point to the corresponding Notion page.
    """

    # Start the timer to measure execution time.
    start_time = time.time()
    dry_run = _args.dry_run

    if dry_run:
        print(f"{YELLOW}DRY RUN — no changes will be made to Notion.{RESET}\n")

    # Load persistent state from disk (lives inside the docs folder).
    state.load()

    # Resolve Notion root page ID: CLI arg takes precedence, then state, then error.
    if _args.root_page_id:
        state.set_notion_root_page_id(_args.root_page_id)
        if not dry_run:
            state.save()
    elif not state.get_notion_root_page_id():
        print(f"{RED}Error: Notion root page ID is not set.{RESET}")
        print(f"Pass it once with --root-page-id <PAGE_ID> and it will be saved for future runs.")
        return

    # Phase 0: Locate all Markdown (.md) files in the base directory.
    md_files = find_md_files(config.BASE_DIR)
    total_files = len(md_files)
    if total_files == 0:
        print(f"{YELLOW}No Markdown (.md) files found.{RESET}")
        return

    # On first run, populate state by walking the existing Notion tree.
    reconcile_state(md_files)

    # Archive Notion pages corresponding to markdown files that no longer exist.
    deleted_pages = delete_notion_page_if_missing(md_files, dry_run=dry_run)
    if deleted_pages:
        label = "Would archive" if dry_run else "Archived"
        print(f"{YELLOW}{label} {deleted_pages} missing page(s).{RESET}")
    print(f"Found {total_files} Markdown file(s). Starting sync...")

    # Phase 1: Map files to Notion pages (create if missing).
    md_to_notion = {}
    new_pages = 0
    updated_pages = 0
    skipped_pages = 0
    failed_uploads = 0
    failed_files = []
    dry_run_new = set()  # Track new pages so Phase 2 doesn't report them again.

    for md_file in md_files:
        result = upload_markdown_file_to_notion(md_file, update_content=False, dry_run=dry_run)
        if isinstance(result, tuple):
            status, page_id = result
        else:
            status, page_id = result, None

        if status == "created":
            new_pages += 1
            if dry_run:
                dry_run_new.add(md_file)
        elif status == "updated":
            updated_pages += 1
        elif status == "skipped":
            skipped_pages += 1
        elif status == "failed":
            failed_uploads += 1
            failed_files.append(md_file)
            continue

        if page_id:
            filename = os.path.basename(md_file)
            notion_url = f"https://www.notion.so/{filename}-{page_id.replace('-', '')}"
            md_to_notion[filename] = notion_url
            print(f"Mapped {filename} -> {notion_url}")
        elif not dry_run:
            print(f"{RED}Warning: No page ID returned for {md_file}.{RESET}")

    # Phase 2: Sync page content (diff and patch existing pages).
    for md_file in md_files:
        if md_file in dry_run_new:
            continue  # Already reported as "would create" in Phase 1.
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                md_content = f.read()
        except Exception as e:
            print(f"{RED}Error reading {md_file}: {e}{RESET}")
            continue

        updated_content = replace_md_links(md_content, md_to_notion)
        result = upload_markdown_file_to_notion(md_file, update_content=True,
                                                new_content=updated_content, dry_run=dry_run)
        if isinstance(result, tuple):
            status, _ = result
        else:
            status = result

        if status == "updated":
            updated_pages += 1
            if not dry_run:
                print(f"Updated content for {os.path.basename(md_file)}")
        elif status == "failed":
            failed_uploads += 1
            failed_files.append(md_file)

    # Calculate the total execution time.
    end_time = time.time()
    elapsed_time = end_time - start_time

    # Recalculate skipped count.
    skipped_pages = total_files - (new_pages + updated_pages + failed_uploads)

    # Display the final sync summary.
    prefix = "[dry] Would " if dry_run else ""
    print(f"\n======{'Dry Run ' if dry_run else ''}Sync Summary======")
    print(f"Total Files: {total_files}")

    if new_pages > 0:
        print(f"{GREEN}{prefix}Create: {new_pages}{RESET}")
    if updated_pages > 0:
        print(f"{GREEN}{prefix}Update: {updated_pages}{RESET}")
    if skipped_pages > 0:
        print(f"Skipped: {skipped_pages} (Already up to date)")
    if deleted_pages > 0:
        print(f"{YELLOW}{prefix}Archive: {deleted_pages}{RESET}")

    # Report any failed uploads.
    if failed_uploads > 0:
        print(f"{RED}Failed: {failed_uploads}/{total_files}{RESET}")
        print("\nThe following files failed to sync:")
        for file in failed_files:
            print(f"{RED} - {str(file)}{RESET}")

    # Success message when all files have synced without failures.
    if (new_pages > 0 or updated_pages > 0 or deleted_pages > 0 or skipped_pages > 0) and failed_uploads == 0:
        msg = "Dry run complete — no changes made to Notion." if dry_run else "All files synced successfully!"
        print(f"{GREEN}{msg}{RESET}")

    # Total time taken
    hours, remainder = divmod(elapsed_time, 3600)
    minutes, seconds = divmod(remainder, 60)
    print(f"\nTotal time taken: {int(hours)}h {int(minutes)}m {seconds:.2f}s")

if __name__ == "__main__":
    try:
        sync_markdown_to_notion()
    except KeyboardInterrupt:
        print(f"\n{RED}Aborted{RESET}")
