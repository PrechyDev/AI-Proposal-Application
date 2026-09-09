import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Reference files and generated PDFs both go to Supabase Storage (spec
# section 9) rather than local disk, which doesn't survive redeploys on
# most PaaS platforms. Only reference files use this bucket so far (step 8);
# a PDF bucket/path is a later step's concern.
REFERENCE_FILES_BUCKET = "reference-files"

_TIMEOUT = 30.0


class StorageError(Exception):
    """Raised when a Supabase Storage request fails."""


def _base_url() -> str:
    return get_settings().supabase_project_url.rstrip("/") + "/storage/v1"


def _auth_headers() -> dict:
    key = get_settings().supabase_service_role_key
    # Both headers are required by Supabase Storage's REST API: apikey
    # identifies the project/key, Authorization is what's actually checked
    # against the bucket's access policy.
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def ensure_bucket_exists() -> None:
    """Create the reference-files bucket if it doesn't exist yet.

    Called once at app startup. Deliberately non-fatal on failure (unlike
    the DB startup check) - a storage outage shouldn't take down routes
    that don't touch reference files.
    """
    try:
        resp = httpx.post(
            f"{_base_url()}/bucket",
            headers=_auth_headers(),
            json={"id": REFERENCE_FILES_BUCKET, "name": REFERENCE_FILES_BUCKET, "public": False},
            timeout=_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            logger.info("Created Supabase Storage bucket %r.", REFERENCE_FILES_BUCKET)
        elif "already exists" in resp.text.lower() or "Duplicate" in resp.text:
            logger.info("Supabase Storage bucket %r already exists.", REFERENCE_FILES_BUCKET)
        else:
            logger.error(
                "Unexpected response creating Supabase Storage bucket %r: %s %s",
                REFERENCE_FILES_BUCKET, resp.status_code, resp.text,
            )
    except httpx.HTTPError:
        logger.exception("Failed to reach Supabase Storage while ensuring bucket exists.")


def upload_file(storage_path: str, content: bytes, content_type: str) -> None:
    url = f"{_base_url()}/object/{REFERENCE_FILES_BUCKET}/{storage_path}"
    try:
        resp = httpx.post(
            url,
            headers={**_auth_headers(), "Content-Type": content_type},
            content=content,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Could not reach Supabase Storage: {exc}") from None
    if resp.status_code not in (200, 201):
        logger.error("Supabase Storage upload failed (%s): %s", resp.status_code, resp.text)
        raise StorageError(f"Upload failed with status {resp.status_code}")


def delete_file(storage_path: str) -> None:
    """Permanently removes an object from storage - used only for a real,
    final delete (the trash's "Delete Permanently"/"Empty Trash"/auto-purge
    paths), never for the softer "move to trash" step, which only touches
    the DB row's `deleted_at`.
    """
    url = f"{_base_url()}/object/{REFERENCE_FILES_BUCKET}/{storage_path}"
    try:
        resp = httpx.delete(url, headers=_auth_headers(), timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise StorageError(f"Could not reach Supabase Storage: {exc}") from None
    if resp.status_code not in (200, 204):
        logger.error("Supabase Storage delete failed (%s): %s", resp.status_code, resp.text)
        raise StorageError(f"Delete failed with status {resp.status_code}")


def create_signed_url(storage_path: str, expires_in: int = 60) -> str:
    """Returns a short-lived URL the browser can fetch directly from
    Supabase Storage, bypassing our own server as a byte-proxy. Preview
    previously round-tripped the full file through this app on every
    open (browser -> us -> Supabase -> us -> browser); a signed URL turns
    that into a single hop straight to Supabase, which is the real fix
    for how slow a preview felt to open, not just how it looks while
    loading. The bucket is private, so an unsigned/public URL would 403 -
    this is Supabase's supported way to grant temporary, unauthenticated
    read access to one object without making the whole bucket public.
    """
    url = f"{_base_url()}/object/sign/{REFERENCE_FILES_BUCKET}/{storage_path}"
    try:
        resp = httpx.post(
            url, headers=_auth_headers(), json={"expiresIn": expires_in}, timeout=_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise StorageError(f"Could not reach Supabase Storage: {exc}") from None
    if resp.status_code != 200:
        logger.error("Supabase Storage sign failed (%s): %s", resp.status_code, resp.text)
        raise StorageError(f"Sign failed with status {resp.status_code}")
    signed_path = resp.json()["signedURL"]
    return f"{_base_url()}{signed_path}"


def download_file(storage_path: str) -> bytes:
    url = f"{_base_url()}/object/{REFERENCE_FILES_BUCKET}/{storage_path}"
    try:
        resp = httpx.get(url, headers=_auth_headers(), timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise StorageError(f"Could not reach Supabase Storage: {exc}") from None
    if resp.status_code != 200:
        logger.error("Supabase Storage download failed (%s): %s", resp.status_code, resp.text)
        raise StorageError(f"Download failed with status {resp.status_code}")
    return resp.content
