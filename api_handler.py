"""
API Handler (Lambda)
=====================
API Gateway Lambda — routes WxO tool calls to session management,
uploads, pipeline triggers, status checks, results, and RCA.

Routes:
  POST /eval/session/start   → create session
  POST /eval/session/config  → update session config
  POST /eval/upload          → presigned URL for Excel upload
  POST /eval/start           → start Step Functions pipeline
  POST /eval/status          → check session/pipeline status
  POST /eval/results         → get analysis results
  POST /eval/explain         → RCA for a specific test
  POST /eval/redteam/list    → list attack types
  POST /eval/redteam/start   → start red team campaign
  POST /eval/redteam/results → get red team results
"""

import json
import os
import logging
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "wxo-eval-pipeline")
SFN_ARN = os.environ.get("SFN_ARN", "")
REDTEAM_SFN_ARN = os.environ.get("REDTEAM_SFN_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def lambda_handler(event, context):
    """Main Lambda entry point for API Gateway."""
    path = event.get("path", "")
    method = event.get("httpMethod", "")

    if method == "OPTIONS":
        return _response(200, {"message": "ok"})

    try:
        raw_body = event.get("body")
        if raw_body and isinstance(raw_body, str):
            body = json.loads(raw_body)
        elif isinstance(raw_body, dict):
            body = raw_body
        else:
            body = {}
    except (json.JSONDecodeError, TypeError):
        body = {}

    # Sanitize session_id — LLM sometimes hallucinates invalid values
    if body.get("session_id") and not body["session_id"].startswith("sess_"):
        logger.warning(f"[API] Invalid session_id from agent: {body['session_id']}, removing")
        body.pop("session_id", None)

    logger.info(f"[API] {method} {path} body_keys={list((body or {}).keys())}")

    # GET /eval/upload-page serves HTML upload form
    if path == "/eval/upload-page" and method == "GET":
        return handle_upload_page(event.get("queryStringParameters") or {})

    routes = {
        "/eval/session/start": handle_session_start,
        "/eval/session/config": handle_session_config,
        "/eval/upload": handle_upload,
        "/eval/start": handle_start,
        "/eval/status": handle_status,
        "/eval/results": handle_results,
        "/eval/explain": handle_explain,
        "/eval/reanalyze": handle_reanalyze,
        "/eval/redteam": handle_redteam,
        "/eval/redteam/list": handle_redteam_list,
        "/eval/redteam/start": handle_redteam_start,
        "/eval/redteam/results": handle_redteam_results,
    }

    handler = routes.get(path)
    if not handler:
        return _response(404, {"error": f"Unknown route: {path}"})

    try:
        result = handler(body)
        return _response(200, result)
    except ValueError as e:
        return _response(400, {"error": str(e)})
    except Exception as e:
        logger.error(f"[API] Error in {path}: {e}", exc_info=True)
        return _response(500, {"error": str(e)})


# ---------------------------------------------------------------------------
# Route Handlers
# ---------------------------------------------------------------------------

def handle_session_start(body: dict) -> dict:
    """Create a new eval session."""
    import session_store

    session = session_store.create_session()

    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "config": session["config"],
        "message": f"Session created: {session['session_id']}. "
                   "Which agent would you like to evaluate?",
    }


def handle_session_config(body: dict) -> dict:
    """Update session configuration (agent_name, limit, skip_rca, etc.)."""
    import session_store

    session_id = body.get("session_id")
    if not session_id:
        latest = session_store.find_latest_session()
        if not latest:
            raise ValueError("No active session. Call eval_session_start first.")
        session_id = latest["session_id"]

    config_updates = {}
    for key in ("agent_name", "tool_name", "limit", "skip_judge", "skip_rca", "model_id"):
        if key in body:
            config_updates[key] = body[key]

    if not config_updates:
        raise ValueError("No config fields provided. Set agent_name, tool_name, limit, skip_judge, skip_rca, or model_id.")

    session = session_store.update_config(session_id, config_updates)

    return {
        "session_id": session_id,
        "config": session.get("config", {}),
        "status": session.get("status"),
        "message": f"Config updated for session {session_id}.",
    }


def handle_upload(body: dict) -> dict:
    """Generate a presigned upload URL and a browser-friendly upload page link."""
    import boto3
    import session_store

    session_id = body.get("session_id")
    if not session_id:
        latest = session_store.find_latest_session()
        if not latest:
            raise ValueError("No active session. Call eval_session_start first.")
        session_id = latest["session_id"]

    file_name = body.get("file_name", "questions.xlsx")
    # Sanitize
    safe_name = "".join(c for c in file_name if c.isalnum() or c in "._-").strip() or "questions.xlsx"
    s3_key = f"uploads/{session_id}/{safe_name}"

    s3 = boto3.client("s3", region_name=REGION)
    presigned_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key, "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        ExpiresIn=900,
    )

    # Save excel_key to session
    session_store.update_config(session_id, {"excel_key": s3_key})
    session_store.set_status(session_id, "uploading")

    # Build browser-friendly upload page link
    # Detect API base URL from Lambda env or use default
    api_base = os.environ.get("API_BASE_URL", "")
    if not api_base:
        api_base = f"https://{os.environ.get('API_ID', 'YOUR_API_GATEWAY_ID')}.execute-api.{REGION}.amazonaws.com/{os.environ.get('STAGE', 'prod')}"
    upload_page_url = f"{api_base}/eval/upload-page?session_id={session_id}"

    return {
        "session_id": session_id,
        "upload_url": presigned_url,
        "upload_page": upload_page_url,
        "s3_key": s3_key,
        "expires_in": 900,
        "message": f"Click here to upload your Excel file: {upload_page_url}",
    }


def handle_upload_page(params: dict) -> dict:
    """Serve an HTML file upload page. GET /eval/upload-page?session_id=xxx"""
    import boto3
    import session_store

    session_id = params.get("session_id", "")
    if not session_id:
        latest = session_store.find_latest_session()
        if not latest:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "text/html"},
                "body": "<h2>No active session found.</h2><p>Start a session first via the eval agent.</p>",
            }
        session_id = latest["session_id"]

    session = session_store.get_session(session_id)
    if not session:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "text/html"},
            "body": f"<h2>Session not found: {session_id}</h2>",
        }

    api_base = os.environ.get("API_BASE_URL", "")
    if not api_base:
        api_base = f"https://{os.environ.get('API_ID', 'YOUR_API_GATEWAY_ID')}.execute-api.{REGION}.amazonaws.com/{os.environ.get('STAGE', 'prod')}"

    # Generate two presigned URLs: one for Excel, one for JSON
    s3 = boto3.client("s3", region_name=REGION)

    s3_key = f"uploads/{session_id}/questions.xlsx"
    presigned_url_excel = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": S3_BUCKET,
            "Key": s3_key,
            "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        ExpiresIn=900,
    )

    s3_key_json = f"uploads/{session_id}/redteam_data.json"
    presigned_url_json = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": S3_BUCKET,
            "Key": s3_key_json,
            "ContentType": "application/json",
        },
        ExpiresIn=900,
    )
    presigned_url = presigned_url_excel  # default for backward compat

    # Update session
    session_store.update_config(session_id, {"excel_key": s3_key})
    session_store.set_status(session_id, "uploading")

    agent_name = session.get("config", {}).get("agent_name", "unknown")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Eval Upload — {session_id}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; min-height: 100vh;
         display: flex; align-items: center; justify-content: center; }}
  .card {{ background: #1e293b; border-radius: 16px; padding: 40px;
           max-width: 520px; width: 90%; box-shadow: 0 25px 50px rgba(0,0,0,0.4); }}
  h1 {{ font-size: 24px; margin-bottom: 8px; color: #f8fafc; }}
  .meta {{ color: #94a3b8; font-size: 14px; margin-bottom: 24px; }}
  .meta span {{ color: #38bdf8; }}
  .drop-zone {{ border: 2px dashed #475569; border-radius: 12px; padding: 40px 20px;
                text-align: center; cursor: pointer; transition: all 0.2s; }}
  .drop-zone:hover, .drop-zone.drag-over {{ border-color: #38bdf8; background: rgba(56,189,248,0.05); }}
  .drop-zone p {{ font-size: 16px; margin-bottom: 8px; }}
  .drop-zone small {{ color: #64748b; }}
  .file-name {{ color: #38bdf8; font-weight: 600; margin-top: 12px; }}
  input[type=file] {{ display: none; }}
  .btn {{ display: block; width: 100%; padding: 14px; margin-top: 24px; border: none;
          border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer;
          background: #2563eb; color: white; transition: background 0.2s; }}
  .btn:hover {{ background: #1d4ed8; }}
  .btn:disabled {{ background: #334155; color: #64748b; cursor: not-allowed; }}
  .status {{ margin-top: 16px; padding: 12px; border-radius: 8px; font-size: 14px; display: none; }}
  .status.success {{ display: block; background: rgba(34,197,94,0.1); color: #4ade80; border: 1px solid #166534; }}
  .status.error {{ display: block; background: rgba(239,68,68,0.1); color: #f87171; border: 1px solid #7f1d1d; }}
  .status.uploading {{ display: block; background: rgba(56,189,248,0.1); color: #38bdf8; border: 1px solid #0c4a6e; }}
  .progress {{ width: 100%; height: 6px; background: #334155; border-radius: 3px; margin-top: 8px; overflow: hidden; }}
  .progress-bar {{ height: 100%; background: #2563eb; border-radius: 3px; transition: width 0.3s; width: 0%; }}
</style>
</head>
<body>
<div class="card">
  <h1>Upload Test Data</h1>
  <p class="meta">Session: <span>{session_id}</span><br>Agent: <span>{agent_name}</span></p>

  <div class="drop-zone" id="dropZone">
    <p>Drag & drop your file here</p>
    <small>or click to browse (.xlsx or .json)</small>
    <div class="file-name" id="fileName"></div>
  </div>
  <input type="file" id="fileInput" accept=".xlsx,.xls,.json">

  <button class="btn" id="uploadBtn" disabled>Upload & Start Pipeline</button>

  <div class="status" id="status"></div>
  <div class="progress" style="display:none" id="progressWrap">
    <div class="progress-bar" id="progressBar"></div>
  </div>
</div>

<script>
const PRESIGNED_URL_EXCEL = {json.dumps(presigned_url_excel)};
const PRESIGNED_URL_JSON = {json.dumps(presigned_url_json)};
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const uploadBtn = document.getElementById('uploadBtn');
const statusEl = document.getElementById('status');
const fileNameEl = document.getElementById('fileName');
const progressWrap = document.getElementById('progressWrap');
const progressBar = document.getElementById('progressBar');
let selectedFile = null;
let fileType = 'excel';

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => {{ e.preventDefault(); dropZone.classList.add('drag-over'); }});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {{
  e.preventDefault(); dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
}});
fileInput.addEventListener('change', () => {{ if (fileInput.files.length) selectFile(fileInput.files[0]); }});

function selectFile(file) {{
  if (file.name.match(/\\.json$/i)) {{
    fileType = 'json';
  }} else if (file.name.match(/\\.xlsx?$/i)) {{
    fileType = 'excel';
  }} else {{
    showStatus('Please select an Excel (.xlsx) or JSON (.json) file', 'error'); return;
  }}
  selectedFile = file;
  fileNameEl.textContent = file.name + ' (' + (file.size / 1024).toFixed(1) + ' KB)';
  uploadBtn.disabled = false;
  statusEl.style.display = 'none';
}}

uploadBtn.addEventListener('click', async () => {{
  if (!selectedFile) return;
  uploadBtn.disabled = true;
  uploadBtn.textContent = 'Uploading...';
  showStatus('Uploading file to S3...', 'uploading');
  progressWrap.style.display = 'block';

  try {{
    const xhr = new XMLHttpRequest();
    xhr.upload.addEventListener('progress', e => {{
      if (e.lengthComputable) progressBar.style.width = (e.loaded / e.total * 100) + '%';
    }});
    await new Promise((resolve, reject) => {{
      const uploadUrl = fileType === 'json' ? PRESIGNED_URL_JSON : PRESIGNED_URL_EXCEL;
      const contentType = fileType === 'json' ? 'application/json' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
      xhr.open('PUT', uploadUrl);
      xhr.setRequestHeader('Content-Type', contentType);
      xhr.onload = () => {{
        if (xhr.status === 200) resolve();
        else reject(new Error('HTTP ' + xhr.status + ': ' + xhr.responseText.substring(0, 200)));
      }};
      xhr.onerror = () => reject(new Error('Network error — check browser console for CORS details'));
      xhr.send(selectedFile);
    }});
    progressBar.style.width = '100%';
    uploadBtn.textContent = 'Uploaded!';
    showStatus('Upload successful! Pipeline is starting...', 'success');
    // Show "go back to chat" prompt
    setTimeout(() => {{
      const goBack = document.createElement('div');
      goBack.className = 'status success';
      goBack.innerHTML = '<strong>Go back to the chat and type "done"</strong> — the agent will show you the pipeline progress and results.';
      goBack.style.marginTop = '12px';
      document.querySelector('.card').appendChild(goBack);
    }}, 2000);
    // Also poll status on this page
    pollStatus();
  }} catch (err) {{
    showStatus('Upload failed: ' + err.message, 'error');
    uploadBtn.disabled = false;
    uploadBtn.textContent = 'Retry Upload';
  }}
}});

function showStatus(msg, type) {{
  statusEl.textContent = msg;
  statusEl.className = 'status ' + type;
}}

const API_BASE = {json.dumps(api_base)};
const SESSION_ID = {json.dumps(session_id)};

async function pollStatus() {{
  for (let i = 0; i < 40; i++) {{
    await new Promise(r => setTimeout(r, 15000));
    try {{
      const resp = await fetch(API_BASE + '/eval/status', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{session_id: SESSION_ID}})
      }});
      const data = await resp.json();
      const step = data.step || '';
      const status = data.status || '';
      const progress = data.progress || '';
      if (status === 'completed') {{
        showStatus('Pipeline complete! ' + progress + ' — Go back to the chat to see results.', 'success');
        return;
      }} else if (status === 'failed') {{
        showStatus('Pipeline failed: ' + (data.error || 'Unknown error'), 'error');
        return;
      }} else {{
        showStatus('Pipeline running — ' + progress, 'uploading');
      }}
    }} catch (e) {{
      // ignore poll errors
    }}
  }}
}}
</script>
</body>
</html>"""

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/html",
            "Access-Control-Allow-Origin": "*",
        },
        "body": html,
    }


def handle_start(body: dict) -> dict:
    """Manually start the Step Functions pipeline."""
    import boto3
    import session_store

    session_id = body.get("session_id")
    if not session_id:
        latest = session_store.find_latest_session()
        if not latest:
            raise ValueError("No active session. Call eval_session_start first.")
        session_id = latest["session_id"]

    session = session_store.get_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    config = session.get("config", {})
    if not config.get("excel_key"):
        raise ValueError("No Excel file uploaded yet. Use eval_upload first.")

    # Generate run_id
    run_id = f"pipe_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    # Start Step Functions execution
    sfn = boto3.client("stepfunctions", region_name=REGION)
    sfn_input = {
        "session_id": session_id,
        "run_id": run_id,
        "config": config,
    }

    sfn_arn = SFN_ARN
    if not sfn_arn:
        raise ValueError("Step Functions ARN not configured (SFN_ARN env var)")

    execution = sfn.start_execution(
        stateMachineArn=sfn_arn,
        name=f"{session_id}-{run_id}",
        input=json.dumps(sfn_input, default=str),
    )

    # Update session
    session_store.set_status(
        session_id, "running",
        step="generate",
        progress="Pipeline started — generating test cases...",
        run_id=run_id,
    )

    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": "running",
        "execution_arn": execution["executionArn"],
        "message": f"Pipeline started (run_id: {run_id}). I'll track progress — ask me for status anytime.",
    }


def handle_status(body: dict) -> dict:
    """Check pipeline status from DynamoDB session.

    If wait=true, polls DynamoDB for up to 25 seconds waiting for
    a terminal state (completed/failed). This lets the agent chain
    calls without the user having to ask repeatedly.

    If status is 'completed', results are included inline so the
    agent can display them immediately without a separate call.
    """
    import time
    import session_store

    session_id = body.get("session_id")
    if not session_id:
        latest = session_store.find_latest_session()
        if not latest:
            return {"status": "no_sessions", "message": "No sessions found. Start one with eval_session_start."}
        session_id = latest["session_id"]

    wait = str(body.get("wait", "false")).lower() in ("true", "1", "yes")
    max_wait = 25  # seconds (under API Gateway 29s timeout)

    session = session_store.get_session(session_id)
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    # If wait=true, poll until terminal state or timeout
    if wait and session.get("status") in ("running", "uploading"):
        start_time = time.time()
        while time.time() - start_time < max_wait:
            time.sleep(5)
            session = session_store.get_session(session_id)
            if session.get("status") in ("completed", "failed"):
                break

    status_val = session.get("status", "unknown")

    error_val = session.get("error")

    result = {
        "session_id": session_id,
        "status": status_val,
        "step": session.get("step"),
        "progress": session.get("progress"),
        "run_id": session.get("run_id"),
        "config": session.get("config", {}),
        "message": _status_message(session),
    }
    # Only include error if non-empty (avoids stale errors from previous runs)
    if error_val:
        result["error"] = error_val

    # When still running after wait, tell the user to check back
    if status_val == "running" and wait:
        result["message"] = (
            f"{session.get('progress', 'Pipeline is running...')} "
            "Tell the user to type status again in a minute to check progress."
        )

    # If completed, include results inline
    if session.get("status") == "completed" and session.get("run_id"):
        try:
            import storage
            run_id = session["run_id"]
            report = storage.read_json(f"analyze/{run_id}/report.json")
            if report:
                summary = report.get("summary", {})
                cases = report.get("cases", [])
                case_summaries = []
                failures = []
                for c in cases:
                    test_name = c.get("test_name", "?")
                    journey = "PASS" if c.get("journey_success") else "FAIL"
                    tools = f"{c.get('correct_tool_count', 0)}/{c.get('expected_tool_count', 0)}"
                    judge = c.get("llm_judge", {})
                    judge_verdict = judge.get("verdict", "N/A")
                    judge_score = judge.get("score", 0)
                    judge_text = f"{judge_verdict} ({judge_score:.1f})"
                    rca_count = len(c.get("rca", []))
                    case_summaries.append({"test": test_name, "question": c.get("question", "")[:80], "journey": journey, "tools": tools, "judge": judge_text, "rca_issues": rca_count})
                    if not c.get("journey_success"):
                        failures.append(test_name)
                rca_summary = summary.get("rca_summary", {})
                result["results"] = {
                    "summary": {
                        "journey_success_rate": round(summary.get("journey_success_rate", 0), 1),
                        "tool_recall": round(summary.get("tool_recall", 0), 1),
                        "tool_precision": round(summary.get("tool_precision", 0), 1),
                        "llm_judge_pass_rate": round(summary.get("llm_judge_pass_rate", 0), 1),
                        "llm_judge_avg_score": round(summary.get("llm_judge_avg_score", 0), 2),
                        "llm_judge_correct": summary.get("llm_judge_correct", 0),
                        "llm_judge_partial": summary.get("llm_judge_partial", 0),
                        "llm_judge_incorrect": summary.get("llm_judge_incorrect", 0),
                        "total_cases": summary.get("total_cases", 0),
                        "rca_summary": rca_summary,
                    },
                    "cases": case_summaries,
                    "failures": failures,
                }
                status_msg = (
                    f"Pipeline complete! Journey: {summary.get('journey_success_rate', 0):.0f}% | "
                    f"Tools: {summary.get('tool_recall', 0):.0f}% | "
                    f"Judge: {summary.get('llm_judge_pass_rate', 0):.0f}%"
                )
                if rca_summary.get("total_issues", 0) > 0:
                    status_msg += f" | RCA: {rca_summary['total_issues']} issues"
                result["message"] = status_msg
        except Exception as e:
            logger.warning(f"Failed to inline results: {e}")

    return result


def _status_message(session: dict) -> str:
    """Generate a human-friendly status message."""
    status = session.get("status", "unknown")
    step = session.get("step")
    progress = session.get("progress")

    if status == "configuring":
        return "Session is being configured. Tell me the agent name and upload your Excel file."
    elif status == "uploading":
        return "Waiting for Excel file upload. Use the presigned URL to upload."
    elif status == "running":
        if progress:
            return progress
        step_names = {"generate": "Generating test cases", "evaluate": "Evaluating agent",
                      "enrich": "Extracting tool calls", "analyze": "Running LLM analysis"}
        return step_names.get(step, f"Running ({step})...")
    elif status == "completed":
        return "Pipeline completed! Ask me to show results."
    elif status == "failed":
        error = session.get("error", "Unknown error")
        return f"Pipeline failed: {error}"
    return f"Status: {status}"


def handle_results(body: dict) -> dict:
    """Get evaluation results from S3."""
    import storage
    import session_store

    session_id = body.get("session_id")
    run_id = body.get("run_id")

    if not run_id:
        if session_id:
            session = session_store.get_session(session_id)
            if session:
                run_id = session.get("run_id")
        if not run_id:
            latest = session_store.find_latest_session()
            if latest:
                run_id = latest.get("run_id")
                session_id = latest["session_id"]

    if not run_id:
        # Fall back to S3-based lookup
        run_id = storage.find_latest_run()
        if not run_id:
            raise ValueError("No completed evaluation runs found.")

    report = storage.read_json(f"analyze/{run_id}/report.json")
    if not report:
        raise ValueError(f"No analysis report found for run {run_id}. Pipeline may still be running.")

    summary = report.get("summary", {})
    cases = report.get("cases", [])

    # Build simplified per-test results
    case_summaries = []
    failures = []
    for c in cases:
        test_name = c.get("test_name", "?")
        journey = "PASS" if c.get("journey_success") else "FAIL"
        tools = f"{c.get('correct_tool_count', 0)}/{c.get('expected_tool_count', 0)}"
        judge = c.get("llm_judge", {})
        judge_verdict = judge.get("verdict", "N/A")
        judge_score = judge.get("score", 0)
        judge_text = f"{judge_verdict} ({judge_score:.1f})"

        # Include RCA count per test
        rca_count = len(c.get("rca", []))

        case_summaries.append({
            "test": test_name,
            "question": c.get("question", "")[:80],
            "journey": journey,
            "tools": tools,
            "judge": judge_text,
            "rca_issues": rca_count,
        })
        if not c.get("journey_success"):
            failures.append(test_name)

    # RCA summary from report
    rca_summary = summary.get("rca_summary", {})

    result_msg = (
        f"Journey: {summary.get('journey_success_rate', 0):.0f}% | "
        f"Tools: {summary.get('tool_recall', 0):.0f}% recall | "
        f"Judge: {summary.get('llm_judge_pass_rate', 0):.0f}%"
    )
    if rca_summary.get("total_issues", 0) > 0:
        result_msg += f" | RCA: {rca_summary['total_issues']} issues ({rca_summary.get('missing_tool_calls', 0)} missing, {rca_summary.get('extra_tool_calls', 0)} extra)"

    return {
        "session_id": session_id,
        "run_id": run_id,
        "summary": {
            "journey_success_rate": round(summary.get("journey_success_rate", 0), 1),
            "tool_recall": round(summary.get("tool_recall", 0), 1),
            "tool_precision": round(summary.get("tool_precision", 0), 1),
            "llm_judge_pass_rate": round(summary.get("llm_judge_pass_rate", 0), 1),
            "llm_judge_avg_score": round(summary.get("llm_judge_avg_score", 0), 2),
            "llm_judge_correct": summary.get("llm_judge_correct", 0),
            "llm_judge_partial": summary.get("llm_judge_partial", 0),
            "llm_judge_incorrect": summary.get("llm_judge_incorrect", 0),
            "total_cases": summary.get("total_cases", 0),
            "rca_summary": rca_summary,
        },
        "cases": case_summaries,
        "failures": failures,
        "message": result_msg,
    }


def handle_explain(body: dict) -> dict:
    """Run LLM-as-Judge analysis for a specific test case, or return all failures with RCA.

    When test_name="all" or "failures", reads the existing report and returns
    detailed RCA for all cases with issues — no re-analysis needed.
    """
    import storage
    import session_store

    test_name = body.get("test_name", "all")

    session_id = body.get("session_id")
    run_id = body.get("run_id")

    if not run_id:
        if session_id:
            session = session_store.get_session(session_id)
            if session:
                run_id = session.get("run_id")
        if not run_id:
            latest = session_store.find_latest_session()
            if latest:
                run_id = latest.get("run_id")
        if not run_id:
            run_id = storage.find_latest_run()

    if not run_id:
        raise ValueError("No run found. Run the pipeline first.")

    # --- "all" mode: return full RCA details from existing report ---
    if test_name in ("all", "failures"):
        return _explain_all_failures(session_id, run_id)

    # --- Single test mode ---
    from pipeline.analyze import analyze_single_case
    from auth import get_wxo_credentials

    # Normalize test name
    if test_name.isdigit():
        test_name = f"test_{int(test_name):03d}"
    elif not test_name.startswith("test_"):
        test_name = f"test_{test_name}"

    # Load enriched test case and messages
    enriched = storage.read_json(f"enriched/{run_id}/{test_name}.json")
    if not enriched:
        raise ValueError(f"Test case not found: {test_name} in run {run_id}")

    messages = storage.read_json(f"eval_results/{run_id}/messages/{test_name}.messages.json", [])

    # Run analysis with LLM-as-Judge + RCA via Orchestrate Gateway
    token, instance_url = get_wxo_credentials()

    result = analyze_single_case(
        enriched, messages, token=token, instance_url=instance_url,
        skip_judge=False, skip_rca=False,
    )

    # Format explanation from LLM judge
    judge = result.get("llm_judge", {})
    explanation = (
        f"Verdict: {judge.get('verdict', 'N/A')}\n"
        f"Score: {judge.get('score', 0):.2f}\n"
        f"Correctness: {judge.get('correctness', 0):.2f}\n"
        f"Completeness: {judge.get('completeness', 0):.2f}\n"
        f"Relevance: {judge.get('relevance', 0):.2f}\n"
        f"Reasoning: {judge.get('reasoning', 'No reasoning available')}"
    )

    # Format RCA explanation for failed tool calls
    rca_list = result.get("rca", [])
    rca_explanation = ""
    if rca_list:
        rca_lines = ["\nRoot Cause Analysis:"]
        for i, rca in enumerate(rca_list, 1):
            rca_lines.append(
                f"\n  Issue {i}:\n"
                f"    Reason: {rca.get('reason_tag', 'unknown')}\n"
                f"    Severity: {rca.get('severity', 'unknown')}\n"
                f"    Root Cause: {rca.get('root_cause', '')}\n"
                f"    Suggestion: {rca.get('suggestion', '')}"
            )
        rca_explanation = "\n".join(rca_lines)

    # Pre-render the analysis as a formatted table
    journey_str = "PASS" if result.get("journey_success") else "FAIL"
    correct_tools = sum(1 for v in result.get("tool_verdicts", []) if v.get("matched"))
    total_tools = len(result.get("tool_verdicts", []))

    lines = [
        f"**{test_name}: {result.get('question', '')}**",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Journey | {journey_str} |",
        f"| Tools | {correct_tools}/{total_tools} matched |",
        f"| Verdict | {judge.get('verdict', 'N/A')} ({judge.get('score', 0):.2f}) |",
        "",
        f"**Reasoning:** {judge.get('reasoning', 'N/A')}",
    ]
    for i, rca in enumerate(rca_list, 1):
        lines.append("")
        lines.append(f"**Root Cause {i}:** {rca.get('root_cause', '')}")
        lines.append(f"**Suggestion {i}:** {rca.get('suggestion', '')}")

    return {"message": "\n".join(lines)}


def _classify_severity(judge: dict, journey_pass: bool, tool_verdicts: list) -> dict:
    """Composite severity: worst signal across 3 dimensions wins.

    Dimensions:
      1. Answer quality (LLM judge score)
         - HIGH:   score < 0.3
         - MEDIUM: 0.3 <= score < 0.7
         - GOOD:   score >= 0.7
      2. Journey (conversation flow)
         - HIGH:   journey FAIL
         - GOOD:   journey PASS
      3. Tool routing (tool call verdicts)
         - HIGH:   missing tool call OR incorrect tool called
         - MEDIUM: extra tool call
         - GOOD:   all correct

    Final severity = worst across all three.

    Returns dict with overall severity + per-dimension breakdown.
    """
    score = judge.get("score", 0)

    # --- Dimension 1: Answer quality ---
    if score < 0.3:
        answer_sev = "high"
    elif score < 0.7:
        answer_sev = "medium"
    else:
        answer_sev = "good"

    # --- Dimension 2: Journey ---
    journey_sev = "good" if journey_pass else "high"

    # --- Dimension 3: Tool routing ---
    has_missing_or_incorrect = any(
        v.get("verdict", "") in ("missing tool call", "incorrect tool called")
        for v in tool_verdicts
    )
    has_extra = any(
        v.get("verdict", "") == "extra tool call"
        for v in tool_verdicts
    )

    if has_missing_or_incorrect:
        tool_sev = "high"
    elif has_extra:
        tool_sev = "medium"
    else:
        tool_sev = "good"

    # --- Composite: worst wins ---
    order = {"high": 0, "medium": 1, "good": 2}
    dims = [answer_sev, journey_sev, tool_sev]
    overall = min(dims, key=lambda s: order[s])

    return {
        "overall": overall,
        "answer": answer_sev,
        "journey": journey_sev,
        "tools": tool_sev,
    }


# Severity sort order: high=0, medium=1, good=2
_SEVERITY_ORDER = {"high": 0, "medium": 1, "good": 2}


def _explain_all_failures(session_id: str, run_id: str) -> dict:
    """Return detailed RCA breakdown for ALL cases from the existing report.

    Reads analyze/{run_id}/report.json — no re-analysis needed.
    Returns ALL cases sorted by severity: HIGH → MEDIUM → GOOD.
    Even correct cases get analysis with "good" severity and suggestions.
    """
    import storage

    report = storage.read_json(f"analyze/{run_id}/report.json")
    if not report:
        raise ValueError(f"No analysis report found for run {run_id}. Run the pipeline first.")

    summary = report.get("summary", {})
    cases = report.get("cases", [])
    rca_summary = summary.get("rca_summary", {})

    # Build detailed breakdown for EVERY case
    detailed_cases = []
    severity_counts = {"high": 0, "medium": 0, "good": 0}

    for c in cases:
        test_name = c.get("test_name", "?")
        rca_list = c.get("rca", [])
        tool_verdicts = c.get("tool_verdicts", [])
        judge = c.get("llm_judge", {})
        journey_pass = c.get("journey_success", False)

        # Composite severity: worst signal across answer/journey/tools
        sev = _classify_severity(judge, journey_pass, tool_verdicts)
        severity = sev["overall"]
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

        # Build human-readable explanation
        explanation = ""
        if judge:
            explanation = (
                f"Verdict: {judge.get('verdict', 'N/A')}\n"
                f"Score: {judge.get('score', 0):.2f}\n"
                f"Correctness: {judge.get('correctness', 0):.2f}\n"
                f"Completeness: {judge.get('completeness', 0):.2f}\n"
                f"Relevance: {judge.get('relevance', 0):.2f}\n"
                f"Reasoning: {judge.get('reasoning', 'N/A')}"
            )

        rca_explanation = ""
        if rca_list:
            rca_lines = ["Root Cause Analysis:"]
            for i, rca in enumerate(rca_list, 1):
                rca_lines.append(
                    f"  Issue {i}:\n"
                    f"    Reason: {rca.get('reason_tag', 'unknown')}\n"
                    f"    Severity: {rca.get('severity', 'unknown')}\n"
                    f"    Root Cause: {rca.get('root_cause', '')}\n"
                    f"    Suggestion: {rca.get('suggestion', '')}"
                )
            rca_explanation = "\n".join(rca_lines)

        # Count non-correct tool verdicts
        tool_issues = [v for v in tool_verdicts if v.get("verdict", "") != "correct"]

        detailed_cases.append({
            "test_name": test_name,
            "question": c.get("question", ""),
            "journey": "PASS" if journey_pass else "FAIL",
            "tools": f"{c.get('correct_tool_count', 0)}/{c.get('expected_tool_count', 0)}",
            "judge": f"{judge.get('verdict', 'N/A')} ({judge.get('score', 0):.1f})",
            "judge_score": judge.get("score", 0),
            "severity": severity,
            "severity_breakdown": {
                "answer": sev["answer"],
                "journey": sev["journey"],
                "tools": sev["tools"],
            },
            "tool_verdicts": tool_verdicts,
            "tool_issues": len(tool_issues),
            "rca": rca_list,
            "explanation": explanation,
            "rca_explanation": rca_explanation,
            "agent_response": c.get("agent_response", ""),
        })

    # Sort by severity: high first, then medium, then good
    detailed_cases.sort(key=lambda x: (_SEVERITY_ORDER.get(x["severity"], 9), x["test_name"]))

    return {
        "session_id": session_id,
        "run_id": run_id,
        "mode": "all",
        "total_cases": len(detailed_cases),
        "severity_counts": severity_counts,
        "rca_summary": rca_summary,
        "cases": detailed_cases,
        "message": (
            f"Analysis for {len(detailed_cases)} test cases: "
            f"{severity_counts.get('high', 0)} HIGH severity, "
            f"{severity_counts.get('medium', 0)} MEDIUM severity, "
            f"{severity_counts.get('good', 0)} GOOD. "
            f"Sorted by priority (high → medium → good)."
        ),
    }


def handle_reanalyze(body: dict) -> dict:
    """Re-run analysis on existing eval results without re-running the full pipeline.

    Useful for:
    - Re-analyzing with different settings (skip_rca, skip_judge, model)
    - Running RCA on results that were originally analyzed without it
    - Changing the model for LLM-as-Judge
    """
    import storage
    import session_store
    from pipeline.analyze import analyze_run
    from auth import get_wxo_credentials

    run_id = body.get("run_id")
    session_id = body.get("session_id")

    if not run_id:
        if session_id:
            session = session_store.get_session(session_id)
            if session:
                run_id = session.get("run_id")
        if not run_id:
            latest = session_store.find_latest_session()
            if latest:
                run_id = latest.get("run_id")
                session_id = latest["session_id"]
        if not run_id:
            run_id = storage.find_latest_run()

    if not run_id:
        raise ValueError("No run found. Run the pipeline first.")

    # Check that enriched data exists for this run
    enriched_keys = storage.list_keys(f"enriched/{run_id}/", suffix=".json")
    if not enriched_keys:
        raise ValueError(f"No enriched test cases for run {run_id}. Run the full pipeline first.")

    # Config overrides from request
    skip_judge = body.get("skip_judge", False)
    skip_rca = body.get("skip_rca", False)
    model_id = body.get("model_id", "meta-llama/llama-3-3-70b-instruct")

    token, instance_url = get_wxo_credentials()

    # Update session status if we have one
    if session_id:
        try:
            session_store.set_status(
                session_id, "running", step="reanalyze",
                progress="Re-analyzing with updated settings..."
            )
        except Exception:
            pass

    report = analyze_run(
        run_id=run_id,
        skip_judge=skip_judge,
        skip_rca=skip_rca,
        token=token,
        instance_url=instance_url,
        model=model_id,
    )

    if report.get("error"):
        raise ValueError(report["error"])

    summary = report.get("summary", {})
    rca_summary = summary.get("rca_summary", {})

    # Update session as completed
    if session_id:
        try:
            progress_msg = (
                f"Re-analysis complete! Journey: {summary.get('journey_success_rate', 0):.0f}% | "
                f"Tools: {summary.get('tool_recall', 0):.0f}% | "
                f"Judge: {summary.get('llm_judge_pass_rate', 0):.0f}%"
            )
            if rca_summary.get("total_issues", 0) > 0:
                progress_msg += f" | RCA: {rca_summary['total_issues']} issues"
            session_store.set_status(
                session_id, "completed", step="done",
                progress=progress_msg,
                results_key=f"analyze/{run_id}/report.json",
            )
        except Exception:
            pass

    return {
        "session_id": session_id,
        "run_id": run_id,
        "summary": {
            "journey_success_rate": round(summary.get("journey_success_rate", 0), 1),
            "tool_recall": round(summary.get("tool_recall", 0), 1),
            "llm_judge_pass_rate": round(summary.get("llm_judge_pass_rate", 0), 1),
            "llm_judge_avg_score": round(summary.get("llm_judge_avg_score", 0), 2),
            "total_cases": summary.get("total_cases", 0),
            "rca_summary": rca_summary,
        },
        "settings_used": {
            "skip_judge": skip_judge,
            "skip_rca": skip_rca,
            "model_id": model_id,
        },
        "message": (
            f"Re-analysis complete! Journey: {summary.get('journey_success_rate', 0):.0f}% | "
            f"RCA analyzed: {rca_summary.get('total_analyzed', 0)} tool calls"
        ),
    }


def _find_enriched_run(run_id, session_id):
    """Find a run_id with enriched data. Returns (run_id, enriched_count) or (None, 0)."""
    import storage
    import session_store

    # Try the provided/session run_id first
    if run_id:
        keys = storage.list_keys(f"enriched/{run_id}/", suffix=".json")
        if keys:
            return run_id, len(keys)

    # Try latest session
    if not run_id:
        if session_id:
            session = session_store.get_session(session_id)
            if session and session.get("run_id"):
                rid = session["run_id"]
                keys = storage.list_keys(f"enriched/{rid}/", suffix=".json")
                if keys:
                    return rid, len(keys)
        latest = session_store.find_latest_session()
        if latest and latest.get("run_id"):
            rid = latest["run_id"]
            keys = storage.list_keys(f"enriched/{rid}/", suffix=".json")
            if keys:
                return rid, len(keys)

    # Try S3-based latest run
    fallback = storage.find_latest_run()
    if fallback:
        keys = storage.list_keys(f"enriched/{fallback}/", suffix=".json")
        if keys:
            return fallback, len(keys)

    return None, 0


def _find_recent_enriched_runs(limit=5):
    """Find multiple recent enriched runs. Returns list of {run_id, enriched_count}."""
    import storage

    all_keys = storage.list_keys("enriched/", suffix=".json")
    if not all_keys:
        return []

    # Group by run_id: enriched/{run_id}/test_001.json
    runs = {}
    for k in all_keys:
        parts = k.split("/")
        if len(parts) >= 3:
            rid = parts[1]
            runs[rid] = runs.get(rid, 0) + 1

    # Sort by run_id descending (they contain timestamps like pipe_20260228_005422)
    sorted_runs = sorted(runs.items(), key=lambda x: x[0], reverse=True)[:limit]

    return [{"run_id": rid, "enriched_count": count} for rid, count in sorted_runs]


def handle_redteam(body: dict) -> dict:
    """Run red teaming analysis asynchronously via pipeline Lambda.

    Smart routing based on source parameter:
    - source="auto" (default):
        - If CURRENT session ran eval and has enriched data → auto-run/show cached
        - If no eval in current session → return no_data with recent runs list (ask user)
    - source="latest": use most recent enriched run directly
    - source="show": preview available enriched data
    - source="run:{run_id}": run red team on a specific run_id the user picked
    """
    import json
    import storage
    import session_store
    import boto3

    run_id = body.get("run_id")
    session_id = body.get("session_id")
    source = body.get("source", "auto")

    # --- Validate and resolve session ---
    # LLM sometimes hallucinates session_id — validate it starts with "sess_"
    session_from_agent = False
    if session_id and not session_id.startswith("sess_"):
        logger.warning(f"Invalid session_id from agent: {session_id}, falling back to latest")
        session_id = None
    if session_id:
        session_from_agent = True
    else:
        latest = session_store.find_latest_session()
        if latest:
            session_id = latest["session_id"]

    # --- "show" mode: preview available enriched data ---
    if source == "show":
        found_run, count = _find_enriched_run(run_id, session_id)
        if found_run:
            keys = storage.list_keys(f"enriched/{found_run}/", suffix=".json")
            preview_cases = []
            for k in keys[:5]:
                tc = storage.read_json(k)
                if tc:
                    preview_cases.append({
                        "test": k.split("/")[-1].replace(".json", ""),
                        "question": tc.get("starting_sentence", "")[:80],
                        "agent": tc.get("agent", ""),
                        "tools_expected": len([d for d in tc.get("goal_details", []) if d.get("type") == "tool_call"]),
                    })
            return {
                "session_id": session_id,
                "run_id": found_run,
                "status": "available",
                "enriched_count": count,
                "preview": preview_cases,
                "message": f"Found {count} enriched test cases from run {found_run}.",
            }
        return {
            "status": "no_data",
            "message": "No enriched evaluation data found.",
        }

    # --- "uploaded" mode: user uploaded JSON for red teaming ---
    if source == "uploaded":
        return _handle_redteam_uploaded(session_id, body.get("model_id"), session_store)

    # --- "run:{run_id_or_number}" mode: user picked a specific run ---
    if source.startswith("run:"):
        picked = source.split(":", 1)[1].strip()
        # If it's a number (e.g. "2"), resolve to actual run_id from recent runs list
        if picked.isdigit():
            idx = int(picked) - 1  # 1-based → 0-based
            recent = _find_recent_enriched_runs(limit=10)
            if 0 <= idx < len(recent):
                picked_run_id = recent[idx]["run_id"]
                logger.info(f"Resolved run number {picked} to {picked_run_id}")
            else:
                return {"status": "no_data", "message": f"Invalid run number: {picked}. Available: 1-{len(recent)}."}
        else:
            picked_run_id = picked
        keys = storage.list_keys(f"enriched/{picked_run_id}/", suffix=".json")
        if not keys:
            return {"status": "no_data", "message": f"No enriched data found for run {picked_run_id}."}
        # Check cache
        existing = storage.read_json(f"redteam/{picked_run_id}/report.json")
        if existing:
            return _format_redteam_complete(existing, session_id, picked_run_id)
        # Invoke async
        return _invoke_redteam_async(
            session_id, picked_run_id, len(keys), body.get("model_id"), session_store
        )

    # --- "latest" mode: explicitly use most recent enriched run ---
    if source == "latest":
        found_run, count = _find_enriched_run(None, session_id)
        if not found_run:
            return {"status": "no_data", "message": "No enriched evaluation data found anywhere."}
        # Check cache
        existing = storage.read_json(f"redteam/{found_run}/report.json")
        if existing:
            return _format_redteam_complete(existing, session_id, found_run)
        # Invoke async
        return _invoke_redteam_async(
            session_id, found_run, count, body.get("model_id"), session_store
        )

    # --- "auto" mode: smart routing ---
    # Step 1: Check if CURRENT session has its own eval data (enriched)
    # Only trust session data if the agent actually provided the session_id
    # (not resolved via fallback — fallback means new session with no tracking)
    current_session_run_id = None
    current_session_has_eval = False
    if session_id and session_from_agent:
        session = session_store.get_session(session_id)
        if session and session.get("run_id"):
            current_session_run_id = session["run_id"]
            enriched_keys = storage.list_keys(f"enriched/{current_session_run_id}/", suffix=".json")
            if enriched_keys:
                current_session_has_eval = True

    if current_session_has_eval:
        # This session ran eval — auto-run or show cached results
        rid = current_session_run_id
        enriched_count = len(storage.list_keys(f"enriched/{rid}/", suffix=".json"))
        existing = storage.read_json(f"redteam/{rid}/report.json")
        if existing:
            return _format_redteam_complete(existing, session_id, rid)
        return _invoke_redteam_async(
            session_id, rid, enriched_count, body.get("model_id"), session_store
        )

    # Step 2: No eval in current session — ALWAYS ask the user
    recent_runs = _find_recent_enriched_runs(limit=5)
    if recent_runs:
        return {
            "session_id": session_id,
            "status": "no_data",
            "recent_runs": recent_runs,
            "latest_run_id": recent_runs[0]["run_id"],
            "latest_enriched_count": recent_runs[0]["enriched_count"],
            "message": (
                f"No eval data in this session. I found {len(recent_runs)} recent eval run(s). "
                f"Would you like to red team the recent data, or upload your own JSON file?"
            ),
        }
    else:
        return {
            "session_id": session_id,
            "status": "no_data",
            "recent_runs": [],
            "message": (
                "No enriched evaluation data found anywhere. "
                "Please run the full eval pipeline first, or upload a JSON file with test data."
            ),
        }


def _handle_redteam_uploaded(session_id: str, model_id: str, session_store) -> dict:
    """Process uploaded JSON file for red teaming.

    Reads the JSON from uploads/{session_id}/redteam_data.json,
    converts each entry to enriched format + synthetic messages,
    saves to enriched/{run_id}/ and eval_results/{run_id}/messages/,
    then invokes red team async.
    """
    import storage

    # Find the uploaded JSON file
    json_key = f"uploads/{session_id}/redteam_data.json"
    data = storage.read_json(json_key)

    if not data:
        # Try to find any JSON file in the session's upload folder
        all_upload_keys = storage.list_keys(f"uploads/{session_id}/", suffix=".json")
        for k in all_upload_keys:
            data = storage.read_json(k)
            if data:
                json_key = k
                break

    if not data:
        return {
            "session_id": session_id,
            "status": "no_data",
            "message": "No uploaded JSON file found. Please upload a JSON file first.",
        }

    if not isinstance(data, list):
        return {
            "session_id": session_id,
            "status": "no_data",
            "message": "Invalid JSON format. Expected an array of test cases.",
        }

    # Generate a run_id for this uploaded data
    run_id = f"pipe_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    # Convert each entry to enriched format + synthetic messages
    for i, entry in enumerate(data):
        test_name = f"test_{i+1:03d}"
        question = entry.get("question", "")
        agent_response = entry.get("agent_response", "")
        agent_name = entry.get("agent_name", "unknown_agent")
        expected_answer = entry.get("expected_answer", "")

        # Create enriched format (matches what the pipeline produces)
        enriched = {
            "starting_sentence": question,
            "story": question,
            "agent": agent_name,
            "goal_details": [],
        }
        if expected_answer:
            enriched["goal_details"].append({
                "type": "text",
                "name": "summarize",
                "response": expected_answer,
            })

        # Create synthetic messages (matches WxO message format)
        messages = [
            {"message": {"role": "user", "type": "text", "content": question}},
            {"message": {"role": "assistant", "type": "text", "content": agent_response}},
        ]

        # Save to S3 in the same structure the pipeline uses
        storage.write_json(f"enriched/{run_id}/{test_name}.json", enriched)
        storage.write_json(
            f"eval_results/{run_id}/messages/{test_name}.messages.json", messages
        )

    logger.info(
        f"[RedTeam] Uploaded JSON processed: {len(data)} cases → run_id={run_id}"
    )

    # Store the run_id on the session so auto-mode can find it later
    try:
        session_store.set_status(
            session_id, "running", step="redteam",
            run_id=run_id,
            progress=f"Processing uploaded JSON ({len(data)} test cases)...",
        )
    except Exception:
        pass

    # Invoke red team async
    return _invoke_redteam_async(
        session_id, run_id, len(data), model_id, session_store
    )


def _format_redteam_complete(report: dict, session_id: str, run_id: str) -> dict:
    """Format cached redteam results for response."""
    summary = report.get("summary", {})
    all_findings = report.get("all_findings", [])
    finding_lines = []
    for f in all_findings[:10]:
        finding_lines.append(
            f"[{f.get('severity', '?').upper()}] {f.get('category', '?')}: "
            f"{f.get('description', '')} (test: {f.get('test_name', '?')})"
        )
    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": "complete",
        "summary": summary,
        "findings": all_findings,
        "findings_text": "\n".join(finding_lines) if finding_lines else "No findings.",
        "message": (
            f"Red team analysis complete: {summary.get('total_findings', 0)} findings "
            f"across {summary.get('total_cases', 0)} test cases "
            f"({summary.get('high_risk_cases', 0)} high risk)"
        ),
    }


def _invoke_redteam_async(session_id, run_id, enriched_count, model_id, session_store):
    """Invoke pipeline Lambda async for red teaming."""
    import json
    import boto3

    model_id = model_id or "meta-llama/llama-3-3-70b-instruct"
    pipeline_lambda = os.environ.get("PIPELINE_LAMBDA", "wxo-eval-pipeline")

    try:
        client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        client.invoke(
            FunctionName=pipeline_lambda,
            InvocationType="Event",
            Payload=json.dumps({
                "step": "redteam",
                "session_id": session_id,
                "run_id": run_id,
                "config": {"model_id": model_id},
            }),
        )
    except Exception as e:
        logger.error(f"Failed to invoke pipeline Lambda for redteam: {e}")
        raise ValueError(f"Failed to start red team analysis: {e}")

    if session_id:
        try:
            session_store.set_status(
                session_id, "running", step="redteam",
                progress=f"Red team analysis started on {enriched_count} test cases..."
            )
        except Exception:
            pass

    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": "started",
        "enriched_count": enriched_count,
        "message": (
            f"Red team analysis started on {enriched_count} test cases. "
            "Running in the background — I'll poll for results."
        ),
    }


# ---------------------------------------------------------------------------
# New Red Team Routes (attack-based red teaming)
# ---------------------------------------------------------------------------

def handle_redteam_list(body: dict) -> dict:
    """Return the 15 attack types with a pre-formatted display table."""
    from pipeline.redteam import list_attacks

    category = body.get("category")
    # Ignore null/empty string values from LLM tool calls
    if category in (None, "", "null", "None"):
        category = None
    attacks = list_attacks(category=category)

    on_policy_count = sum(1 for a in attacks if a["attack_category"] == "on_policy")
    off_policy_count = sum(1 for a in attacks if a["attack_category"] == "off_policy")
    total_variants = sum(a["variant_count"] for a in attacks)

    # Build pre-formatted table for the agent to display directly
    lines = []
    lines.append("**Available Red Team Attacks:**\n")
    lines.append("| # | Category | Name | Type | Variants |")
    lines.append("|---|----------|------|------|----------|")
    for a in attacks:
        lines.append(
            f"| {a['index']} | {a['attack_category']} | {a['attack_name']} "
            f"| {a['attack_type']} | {a['variant_count']} |"
        )
    lines.append("")
    lines.append(f"**On-policy** ({on_policy_count}): Test if the agent can be manipulated within its domain.")
    lines.append(f"**Off-policy** ({off_policy_count}): Test for prompt leakage, unsafe topics, jailbreaking.")
    lines.append(f"\n**Total: {len(attacks)} attack types, {total_variants} variants.**")
    lines.append("\nWhich attacks do you want to run? You can say:")
    lines.append("- **all** to run everything")
    lines.append("- Attack names: e.g. `crescendo_prompt_leakage, jailbreaking`")
    lines.append("- Numbers: e.g. `1, 3, 10`")

    return {
        "message": "\n".join(lines),
    }


def _format_attack_table(attacks: list) -> str:
    """Build a pre-formatted attack table string."""
    lines = []
    lines.append("| # | Category | Name | Type | Variants |")
    lines.append("|---|----------|------|------|----------|")
    for a in attacks:
        lines.append(
            f"| {a['index']} | {a['attack_category']} | {a['attack_name']} "
            f"| {a['attack_type']} | {a['variant_count']} |"
        )
    on_count = sum(1 for a in attacks if a["attack_category"] == "on_policy")
    off_count = sum(1 for a in attacks if a["attack_category"] == "off_policy")
    total_v = sum(a["variant_count"] for a in attacks)
    lines.append(f"\n**On-Policy** ({on_count}) | **Off-Policy** ({off_count}) | **Total: {len(attacks)} attacks, {total_v} variants**")
    lines.append("\nSay **all**, attack names (e.g. `crescendo_prompt_leakage, jailbreaking`), or numbers (e.g. `1, 3, 10`).")
    return "\n".join(lines)


def handle_redteam_start(body: dict) -> dict:
    """Start a red team attack campaign via Step Functions."""
    import boto3
    import session_store

    attacks = body.get("attacks")
    agent_name = body.get("agent_name")
    confirmed = body.get("confirmed", False)

    # --- VALIDATION: API controls the conversation flow ---
    import re
    from pipeline.redteam import list_attacks

    # Step 1: Validate agent_name
    if not agent_name or not re.match(r'^[a-zA-Z0-9_]+$', agent_name.strip()):
        return {
            "status": "need_input",
            "message": "Which WxO agent do you want to red team? Please provide the exact agent name (e.g. your_target_agent).",
        }

    agent_name = agent_name.strip()

    # Step 2: ALWAYS show attack choices first — ignore attacks param unless confirmed=true
    if not confirmed:
        return {
            "status": "need_input",
            "message": (
                f"Ask the user to pick attacks for {agent_name}. "
                f"Available attacks — On-Policy: instruction_override, crescendo_attack, emotional_appeal, imperative_emphasis, role_playing, random_prefix, random_postfix, encoded_input, foreign_languages. "
                f"Off-Policy: crescendo_prompt_leakage, functionality_based_attacks, undermine_model, unsafe_topics, jailbreaking, topic_derailment. "
                f"User can say all or pick specific names. Set confirmed=true after user picks."
            ),
        }

    # Step 3: confirmed=true — validate attacks and start
    if not attacks:
        return {
            "status": "need_input",
            "message": f"You need to include the attacks parameter. Ask the user which attacks to run against {agent_name}.",
        }

    # Normalize attacks input
    if isinstance(attacks, str) and attacks != "all":
        attacks = [a.strip() for a in attacks.split(",")]

    model_id = body.get("model_id", "meta-llama/llama-3-3-70b-instruct")
    max_variants = body.get("max_variants")

    # Resolve or create session
    session_id = body.get("session_id")
    if not session_id:
        latest = session_store.find_latest_session()
        if latest:
            session_id = latest["session_id"]
        else:
            session = session_store.create_session()
            session_id = session["session_id"]

    # Generate run_id
    run_id = f"rt_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    # Count total attacks for estimation
    from pipeline.redteam import ATTACK_LIST
    if attacks == "all" or attacks == ["all"]:
        total_variants = sum(
            len(a["attack_instructions"])
            for a in ATTACK_LIST
        )
        attack_count = len(ATTACK_LIST)
    else:
        attack_names = attacks if isinstance(attacks, list) else [attacks]
        total_variants = 0
        attack_count = 0
        for a in ATTACK_LIST:
            if a["attack_name"] in attack_names:
                v = len(a["attack_instructions"])
                if max_variants:
                    v = min(v, int(max_variants))
                total_variants += v
                attack_count += 1

    config = {
        "attacks": attacks,
        "agent_name": agent_name,
        "model_id": model_id,
        "max_variants": max_variants,
    }

    # Start Step Functions execution
    sfn_arn = REDTEAM_SFN_ARN
    if not sfn_arn:
        raise ValueError("Red team Step Functions ARN not configured (REDTEAM_SFN_ARN env var)")

    sfn = boto3.client("stepfunctions", region_name=REGION)
    sfn_input = {
        "session_id": session_id,
        "run_id": run_id,
        "config": config,
    }

    execution = sfn.start_execution(
        stateMachineArn=sfn_arn,
        name=f"{session_id}-{run_id}",
        input=json.dumps(sfn_input, default=str),
    )

    # Update session
    session_store.set_status(
        session_id, "running",
        step="redteam_plan",
        progress=f"Red team started: planning {total_variants} attack scenarios...",
        run_id=run_id,
        error="",
    )

    # Estimate duration: ~3 min per batch of 3 at concurrency 3
    est_batches = (total_variants + 2) // 3
    est_minutes = max(5, est_batches * 3)

    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": "started",
        "attack_count": attack_count,
        "total_attacks": total_variants,
        "est_duration": f"~{est_minutes} minutes",
        "execution_arn": execution["executionArn"],
        "message": (
            f"Red team campaign started: {total_variants} attacks against {agent_name}. "
            f"Estimated duration: ~{est_minutes} minutes. "
            "Tell the user to type status to check progress. Do NOT call eval_status yourself."
        ),
    }


def handle_redteam_results(body: dict) -> dict:
    """Get red team attack report from S3."""
    import storage
    import session_store

    session_id = body.get("session_id")
    run_id = body.get("run_id")

    if not run_id:
        if session_id:
            session = session_store.get_session(session_id)
            if session:
                run_id = session.get("run_id")
        if not run_id:
            latest = session_store.find_latest_session()
            if latest:
                run_id = latest.get("run_id")
                session_id = latest["session_id"]

    if not run_id:
        # Try to find the latest red team report
        rt_keys = storage.list_keys("redteam/", suffix="/report.json")
        if rt_keys:
            # Extract run_id from redteam/{run_id}/report.json
            run_id = rt_keys[-1].split("/")[1]
        else:
            raise ValueError("No red team results found. Start a campaign first.")

    report = storage.read_json(f"redteam/{run_id}/report.json")
    if not report:
        raise ValueError(f"No red team report found for run {run_id}. Campaign may still be running.")

    summary = report.get("summary", {})
    attacks = report.get("attacks", [])
    recommendations = report.get("recommendations", [])

    # Build per-attack summary
    attack_summaries = []
    for a in attacks:
        attack_summaries.append({
            "attack_name": a.get("attack_name"),
            "attack_category": a.get("attack_category"),
            "attack_type": a.get("attack_type"),
            "succeeded": a.get("succeeded", False),
            "turns": a.get("turns", 0),
            "explanation": a.get("details", {}).get("explanation", ""),
        })

    # Build pre-formatted display text
    on_p = summary.get("on_policy", {})
    off_p = summary.get("off_policy", {})
    succeeded = summary.get("succeeded", 0)
    total = summary.get("total_attacks", 0)
    rate = summary.get("success_rate", 0)

    lines = []
    lines.append(f"**Red Team Results — {succeeded}/{total} attacks succeeded ({rate:.0f}% success rate)**\n")
    lines.append("**Summary:**")
    lines.append("| Category | Total | Succeeded | Rate |")
    lines.append("|----------|-------|-----------|------|")
    on_total = on_p.get("total", 0)
    on_succ = on_p.get("succeeded", 0)
    off_total = off_p.get("total", 0)
    off_succ = off_p.get("succeeded", 0)
    on_rate = (on_succ / on_total * 100) if on_total else 0
    off_rate = (off_succ / off_total * 100) if off_total else 0
    lines.append(f"| On-policy | {on_total} | {on_succ} | {on_rate:.0f}% |")
    lines.append(f"| Off-policy | {off_total} | {off_succ} | {off_rate:.0f}% |")

    lines.append("\n**Per-Attack Breakdown:**")
    lines.append("| # | Attack | Category | Result | Explanation |")
    lines.append("|---|--------|----------|--------|-------------|")
    # Show succeeded first, then failed
    sorted_attacks = sorted(attack_summaries, key=lambda x: (not x.get("succeeded", False)))
    for i, a in enumerate(sorted_attacks, 1):
        result = "SUCCEEDED" if a.get("succeeded") else "BLOCKED"
        explanation = a.get("explanation", "")[:120]
        lines.append(f"| {i} | {a['attack_name']} | {a['attack_category']} | {result} | {explanation} |")

    if recommendations:
        lines.append("\n**Recommendations:**")
        for r in recommendations:
            lines.append(f"- {r}")
    else:
        lines.append("\n**No recommendations — all attacks were blocked.**")

    return {
        "session_id": session_id,
        "run_id": run_id,
        "message": "\n".join(lines),
    }
