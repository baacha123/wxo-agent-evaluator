"""
S3 Storage Helper
==================
Abstracts file I/O for the eval pipeline.
Uses S3 in Lambda, falls back to local filesystem for testing.
"""

import json
import os
import io
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

S3_BUCKET = os.environ.get("S3_BUCKET", "wxo-eval-pipeline")
USE_LOCAL = os.environ.get("STORAGE_LOCAL", "").lower() in ("1", "true", "yes")

_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3")
    return _s3_client


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def read_json(key: str, default: Any = None) -> Any:
    """Read a JSON file from S3 (or local)."""
    if USE_LOCAL:
        try:
            with open(key, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    try:
        resp = _get_s3().get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"read_json({key}): {e}")
        return default


def write_json(key: str, data: Any) -> str:
    """Write a JSON file to S3 (or local). Returns the key."""
    body = json.dumps(data, indent=2, ensure_ascii=False)
    if USE_LOCAL:
        os.makedirs(os.path.dirname(key) or ".", exist_ok=True)
        with open(key, "w", encoding="utf-8") as f:
            f.write(body)
        return key
    _get_s3().put_object(Bucket=S3_BUCKET, Key=key, Body=body.encode("utf-8"), ContentType="application/json")
    return key


def write_text(key: str, text: str, content_type: str = "text/plain") -> str:
    """Write text/html to S3 (or local)."""
    if USE_LOCAL:
        os.makedirs(os.path.dirname(key) or ".", exist_ok=True)
        with open(key, "w", encoding="utf-8") as f:
            f.write(text)
        return key
    _get_s3().put_object(Bucket=S3_BUCKET, Key=key, Body=text.encode("utf-8"), ContentType=content_type)
    return key


# ---------------------------------------------------------------------------
# List / Exists
# ---------------------------------------------------------------------------

def list_keys(prefix: str, suffix: str = "") -> List[str]:
    """List S3 keys under a prefix, optionally filtered by suffix."""
    if USE_LOCAL:
        import glob
        pattern = os.path.join(prefix, "**") if os.path.isdir(prefix) else prefix + "*"
        paths = sorted(glob.glob(pattern, recursive=True))
        if suffix:
            paths = [p for p in paths if p.endswith(suffix)]
        return paths
    keys = []
    paginator = _get_s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if not suffix or k.endswith(suffix):
                keys.append(k)
    return sorted(keys)


def key_exists(key: str) -> bool:
    if USE_LOCAL:
        return os.path.exists(key)
    try:
        _get_s3().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Binary / Excel
# ---------------------------------------------------------------------------

def read_bytes(key: str) -> Optional[bytes]:
    """Read raw bytes from S3 (or local)."""
    if USE_LOCAL:
        try:
            with open(key, "rb") as f:
                return f.read()
        except Exception:
            return None
    try:
        resp = _get_s3().get_object(Bucket=S3_BUCKET, Key=key)
        return resp["Body"].read()
    except Exception as e:
        logger.warning(f"read_bytes({key}): {e}")
        return None


def read_excel_df(key: str):
    """Read an Excel file from S3 into a pandas DataFrame."""
    import pandas as pd
    raw = read_bytes(key)
    if raw is None:
        raise FileNotFoundError(f"Excel not found: {key}")
    return pd.read_excel(io.BytesIO(raw))


# ---------------------------------------------------------------------------
# Run Tracking
# ---------------------------------------------------------------------------

def save_run_status(run_id: str, status: dict) -> str:
    return write_json(f"runs/{run_id}.json", status)


def get_run_status(run_id: str) -> Optional[dict]:
    return read_json(f"runs/{run_id}.json")


def find_latest_run() -> Optional[str]:
    """Find the most recent run_id by listing runs/ prefix."""
    keys = list_keys("runs/", suffix=".json")
    if not keys:
        return None
    latest = keys[-1]  # sorted, so last is newest
    # runs/eval_20260226_1430.json -> eval_20260226_1430
    return latest.split("/")[-1].replace(".json", "")
