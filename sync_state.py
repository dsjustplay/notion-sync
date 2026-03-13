import json
import os

STATE_FILE = "sync_state.json"


class SyncState:
    """Persistent local state mapping local paths to Notion IDs and image upload cache."""

    def __init__(self):
        self._data = {"pages": {}, "folders": {}, "images": {}}

    def load(self):
        """Load state from disk. Safe to call even if the file does not exist yet."""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        return self

    def save(self):
        """Persist current state to disk."""
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    # ------------------------------------------------------------------
    # Pages  (key: relative local path, e.g. "docs/guides/intro.md")
    # ------------------------------------------------------------------

    def get_page_id(self, local_path: str) -> str | None:
        return self._data["pages"].get(local_path)

    def set_page_id(self, local_path: str, page_id: str):
        self._data["pages"][local_path] = page_id

    def remove_page(self, local_path: str):
        self._data["pages"].pop(local_path, None)

    def all_pages(self) -> dict:
        return dict(self._data["pages"])

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
    # Images  (key: absolute local image path)
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

