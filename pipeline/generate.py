"""
Generate Pipeline Module
=========================
Generates ADK-compatible test cases from Excel input.
Extracted from commcloud_generate.py, adapted for S3 I/O.
"""

import json
import uuid
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def call_gateway_llm(prompt: str, model_id: str, token: str, instance_url: str,
                     system_prompt: str = None) -> str:
    """Call LLM via Orchestrate Gateway."""
    import requests

    instance_url = instance_url.rstrip("/")
    chat_url = f"{instance_url}/v1/orchestrate/gateway/model/chat/completions"

    x_gateway_config = {
        "strategy": {"mode": "single"},
        "targets": [{"provider": "watsonx", "api_key": "gateway",
                      "override_params": {"model": model_id}}]
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "x-request-id": str(uuid.uuid4()),
        "x-gateway-config": json.dumps(x_gateway_config, separators=(",", ":")),
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": f"watsonx/{model_id}", "messages": messages, "temperature": 0.0}

    response = requests.post(chat_url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()

    result = response.json()
    choices = result.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    raise RuntimeError(f"Unexpected response: {result}")


def extract_keywords(expected_answer: str, model_id: str, token: str,
                     instance_url: str) -> List[str]:
    """Use LLM to extract key terms from an expected answer."""
    prompt = (
        "Extract 3-6 key terms or short phrases from this answer that are essential "
        "for evaluating correctness.\nFocus on: specific numbers, dates, names, actions, "
        "or unique terms that MUST appear in a correct response.\n\n"
        f"ANSWER:\n{expected_answer}\n\n"
        'Return ONLY a JSON array of strings. Example: ["31 days", "enrollment", "portal.com"]\n\n'
        "JSON array:"
    )
    try:
        response = call_gateway_llm(prompt, model_id, token, instance_url,
                                    "You extract keywords. Respond only with a JSON array.")
        response = response.strip()
        if response.startswith("```"):
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        keywords = json.loads(response)
        if isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
            return keywords[:6]
    except Exception as e:
        logger.warning(f"LLM keyword extraction failed: {e}")

    # Fallback: simple extraction
    words = expected_answer.split()
    return list(set(w.strip('.,!?()[]"\'') for w in words if len(w) > 4))[:4]


def generate_story(question: str, model_id: str, token: str,
                   instance_url: str) -> str:
    """Use LLM to generate a brief scenario description."""
    prompt = (
        "Write a very brief (1 sentence, max 20 words) scenario description "
        f"for this question.\nDescribe WHO is asking and WHY.\n\n"
        f"QUESTION: {question}\n\nBrief scenario:"
    )
    try:
        response = call_gateway_llm(prompt, model_id, token, instance_url,
                                    "Write brief scenarios. No quotes.")
        story = response.strip().strip('"').strip("'")
        return story[:150] if len(story) > 150 else story
    except Exception:
        return f"User asking: {question[:50]}..."


def create_test_case(question: str, expected_answer: str, agent_name: str,
                     keywords: List[str], story: str, tool_name: str = None) -> Dict[str, Any]:
    """Create an ADK-compatible test case dict.

    Matches commcloud_generate.py create_test_case() exactly.
    """
    test_case = {
        "agent": agent_name,
        "story": story,
        "starting_sentence": question,
    }

    if tool_name:
        # Include tool call expectation — matches ADK format
        tool_call_name = f"{tool_name}-1"
        test_case["goals"] = {tool_call_name: ["summarize"]}
        test_case["goal_details"] = [
            {"type": "tool_call", "name": tool_call_name, "tool_name": tool_name, "args": {"query": question}},
            {"name": "summarize", "type": "text", "response": expected_answer, "keywords": keywords},
        ]
    else:
        # Response-only evaluation (no tool call expectation)
        test_case["goals"] = {"summarize": []}
        test_case["goal_details"] = [
            {"name": "summarize", "type": "text", "response": expected_answer, "keywords": keywords},
        ]

    return test_case


def generate_from_excel(excel_key: str, agent_name: str, run_id: str,
                        model_id: str = "meta-llama/llama-3-3-70b-instruct",
                        token: str = None, instance_url: str = None,
                        tool_name: str = None, limit: int = None,
                        skip_llm: bool = False) -> Dict[str, Any]:
    """Generate test cases from an Excel file in S3.

    Args:
        excel_key: S3 key or local path to Excel file
        agent_name: WxO agent name
        run_id: Unique run identifier
        model_id: LLM model for keyword extraction
        token: WxO auth token
        instance_url: WxO instance URL
        tool_name: Optional tool name for tool-call expectations
        limit: Max number of test cases
        skip_llm: Skip LLM calls (use simple keywords)

    Returns:
        {run_id, test_count, test_keys, preview}
    """
    import storage

    # Read Excel
    df = storage.read_excel_df(excel_key)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Normalize column names
    alt_map = {"query": "question", "answer": "expected_answer",
               "expected": "expected_answer", "response": "expected_answer"}
    for old, new in alt_map.items():
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})

    if "question" not in df.columns or "expected_answer" not in df.columns:
        raise ValueError(f"Excel must have 'Question' and 'Expected Answer' columns. Found: {df.columns.tolist()}")

    import pandas as pd
    df = df.dropna(subset=["question", "expected_answer"])
    if limit:
        df = df.head(limit)

    test_keys = []
    preview = []

    for idx, row in df.iterrows():
        num = len(test_keys) + 1
        question = str(row["question"]).strip()
        expected = str(row["expected_answer"]).strip()

        if skip_llm or not token or not instance_url:
            keywords = list(set(w.strip('.,!?()[]"\'') for w in expected.split() if len(w) > 4))[:4]
            story = "User asks a single question."
        else:
            story = generate_story(question, model_id, token, instance_url)
            keywords = extract_keywords(expected, model_id, token, instance_url)

        test_case = create_test_case(question, expected, agent_name, keywords, story, tool_name)

        key = f"test_data/{run_id}/test_{num:03d}.json"
        storage.write_json(key, test_case)
        test_keys.append(key)

        if num <= 3:
            preview.append({"test": f"test_{num:03d}", "question": question[:80],
                           "keywords": keywords})

    return {
        "run_id": run_id,
        "test_count": len(test_keys),
        "test_keys": test_keys,
        "preview": preview,
    }
