import difflib
import os

_BOLD  = "\033[1m"
_CYAN  = "\033[36m"
_RED   = "\033[31m"
_GREEN = "\033[32m"
_RESET = "\033[0m"


def format_diff(old_text: str, new_text: str, filepath: str) -> str:
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
    out = []
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            out.append(f"{_BOLD}{line}{_RESET}")
        elif line.startswith("@@"):
            out.append(f"{_CYAN}{line}{_RESET}")
        elif line.startswith("-"):
            out.append(f"{_RED}{line}{_RESET}")
        elif line.startswith("+"):
            out.append(f"{_GREEN}{line}{_RESET}")
        else:
            out.append(line)
    return "\n".join(out)


def find_md_files(base_dir):
    """Find all Markdown files recursively."""
    md_files = []
    for root, _, files in os.walk(base_dir):
        for file in files:
            if file.endswith(".md"):
                md_files.append(os.path.join(root, file))
    return md_files