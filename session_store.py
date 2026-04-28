"""
Session Store (DynamoDB)
=========================
CRUD operations for eval sessions stored in DynamoDB.
Sessions track the full lifecycle: config → upload → running → completed.
"""

import os
import time
import uuid
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "wxo-eval-sessions")
TTL_HOURS = 24

_table = None


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        _table = dynamodb.Table(TABLE_NAME)
    return _table


def create_session(agent_name: str = None) -> Dict[str, Any]:
    """Create a new eval session. Returns the session record."""
    now = datetime.utcnow()
    session_id = f"sess_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    item = {
        "session_id": session_id,
        "created_at": now.isoformat() + "Z",
        "expires_at": int(time.time()) + (TTL_HOURS * 3600),
        "status": "configuring",
        "config": {
            "agent_name": agent_name or "your_target_agent",
            "tool_name": None,
            "excel_key": None,
            "limit": None,
            "skip_judge": False,
            "skip_rca": False,
            "model_id": "meta-llama/llama-3-3-70b-instruct",
        },
        "run_id": None,
        "step": None,
        "progress": None,
        "results_key": None,
        "error": None,
    }

    _get_table().put_item(Item=item)
    logger.info(f"Created session: {session_id}")
    return item


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Get a session by ID."""
    resp = _get_table().get_item(Key={"session_id": session_id})
    return resp.get("Item")


def update_session(session_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update specific fields on a session.

    Args:
        session_id: Session to update
        updates: Dict of field names to new values

    Returns:
        Updated session record
    """
    expr_names = {}
    expr_values = {}
    set_parts = []

    for key, value in updates.items():
        safe_key = f"#{key.replace('.', '_')}"
        val_key = f":{key.replace('.', '_')}"
        expr_names[safe_key] = key
        expr_values[val_key] = value
        set_parts.append(f"{safe_key} = {val_key}")

    resp = _get_table().update_item(
        Key={"session_id": session_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes", {})


def update_config(session_id: str, config_updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge updates into the session's config map."""
    session = get_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    config = session.get("config", {})
    config.update(config_updates)

    return update_session(session_id, {"config": config, "status": "configuring"})


def set_status(session_id: str, status: str, step: str = None,
               progress: str = None, error: str = None, **extra) -> Dict[str, Any]:
    """Update session status + progress fields."""
    updates = {"status": status}
    if step is not None:
        updates["step"] = step
    if progress is not None:
        updates["progress"] = progress
    if error is not None:
        updates["error"] = error
    updates.update(extra)
    return update_session(session_id, updates)


def find_latest_session() -> Optional[Dict[str, Any]]:
    """Find the most recently created session (scan — fine for low volume)."""
    resp = _get_table().scan(Limit=50)
    items = resp.get("Items", [])
    if not items:
        return None
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items[0]


def find_session_by_upload_key(upload_key: str) -> Optional[Dict[str, Any]]:
    """Find session whose config.excel_key matches the upload path."""
    resp = _get_table().scan(
        FilterExpression="config.excel_key = :key",
        ExpressionAttributeValues={":key": upload_key},
        Limit=10,
    )
    items = resp.get("Items", [])
    if not items:
        # Fallback: check if key contains session ID
        for part in upload_key.split("/"):
            if part.startswith("sess_"):
                session = get_session(part)
                if session:
                    return session
        return None
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items[0]
