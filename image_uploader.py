import os
import hashlib
import requests
from config import HEADERS, BASE_DIR, RED, GREEN, YELLOW, RESET
from sync_state import state

NOTION_FILE_UPLOADS_URL = "https://api.notion.com/v1/file_uploads"

# Headers without Content-Type so requests can set the correct multipart boundary.
_UPLOAD_HEADERS = {
    "Authorization": HEADERS["Authorization"],
    "Notion-Version": HEADERS["Notion-Version"],
}


def _sha256(path: str) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_image_to_notion(image_path: str) -> tuple[str, bool] | None:
    """Upload a local image to Notion via the File Upload API.

    Returns (file_upload_id, from_cache) on success, or None on failure.
    from_cache=True means the image was unchanged and served from the local cache.
    from_cache=False means the image was freshly uploaded.
    Uses a local SHA-256 cache (via sync_state) to skip unchanged files.
    """
    if not os.path.exists(image_path):
        print(f"{YELLOW}Image not found: {image_path}{RESET}")
        return None

    # Use a path relative to BASE_DIR as the state key so the cache is portable.
    state_key = os.path.relpath(image_path, BASE_DIR)

    current_hash = _sha256(image_path)
    cached = state.get_image(state_key)

    if cached and cached.get("sha256") == current_hash:
        print(f"{YELLOW}Image unchanged, reusing upload: {os.path.basename(image_path)}{RESET}")
        return cached["file_upload_id"], True

    # Step 1: Create a File Upload object to get an upload URL + ID.
    create_response = requests.post(
        NOTION_FILE_UPLOADS_URL,
        headers=HEADERS,
        json={"mode": "single_part"},
    )
    if create_response.status_code != 200:
        print(f"{RED}Failed to create file upload: {create_response.status_code} - {create_response.text}{RESET}")
        return None

    upload_data = create_response.json()
    upload_id = upload_data["id"]
    upload_url = upload_data["upload_url"]

    # Determine MIME type so Notion classifies the file correctly.
    ext = os.path.splitext(image_path)[1].lower()
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }
    mime_type = mime_types.get(ext, "image/png")

    # Step 2: POST the file bytes as multipart/form-data.
    with open(image_path, "rb") as f:
        send_response = requests.post(
            upload_url,
            headers=_UPLOAD_HEADERS,
            files={"file": (os.path.basename(image_path), f, mime_type)},
        )

    if send_response.status_code not in (200, 201):
        print(f"{RED}Failed to upload image bytes: {send_response.status_code} - {send_response.text}{RESET}")
        return None

    print(f"{GREEN}Uploaded {os.path.basename(image_path)} to Notion (id: {upload_id}){RESET}")
    state.set_image(state_key, current_hash, upload_id)
    state.save()

    return upload_id, False
