"""
Enrich Pipeline Module
=======================
Discovers tool calls from evaluation trajectories and updates test case
expectations. Extracted from commcloud_enrich.py, adapted for S3 I/O.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _parse_tool_call_from_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract normalized tool call {tool_name, args} from one message entry."""
    if msg.get("type") != "tool_call":
        return None

    tool_name = None
    args_obj: Dict[str, Any] = {}

    # Format 1: Direct name + args on the message (WxO native format)
    tool_name = msg.get("name")
    if isinstance(msg.get("args"), dict):
        args_obj = msg["args"]

    # Format 2: tool_calls[].function.name (OpenAI format)
    if not tool_name:
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            first = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
            function_obj = first.get("function", {}) if isinstance(first, dict) else {}
            if isinstance(function_obj, dict):
                tool_name = function_obj.get("name")
                raw_args = function_obj.get("arguments")
                if isinstance(raw_args, str) and raw_args.strip():
                    try:
                        parsed = json.loads(raw_args)
                        if isinstance(parsed, dict):
                            args_obj = parsed
                    except Exception:
                        pass

    # Format 3: JSON in content field
    if not tool_name:
        content = msg.get("content")
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    tool_name = parsed.get("name") or tool_name
                    if isinstance(parsed.get("args"), dict):
                        args_obj = parsed["args"]
            except Exception:
                pass

    if not tool_name:
        return None
    return {"tool_name": str(tool_name), "args": args_obj if isinstance(args_obj, dict) else {}}


def _collect_responded_tool_names(rows: List[Dict[str, Any]]) -> set:
    """Return set of tool names that have a corresponding tool_response.

    Matches tool_call → tool_response by tool_call_id, then returns
    the tool names that got a response.
    """
    # Build map: tool_call_id → tool_name from tool_call messages
    call_id_to_name: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "tool_call":
            call_id = row.get("id") or row.get("tool_call_id") or ""
            name = row.get("name") or ""
            if call_id and name:
                call_id_to_name[call_id] = name

    # Find tool_call_ids that have a tool_response
    responded_ids: set = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "tool_response":
            call_id = row.get("tool_call_id") or ""
            if call_id:
                responded_ids.add(call_id)

    # Return tool names that have responses
    names: set = set()
    for call_id, name in call_id_to_name.items():
        if call_id in responded_ids:
            names.add(name.strip())

    # Fallback: also check for name directly on tool_response (old format)
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "tool_response":
            continue
        name = row.get("name")
        if name:
            names.add(str(name).strip())

    return names


def extract_tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract tool calls that have a response (filters sub-agent routing)."""
    responded_names = _collect_responded_tool_names(messages)
    extracted = []
    for row in messages:
        if not isinstance(row, dict):
            continue
        parsed = _parse_tool_call_from_message(row)
        if parsed and parsed["tool_name"] in responded_names:
            extracted.append(parsed)
    return extracted


def build_enriched_case(base_case: Dict[str, Any],
                        tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge discovered tool calls into goals/goal_details."""
    enriched = dict(base_case)

    original_goal_details = enriched.get("goal_details", [])
    if not isinstance(original_goal_details, list):
        original_goal_details = []

    text_details = [d for d in original_goal_details
                    if isinstance(d, dict) and d.get("type") == "text"]
    if not text_details:
        text_details = [d for d in original_goal_details if isinstance(d, dict)]

    goals: Dict[str, List[str]] = {}
    goal_details: List[Dict[str, Any]] = []

    # Deduplicate tool calls by name
    seen_tools: set = set()
    unique_tool_calls = []
    for tc in tool_calls:
        tn = str(tc.get("tool_name", "")).strip()
        if tn and tn not in seen_tools:
            seen_tools.add(tn)
            unique_tool_calls.append(tc)

    tool_goal_names = []
    for tc in unique_tool_calls:
        tool_name = str(tc.get("tool_name", "")).strip()
        if not tool_name:
            continue
        goal_name = tool_name
        tool_goal_names.append(goal_name)
        goal_details.append({
            "type": "tool_call",
            "name": goal_name,
            "tool_name": tool_name,
            "args": {},  # Name-only matching
        })

    # Chain tools -> summarize
    if tool_goal_names:
        for i, gn in enumerate(tool_goal_names):
            goals[gn] = [tool_goal_names[i + 1]] if i < len(tool_goal_names) - 1 else ["summarize"]
    else:
        goals["summarize"] = []

    for td in text_details:
        td_copy = dict(td)
        td_copy.setdefault("type", "text")
        td_copy.setdefault("name", "summarize")
        goal_details.append(td_copy)

    enriched["goals"] = goals
    enriched["goal_details"] = goal_details
    enriched["_enrich_meta"] = {
        "tool_calls_discovered": len(tool_goal_names),
        "tool_goal_names": tool_goal_names,
    }
    return enriched


def enrich_run(run_id: str, source_test_prefix: str = None) -> Dict[str, Any]:
    """Enrich all test cases for a given evaluation run.

    Args:
        run_id: The evaluation run ID
        source_test_prefix: S3 prefix for original test cases (auto-detected if None)

    Returns:
        {run_id, enriched_count, enriched_keys}
    """
    import storage

    # Find message files
    messages_prefix = f"eval_results/{run_id}/messages/"
    message_keys = storage.list_keys(messages_prefix, suffix=".messages.json")

    if not message_keys:
        return {"run_id": run_id, "enriched_count": 0, "error": "No message files found"}

    # Auto-detect source test prefix
    if not source_test_prefix:
        # Try to find test_data for this run
        run_status = storage.get_run_status(run_id)
        if run_status and "test_data_key" in run_status:
            source_test_prefix = run_status["test_data_key"]
        else:
            source_test_prefix = f"test_data/{run_id}/"

    enriched_keys = []

    for mkey in message_keys:
        # test_001.messages.json -> test_001
        fname = mkey.split("/")[-1]
        stem = fname.replace(".messages.json", "")

        # Load source test case
        source_key = f"{source_test_prefix.rstrip('/')}/{stem}.json"
        base_case = storage.read_json(source_key, {})
        if not base_case:
            logger.warning(f"Source test not found: {source_key}")
            continue

        # Load messages
        messages = storage.read_json(mkey, [])
        if not isinstance(messages, list):
            continue

        # Extract tool calls and enrich
        tool_calls = extract_tool_calls(messages)
        enriched = build_enriched_case(base_case, tool_calls)

        # Save enriched case
        enriched_key = f"enriched/{run_id}/{stem}.json"
        storage.write_json(enriched_key, enriched)
        enriched_keys.append(enriched_key)

        logger.info(f"Enriched {stem}: {len(tool_calls)} tool call(s)")

    return {
        "run_id": run_id,
        "enriched_count": len(enriched_keys),
        "enriched_keys": enriched_keys,
    }
