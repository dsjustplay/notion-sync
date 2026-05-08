import argparse
import os
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Parse arguments and configure BASE_DIR *before* importing any module that
# captures it from config at import time (notion_api, image_uploader, …).
# ---------------------------------------------------------------------------
def _parse_args():
    parser = argparse.ArgumentParser(
        description="Sync local Markdown files to/from Notion.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- push subcommand (push local → Notion) --------------------------------
    sync_parser = subparsers.add_parser(
        "push",
        help="Push local Markdown files to Notion.",
    )
    sync_parser.add_argument(
        "docs_dir",
        help="Path to the folder containing Markdown files to sync.",
    )
    sync_parser.add_argument(
        "--root-page-id",
        metavar="PAGE_ID",
        help="Notion page ID to sync under. Required on the first run; stored in sync_state.json afterwards.",
    )
    sync_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes to Notion. Without this flag the command runs as a dry run and only prints what would change.",
    )
    sync_parser.add_argument(
        "--root-is-file",
        action="store_true",
        help=(
            "Treat the single root .md file as the content of the target page itself. "
            "Its content is written directly to the target page; files in the matching "
            "subfolder become direct children of the target. "
            "Use this when docs_dir contains exactly one .md file paired with a subfolder "
            "of the same name (e.g. 'Fraud Control.md' + 'Fraud Control/')."
        ),
    )
    sync_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite Notion even when remote drift is detected (i.e. the Notion page was "
            "edited directly since the last sync). Without this flag, affected pages are "
            "skipped with a warning so no manual edits are silently lost."
        ),
    )

    # -- pull subcommand (download Notion → local) ----------------------------
    pull_parser = subparsers.add_parser(
        "pull",
        help="Download a Notion page tree to local Markdown files.",
    )
    pull_parser.add_argument(
        "target_dir",
        help="Local directory to write downloaded Markdown files into.",
    )
    pull_parser.add_argument(
        "--root-page-id",
        metavar="PAGE_ID",
        help="Notion page ID to pull from. Required on the first run; stored in sync_state.json afterwards.",
    )
    pull_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write downloaded files to disk. Without this flag the command runs as a dry run and only prints what would be downloaded.",
    )
    pull_parser.add_argument(
        "--diff",
        action="store_true",
        help="In dry-run mode, print a git-style unified diff for every page that would be updated.",
    )
    pull_parser.add_argument(
        "--only-changed",
        action="store_true",
        help=(
            "Only pull pages that Notion has edited since the last sync, "
            "leaving all other local files untouched. "
            "Useful for resolving drift on a single page without overwriting local edits elsewhere."
        ),
    )

    return parser.parse_args()

_args = _parse_args()

import config  # noqa: E402
if _args.command == "push":
    config.BASE_DIR = os.path.abspath(_args.docs_dir)
    config.ROOT_IS_FILE = _args.root_is_file
else:
    config.BASE_DIR = os.path.abspath(_args.target_dir)
    config.ROOT_IS_FILE = False

import time  # noqa: E402
from utils import find_md_files  # noqa: E402
from notion_api import upload_markdown_file_to_notion, delete_notion_page_if_missing, reconcile_state, init_root_context, check_notion_drift  # noqa: E402
from markdown_parser import replace_md_links  # noqa: E402
from config import RED, YELLOW, GREEN, RESET  # noqa: E402
from sync_state import state  # noqa: E402
from notion_to_md import pull_from_notion, pull_only_changed  # noqa: E402

def push_markdown_to_notion():
    """
    Finds local Markdown files, pushes them to Notion by creating or updating pages,
    and updates internal links in each file to point to the corresponding Notion page.
    """

    # Start the timer to measure execution time.
    start_time = time.time()
    dry_run = not _args.apply
    force = _args.force

    if dry_run:
        print(f"{YELLOW}DRY RUN — no changes will be made to Notion.{RESET}\n")

    # Load persistent state from disk (lives inside the docs folder).
    state.load()

    # Resolve Notion root page ID: CLI arg takes precedence, then state, then error.
    if _args.root_page_id:  # argparse converts --root-page-id to root_page_id
        state.set_notion_root_page_id(_args.root_page_id)
        if not dry_run:
            state.save()
    elif not state.get_notion_root_page_id():
        print(f"{RED}Error: Notion root page ID is not set.{RESET}")
        print(f"Pass it once with --root-page-id <PAGE_ID> and it will be saved for future runs.")
        return

    # Detect whether the root is a page tree or a Notion database (cached after first run).
    root_ctx = init_root_context(state.get_notion_root_page_id(), dry_run=dry_run)

    # A standalone database (one that doesn't support block children) cannot receive
    # the root .md file content. Fail early with a clear explanation.
    if root_ctx.is_database() and not root_ctx.root_accepts_blocks and config.ROOT_IS_FILE:
        print(
            f"{RED}Error: --root-is-file is not compatible with a standalone Notion database.{RESET}\n"
            f"A standalone database has no page layer to write the root .md content to.\n"
            f"Use a database that is embedded inside a Notion page (open the database, click '···' → 'Open as page', then share that page), "
            f"or omit --root-is-file and let each .md file become a database row."
        )
        return

    # Phase 0: Locate all Markdown (.md) files in the base directory.
    md_files = find_md_files(config.BASE_DIR)
    total_files = len(md_files)
    if total_files == 0:
        print(f"{YELLOW}No Markdown (.md) files found.{RESET}")
        return

    # On first run, populate state by walking the existing Notion tree.
    reconcile_state(md_files)

    # Pre-flight drift check: warn (dry-run) or abort (apply) if any pages that
    # would be pushed have been independently edited in Notion since the last pull.
    # Must run BEFORE Phase 1 so that creating new child pages (which updates the
    # parent's last_edited_time) does not trigger a false-positive drift alarm.
    if not force:
        drifted = check_notion_drift(md_files, dry_run=dry_run)
        if drifted and not dry_run:
            return  # abort before any writes

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
            name_no_ext = os.path.splitext(filename)[0]
            notion_url = f"https://www.notion.so/{quote(name_no_ext)}-{page_id.replace('-', '')}"
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
                                                new_content=updated_content, dry_run=dry_run,
                                                raw_content=md_content, force=force)
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
        if _args.command == "push":
            push_markdown_to_notion()
        elif _args.command == "pull":
            state.load()
            if _args.only_changed:
                pull_only_changed(config.BASE_DIR, dry_run=not _args.apply,
                                  show_diff=_args.diff and not _args.apply)
            else:
                root_id = _args.root_page_id or state.get_notion_root_page_id()
                if not root_id:
                    print(f"{RED}Error: Notion root page ID is not set.{RESET}")
                    print("Pass it once with --root-page-id <PAGE_ID> and it will be saved for future runs.")
                else:
                    pull_from_notion(config.BASE_DIR, root_id, dry_run=not _args.apply,
                                     show_diff=_args.diff and not _args.apply)
    except KeyboardInterrupt:
        print(f"\n{RED}Aborted{RESET}")
