"""
S3 Trigger Handler (Lambda)
=============================
Triggered by EventBridge when an Excel file lands in the uploads/ prefix.
Looks up the session from the S3 key, then starts the Step Functions pipeline.
"""

import json
import os
import logging
from datetime import datetime

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SFN_ARN = os.environ.get("SFN_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def lambda_handler(event, context):
    """Handle S3 EventBridge notification."""
    logger.info(f"[S3Trigger] Event: {json.dumps(event)[:500]}")

    # EventBridge event structure
    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name", "")
    s3_key = detail.get("object", {}).get("key", "")

    if not s3_key:
        # Try S3 notification format (if using S3 events directly)
        for record in event.get("Records", []):
            s3_info = record.get("s3", {})
            bucket = s3_info.get("bucket", {}).get("name", "")
            s3_key = s3_info.get("object", {}).get("key", "")
            break

    if not s3_key:
        logger.warning("[S3Trigger] No S3 key found in event")
        return {"status": "skipped", "reason": "no_key"}

    logger.info(f"[S3Trigger] File uploaded: s3://{bucket}/{s3_key}")

    # Only process Excel files in uploads/
    if not s3_key.startswith("uploads/"):
        logger.info(f"[S3Trigger] Ignoring non-upload key: {s3_key}")
        return {"status": "skipped", "reason": "not_upload"}

    if not s3_key.endswith((".xlsx", ".xls")):
        logger.info(f"[S3Trigger] Ignoring non-Excel file: {s3_key}")
        return {"status": "skipped", "reason": "not_excel"}

    # Find session from S3 key
    import session_store

    session = session_store.find_session_by_upload_key(s3_key)
    if not session:
        # Try extracting session_id from key: uploads/{session_id}/file.xlsx
        parts = s3_key.split("/")
        if len(parts) >= 3 and parts[1].startswith("sess_"):
            session = session_store.get_session(parts[1])

    if not session:
        logger.warning(f"[S3Trigger] No session found for upload key: {s3_key}")
        return {"status": "skipped", "reason": "no_session"}

    session_id = session["session_id"]
    config = session.get("config", {})

    # Ensure excel_key is set
    if config.get("excel_key") != s3_key:
        session_store.update_config(session_id, {"excel_key": s3_key})
        config["excel_key"] = s3_key

    # Generate run_id
    run_id = f"pipe_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    # Start Step Functions
    sfn = boto3.client("stepfunctions", region_name=REGION)
    sfn_input = {
        "session_id": session_id,
        "run_id": run_id,
        "config": config,
    }

    if not SFN_ARN:
        logger.error("[S3Trigger] SFN_ARN not configured")
        return {"status": "error", "reason": "no_sfn_arn"}

    execution = sfn.start_execution(
        stateMachineArn=SFN_ARN,
        name=f"{session_id}-{run_id}".replace(":", "-"),
        input=json.dumps(sfn_input, default=str),
    )

    # Update session
    session_store.set_status(
        session_id, "running",
        step="generate",
        progress="Pipeline auto-started after Excel upload. Generating test cases...",
        run_id=run_id,
    )

    logger.info(f"[S3Trigger] Started pipeline for session={session_id} run={run_id}")

    return {
        "status": "started",
        "session_id": session_id,
        "run_id": run_id,
        "execution_arn": execution["executionArn"],
    }
