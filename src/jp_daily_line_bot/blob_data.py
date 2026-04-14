from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import load_settings


def _normalize_base_url(raw: str) -> str:
    return raw.strip().rstrip("/")


def _normalize_prefix(raw: str) -> str:
    return raw.strip().strip("/")


def blob_enabled() -> bool:
    settings = load_settings()
    public_base = _normalize_base_url(settings.blob_public_base_url or "")
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    return bool(public_base or token)


def _blob_full_path(blob_relative_path: str) -> str:
    rel = blob_relative_path.strip().lstrip("/")
    if not rel:
        return ""
    settings = load_settings()
    prefix = _normalize_prefix(settings.blob_data_prefix)
    return f"{prefix}/{rel}" if prefix else rel


def _blob_json_url(blob_relative_path: str) -> str | None:
    settings = load_settings()
    base = _normalize_base_url(settings.blob_public_base_url or "")
    if not base:
        return None

    full_path = _blob_full_path(blob_relative_path)
    if not full_path:
        return None

    encoded_path = quote(full_path, safe="/")
    return f"{base}/{encoded_path}"


def _fetch_blob_json_with_sdk(blob_relative_path: str, timeout_sec: int) -> dict[str, Any] | None:
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        return None

    full_path = _blob_full_path(blob_relative_path)
    if not full_path:
        return None

    try:
        from vercel.blob import BlobClient
    except Exception:
        return None

    client = BlobClient(token=token)
    for access in ("private", "public"):
        try:
            result = client.get(full_path, access=access, timeout=timeout_sec, use_cache=True)
        except Exception:
            continue
        content = getattr(result, "content", None)
        if isinstance(content, bytes):
            try:
                payload = json.loads(content.decode("utf-8"))
            except Exception:
                continue
        elif isinstance(content, str):
            try:
                payload = json.loads(content)
            except Exception:
                continue
        else:
            continue

        if isinstance(payload, dict):
            return payload
    return None


def fetch_blob_json(blob_relative_path: str, timeout_sec: int = 20) -> dict[str, Any] | None:
    url = _blob_json_url(blob_relative_path)
    if url:
        try:
            response = requests.get(url, timeout=timeout_sec)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
        except (requests.RequestException, ValueError):
            pass

    return _fetch_blob_json_with_sdk(blob_relative_path, timeout_sec)


def ensure_blob_json_cached(local_path: Path, blob_relative_path: str, timeout_sec: int = 20) -> bool:
    if local_path.exists():
        return True

    payload = fetch_blob_json(blob_relative_path, timeout_sec=timeout_sec)
    if payload is None:
        return False

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
