import json
import os
import config

STATE_FILENAME = "sync_state.json"


def _state_file_path() -> str:
    """Resolve the state file path inside the configured docs folder."""
    return os.path.join(config.BASE_DIR, STATE_FILENAME)


class SyncState:
    """Persistent local state mapping local paths to Notion IDs and image upload cache."""

    def __init__(self):
        self._data = {"pages": {}, "folders": {}, "images": {}, "notion_root_page_id": None}

    def load(self):
        """Load state from disk. Safe to call even if the file does not exist yet."""
        path = _state_file_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            self._data.setdefault("notion_root_page_id", None)
        return self

    def save(self):
        """Persist current state to disk (inside the docs folder)."""
        path = _state_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    # ------------------------------------------------------------------
    # Pages  (key: relative local path, e.g. "docs/guides/intro.md")
    # ------------------------------------------------------------------

    def get_page_id(self, local_path: str) -> str | None:
        entry = self._data["pages"].get(local_path)
        return entry["notion_id"] if entry else None

    def set_page_id(self, local_path: str, page_id: str):
        entry = self._data["pages"].setdefault(local_path, {"notion_id": None, "content_hash": None})
        entry["notion_id"] = page_id

    def remove_page(self, local_path: str):
        self._data["pages"].pop(local_path, None)

    def all_pages(self) -> dict:
        """Returns {local_path: notion_id} for all tracked pages."""
        return {k: v["notion_id"] for k, v in self._data["pages"].items()}

    def get_page_hash(self, local_path: str) -> str | None:
        entry = self._data["pages"].get(local_path)
        return entry["content_hash"] if entry else None

    def set_page_hash(self, local_path: str, content_hash: str):
        entry = self._data["pages"].setdefault(local_path, {"notion_id": None, "content_hash": None})
        entry["content_hash"] = content_hash

    # ------------------------------------------------------------------
    # Notion root page ID  (stored here so the token is the only thing in .env)
    # ------------------------------------------------------------------

    def get_notion_root_page_id(self) -> str | None:
        return self._data.get("notion_root_page_id")

    def set_notion_root_page_id(self, page_id: str):
        self._data["notion_root_page_id"] = page_id

    # ------------------------------------------------------------------
    # Folders  (key: full relative folder path, e.g. "guides/api")
    # ------------------------------------------------------------------

    def get_folder_id(self, folder_path: str) -> str | None:
        return self._data["folders"].get(folder_path)

    def set_folder_id(self, folder_path: str, page_id: str):
        self._data["folders"][folder_path] = page_id

    def remove_folder(self, folder_path: str):
        self._data["folders"].pop(folder_path, None)

    def all_folders(self) -> dict:
        return dict(self._data["folders"])

    # ------------------------------------------------------------------
    # Images  (key: path relative to docs folder, e.g. "Fraud Control/image.png")
    # ------------------------------------------------------------------

    def get_image(self, local_path: str) -> dict | None:
        """Returns {"sha256": ..., "file_upload_id": ...} or None."""
        return self._data["images"].get(local_path)

    def set_image(self, local_path: str, sha256: str, file_upload_id: str):
        self._data["images"][local_path] = {
            "sha256": sha256,
            "file_upload_id": file_upload_id,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        """True when neither pages nor folders have been tracked yet (first run)."""
        return not self._data["pages"] and not self._data["folders"]


# Module-level singleton — import this everywhere instead of instantiating directly.
state = SyncState()

