"""
Evaluate Pipeline Module
=========================
Runs evaluation against a live WxO agent via direct HTTP API calls.
No dependency on the orchestrate CLI — works in Lambda.
"""

import json
import os
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def _normalize_name(s: str) -> str:
    """Normalize an agent name for fuzzy matching: lowercase, strip separators."""
    if not s:
        return ""
    return "".join(c for c in s.lower() if c.isalnum())


def _get_agent_id(instance_url: str, token: str, agent_name: str) -> Optional[str]:
    """Look up agent ID from agent name with fuzzy matching.

    Tries in order:
      1. Exact match on `name`
      2. Exact match on `display_name`
      3. Case-insensitive match on `name` or `display_name`
      4. Normalized match (lowercase, alphanumeric-only) on either field
      5. Normalized prefix match (for auto-suffixed names like XXX_18YBG)
    """
    url = f"{instance_url}/v1/orchestrate/agents"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    agents = resp.json()

    target = agent_name
    target_lower = target.lower()
    target_norm = _normalize_name(target)

    # Pass 1: exact match on name
    for a in agents:
        if a.get("name") == target:
            return a.get("id")

    # Pass 2: exact match on display_name
    for a in agents:
        if a.get("display_name") == target:
            logger.info(f"Matched agent by display_name: {target}")
            return a.get("id")

    # Pass 3: case-insensitive match on either
    for a in agents:
        if (a.get("name", "").lower() == target_lower
                or a.get("display_name", "").lower() == target_lower):
            logger.info(f"Matched agent (case-insensitive): {a.get('name')}")
            return a.get("id")

    # Pass 4: normalized match (strip underscores/hyphens)
    for a in agents:
        if (_normalize_name(a.get("name", "")) == target_norm
                or _normalize_name(a.get("display_name", "")) == target_norm):
            logger.info(f"Matched agent (normalized): {a.get('name')}")
            return a.get("id")

    # Pass 5: normalized prefix match (catches auto-suffixes like _18YBG)
    for a in agents:
        name_norm = _normalize_name(a.get("name", ""))
        display_norm = _normalize_name(a.get("display_name", ""))
        if name_norm.startswith(target_norm) or display_norm.startswith(target_norm):
            logger.info(f"Matched agent (prefix): {a.get('name')} (user typed {target!r})")
            return a.get("id")

    # No match — log available names for debugging
    available = [f"{a.get('name')} (display: {a.get('display_name')})" for a in agents[:20]]
    logger.warning(f"Agent {target!r} not found. Available: {available}")
    return None


def _send_message(instance_url: str, token: str, agent_id: str,
                  message: str, thread_id: str = None) -> Tuple[str, str]:
    """Send a message to a WxO agent. Returns (thread_id, run_id)."""
    url = f"{instance_url}/v1/orchestrate/runs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "message": {"role": "user", "content": message},
        "agent_id": agent_id,
    }
    if thread_id:
        payload["thread_id"] = thread_id

    resp = requests.post(url, headers=headers, json=payload, timeout=300)
    resp.raise_for_status()
    result = resp.json()
    logger.info(f"Run created: {json.dumps(result)[:200]}")
    return result.get("thread_id"), result.get("run_id")


def _wait_for_run(instance_url: str, token: str, run_id: str,
                  poll_interval: int = 2, max_wait: int = 600) -> Dict:
    """Poll run status until completed/failed."""
    url = f"{instance_url}/v1/orchestrate/runs/{run_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    start = time.time()
    while time.time() - start < max_wait:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        status = resp.json()
        run_state = status.get("status", "").lower()
        logger.info(f"Run {run_id} status: {run_state}")
        if run_state in ("completed", "failed", "cancelled"):
            return status
        time.sleep(poll_interval)

    return {"status": "timeout", "message": f"Run {run_id} did not complete within {max_wait}s"}


def _get_messages(instance_url: str, token: str, thread_id: str,
                  retries: int = 3, delay: float = 2.0) -> List[Dict]:
    """Get all messages from a thread (with retry for eventual consistency)."""
    url = f"{instance_url}/v1/orchestrate/threads/{thread_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    raw_messages = None
    for attempt in range(retries):
        resp = requests.get(url, headers=headers, timeout=30)
        logger.info(f"Messages API [{attempt+1}/{retries}]: status={resp.status_code}, len={len(resp.text)}")
        resp.raise_for_status()
        raw_messages = resp.json()
        if raw_messages:
            # Log structure for debugging
            if isinstance(raw_messages, list):
                logger.info(f"Messages: list of {len(raw_messages)} entries, first keys: {list(raw_messages[0].keys()) if raw_messages else '[]'}")
            elif isinstance(raw_messages, dict):
                logger.info(f"Messages: dict with keys: {list(raw_messages.keys())}")
            break
        logger.warning(f"Messages API returned empty/null, retrying in {delay}s...")
        time.sleep(delay)

    if not raw_messages:
        logger.warning(f"No messages found for thread {thread_id} after {retries} attempts")
        return []

    # Convert to the format expected by enrich/analyze
    messages = []
    if isinstance(raw_messages, dict):
        raw_messages = raw_messages.get("messages") or raw_messages.get("data") or [raw_messages]

    for entry in raw_messages:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role", "unknown")

        # --- Extract from step_history (tool calls + tool responses) ---
        step_history = entry.get("step_history") or []
        for step_msg in step_history:
            if not isinstance(step_msg, dict):
                continue
            step_role = step_msg.get("role", role)
            step_details = step_msg.get("step_details") or []
            for detail in step_details:
                if not isinstance(detail, dict):
                    continue

                dtype = detail.get("type", "")

                if dtype == "tool_calls":
                    # Plural form: tool_calls array with {id, name, args}
                    for tool in (detail.get("tool_calls") or []):
                        if not isinstance(tool, dict):
                            continue
                        messages.append({
                            "role": step_role,
                            "type": "tool_call",
                            "id": tool.get("id", ""),
                            "name": tool.get("name", ""),
                            "args": tool.get("args", {}),
                        })

                elif dtype == "tool_call":
                    # Singular form: sub-agent forwarding
                    messages.append({
                        "role": step_role,
                        "type": "tool_call",
                        "id": detail.get("tool_call_id") or detail.get("id", ""),
                        "name": detail.get("name", ""),
                        "args": detail.get("args", {}),
                    })

                elif dtype == "tool_response":
                    messages.append({
                        "role": step_role,
                        "type": "tool_response",
                        "tool_call_id": detail.get("tool_call_id", ""),
                        "name": detail.get("name", ""),
                        "content": detail.get("content") or detail.get("output") or "",
                    })

                elif dtype == "text":
                    text = detail.get("text", "")
                    if text:
                        messages.append({
                            "role": step_role,
                            "type": "text",
                            "content": text,
                        })

        # --- Extract final assistant text from top-level content[] ---
        if role == "assistant":
            content_list = entry.get("content") or []
            if isinstance(content_list, list):
                for block in content_list:
                    if isinstance(block, dict) and block.get("text"):
                        messages.append({
                            "role": "assistant",
                            "type": "text",
                            "content": block["text"],
                        })

    logger.info(f"Parsed {len(messages)} messages from thread {thread_id}")
    return messages


def run_evaluation(test_data_key: str, run_id: str,
                   token: str = None, instance_url: str = None,
                   model_id: str = "meta-llama/llama-3-3-70b-instruct",
                   limit: int = None) -> Dict[str, Any]:
    """Run evaluation by sending test questions to a WxO agent via HTTP API.

    Args:
        test_data_key: S3 prefix for test cases (e.g. "test_data/gen_xxx/")
        run_id: Unique run identifier
        token: WxO auth token
        instance_url: WxO instance URL
        model_id: LLM model (unused in native mode, kept for compatibility)
        limit: Max test cases to evaluate

    Returns:
        {run_id, status, test_count, duration, message}
    """
    import storage

    instance_url = instance_url.rstrip("/")

    # Update run status
    storage.save_run_status(run_id, {
        "run_id": run_id,
        "status": "running",
        "step": "evaluate",
        "progress": "0/?",
        "started_at": datetime.utcnow().isoformat(),
    })

    # Load test cases from S3
    test_keys = storage.list_keys(test_data_key, suffix=".json")
    if limit:
        test_keys = test_keys[:limit]

    if not test_keys:
        return {"run_id": run_id, "status": "error",
                "message": f"No test cases found at {test_data_key}"}

    test_cases = []
    for key in test_keys:
        tc = storage.read_json(key)
        if tc:
            tc["_key"] = key
            test_cases.append(tc)

    # Get agent ID
    agent_name = test_cases[0].get("agent", "your_target_agent") if test_cases else "your_target_agent"

    logger.info(f"[{run_id}] Looking up agent: {agent_name}")
    agent_id = _get_agent_id(instance_url, token, agent_name)
    if not agent_id:
        return {"run_id": run_id, "status": "failed",
                "message": f"Agent not found: {agent_name}"}

    logger.info(f"[{run_id}] Agent ID: {agent_id}, evaluating {len(test_cases)} test cases")

    start_time = datetime.utcnow()
    results = []

    for i, tc in enumerate(test_cases):
        test_name = tc["_key"].split("/")[-1].replace(".json", "")
        question = tc.get("starting_sentence", "")

        logger.info(f"[{run_id}] Test {i+1}/{len(test_cases)}: {test_name}")
        storage.save_run_status(run_id, {
            "run_id": run_id, "status": "running", "step": "evaluate",
            "progress": f"{i+1}/{len(test_cases)}",
            "current_test": test_name,
        })

        try:
            # Send question to agent
            thread_id, wxo_run_id = _send_message(instance_url, token, agent_id, question)

            if not thread_id or not wxo_run_id:
                raise RuntimeError(f"Missing thread_id={thread_id} or run_id={wxo_run_id} from runs API")

            # Wait for the run to complete
            run_status = _wait_for_run(instance_url, token, wxo_run_id)
            if run_status.get("status", "").lower() != "completed":
                raise RuntimeError(f"Run {wxo_run_id} ended with status: {run_status.get('status')}")

            # Brief pause for message propagation
            time.sleep(1)

            # Get response messages
            messages = _get_messages(instance_url, token, thread_id)

            # Save messages to S3
            storage.write_json(
                f"eval_results/{run_id}/messages/{test_name}.messages.json",
                messages,
            )

            results.append({
                "test_name": test_name,
                "status": "completed",
                "message_count": len(messages),
                "thread_id": thread_id,
            })

        except Exception as e:
            logger.error(f"[{run_id}] Test {test_name} failed: {e}")
            results.append({
                "test_name": test_name,
                "status": "failed",
                "error": str(e),
            })

    duration = (datetime.utcnow() - start_time).total_seconds()
    completed = sum(1 for r in results if r["status"] == "completed")
    failed = sum(1 for r in results if r["status"] == "failed")

    storage.save_run_status(run_id, {
        "run_id": run_id,
        "status": "completed",
        "step": "evaluate",
        "test_count": len(test_cases),
        "completed": completed,
        "failed": failed,
        "duration": duration,
        "completed_at": datetime.utcnow().isoformat(),
    })

    return {
        "run_id": run_id,
        "status": "completed",
        "test_count": len(test_cases),
        "completed": completed,
        "failed": failed,
        "duration": round(duration, 1),
        "message": f"Evaluation completed: {completed}/{len(test_cases)} tests in {duration:.1f}s",
    }
