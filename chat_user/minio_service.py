"""MinIO helpers for attachment upload and delete."""

import mimetypes
import os
import uuid
from io import BytesIO
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error

from config import MINIO_CONFIG


def _normalize_endpoint(endpoint: str) -> str:
    """Normalize MinIO endpoint to host:port format accepted by Minio SDK."""
    if not endpoint:
        raise ValueError("MinIO endpoint cannot be empty")

    value = endpoint.strip()
    if "://" not in value:
        value = f"http://{value}"

    parsed = urlparse(value)
    if not parsed.netloc:
        raise ValueError("Invalid MinIO endpoint")

    return parsed.netloc


def _get_client() -> Minio:
    endpoint = _normalize_endpoint(MINIO_CONFIG["endpoint"])
    return Minio(
        endpoint,
        access_key=MINIO_CONFIG["access_key"],
        secret_key=MINIO_CONFIG["secret_key"],
        secure=MINIO_CONFIG["secure"],
    )


def _ensure_bucket(client: Minio) -> None:
    bucket_name = MINIO_CONFIG["bucket_name"]
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)


def upload_attachment(filename: str, content: bytes, content_type: str | None = None) -> str:
    """Upload one attachment to MinIO and return its accessible path-style URL."""
    if not filename:
        raise ValueError("filename cannot be empty")

    client = _get_client()
    _ensure_bucket(client)

    ext = os.path.splitext(filename)[1]
    object_name = f"attachments/{uuid.uuid4().hex}{ext}"
    if not content_type:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    try:
        client.put_object(
            MINIO_CONFIG["bucket_name"],
            object_name,
            BytesIO(content),
            length=len(content),
            content_type=content_type,
        )
    except S3Error as exc:
        raise RuntimeError(f"Failed to upload attachment to MinIO: {exc}") from exc

    scheme = "https" if MINIO_CONFIG["secure"] else "http"
    endpoint = _normalize_endpoint(MINIO_CONFIG["endpoint"])
    return f"{scheme}://{endpoint}/{MINIO_CONFIG['bucket_name']}/{object_name}"


def delete_attachment(attachment_url: str) -> bool:
    """Delete one attachment from MinIO by URL."""
    if not attachment_url:
        raise ValueError("attachment_url cannot be empty")

    client = _get_client()
    parsed = urlparse(attachment_url)
    path = parsed.path.lstrip("/")
    bucket_name = MINIO_CONFIG["bucket_name"]
    bucket_prefix = f"{bucket_name}/"
    if not path.startswith(bucket_prefix):
        raise ValueError("attachment_url does not belong to the configured MinIO bucket")

    object_name = path[len(bucket_prefix) :]
    try:
        client.remove_object(bucket_name, object_name)
        return True
    except S3Error as exc:
        raise RuntimeError(f"Failed to delete attachment from MinIO: {exc}") from exc
