"""
Analyze Pipeline Module
========================
Matches tools, runs LLM-as-Judge evaluation via Orchestrate Gateway.
Adapted from commcloud_eval.py's LLM Judge approach for S3/Lambda.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool Call Extraction (with sub-agent filtering)
# ---------------------------------------------------------------------------

def _collect_responded_tool_names(messages: List[Dict]) -> set:
    """Return tool names that have a corresponding tool_response.

    Matches tool_call -> tool_response by tool_call_id, then returns
    the tool names that got a response.
    """
    call_id_to_name = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = msg.get("message", msg) if "message" in msg else msg
        if not isinstance(m, dict) or m.get("type") != "tool_call":
            continue
        call_id = m.get("id") or m.get("tool_call_id") or ""
        name = m.get("name") or ""
        if call_id and name:
            call_id_to_name[call_id] = name

    responded_ids = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = msg.get("message", msg) if "message" in msg else msg
        if not isinstance(m, dict) or m.get("type") != "tool_response":
            continue
        call_id = m.get("tool_call_id") or ""
        if call_id:
            responded_ids.add(call_id)

    names = set()
    for call_id, name in call_id_to_name.items():
        if call_id in responded_ids:
            names.add(name.strip())

    # Fallback: also check for name directly on tool_response (old format)
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = msg.get("message", msg) if "message" in msg else msg
        if isinstance(m, dict) and m.get("type") == "tool_response" and m.get("name"):
            names.add(str(m["name"]).strip())

    return names


def _parse_tool_call(msg: Dict) -> Optional[Dict]:
    """Extract {tool_name, args} from a message."""
    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "tool_call":
        return None

    tool_name = msg.get("name")

    if not tool_name:
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            fn = tool_calls[0].get("function", {}) if isinstance(tool_calls[0], dict) else {}
            tool_name = fn.get("name") if isinstance(fn, dict) else None

    if not tool_name:
        content = msg.get("content")
        if isinstance(content, str) and content.strip().startswith("{"):
            try:
                parsed = json.loads(content)
                tool_name = parsed.get("name") if isinstance(parsed, dict) else None
            except Exception:
                pass

    return {"tool_name": str(tool_name)} if tool_name else None


def extract_actual_tool_calls(messages: List[Dict]) -> List[Dict]:
    """Extract tool calls that have a response. Deduplicates by tool name."""
    responded = _collect_responded_tool_names(messages)
    calls = []
    seen_names: set = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = msg.get("message", msg) if "message" in msg else msg
        parsed = _parse_tool_call(m)
        if parsed and parsed["tool_name"] in responded:
            name = parsed["tool_name"]
            if name not in seen_names:
                seen_names.add(name)
                calls.append(parsed)
    return calls


def extract_text_responses(messages: List[Dict]) -> List[str]:
    """Extract assistant text messages."""
    texts = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = msg.get("message", msg) if "message" in msg else msg
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant" and m.get("type") == "text":
            content = m.get("content", "")
            if content and isinstance(content, str):
                texts.append(content)
    return texts


# ---------------------------------------------------------------------------
# Tool Matching
# ---------------------------------------------------------------------------

def match_tool_calls(goal_details: List[Dict], actual_calls: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Match expected tools against actual tools by name only."""
    expected_tools = [
        d for d in goal_details
        if isinstance(d, dict) and d.get("type") == "tool_call"
    ]

    actual_names = [c["tool_name"] for c in actual_calls]
    remaining = list(actual_names)

    results = []
    for et in expected_tools:
        exp_name = et.get("name", et.get("tool_name", ""))
        if exp_name in remaining:
            remaining.remove(exp_name)
            results.append({"expected": exp_name, "matched": True, "actual": exp_name, "type": "name_only"})
        else:
            results.append({"expected": exp_name, "matched": False, "actual": None, "type": "missing"})

    extra = [{"tool_name": n} for n in remaining]
    return results, extra


def match_tool_calls_with_verdicts(goal_details: List[Dict],
                                    actual_calls: List[Dict]) -> Dict[str, Any]:
    """Enhanced tool matching producing ADK-style per-tool verdicts.

    Verdicts:
      - "correct" — tool name matches expected
      - "missing tool call" — expected tool never called
      - "extra tool call" — unexpected tool not in expected list
    """
    expected_tools = [
        d for d in goal_details
        if isinstance(d, dict) and d.get("type") == "tool_call"
    ]
    actual_names = [c["tool_name"] for c in actual_calls]
    remaining = list(actual_names)

    verdicts = []
    for et in expected_tools:
        exp_name = et.get("name", et.get("tool_name", ""))
        if exp_name in remaining:
            remaining.remove(exp_name)
            verdicts.append({
                "expected": exp_name, "actual": exp_name,
                "verdict": "correct", "matched": True,
            })
        else:
            # Check if a different tool was called instead
            verdicts.append({
                "expected": exp_name, "actual": None,
                "verdict": "missing tool call", "matched": False,
            })

    # Remaining actuals are extra/unexpected
    extra = [{"tool_name": n, "verdict": "extra tool call"} for n in remaining]

    counts = {
        "correct": sum(1 for v in verdicts if v["verdict"] == "correct"),
        "missing": sum(1 for v in verdicts if v["verdict"] == "missing tool call"),
        "extra": len(extra),
    }

    return {"tool_verdicts": verdicts, "extra_calls": extra, "counts": counts}


# ---------------------------------------------------------------------------
# LLM-as-Judge via Orchestrate Gateway
# (Exact same approach as commcloud_eval.py cmd_judge)
# ---------------------------------------------------------------------------

def _call_gateway_llm(prompt: str, model_id: str, token: str,
                      instance_url: str) -> str:
    """Call LLM via Orchestrate Gateway — same as commcloud_eval.py."""
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

    payload = {
        "model": f"watsonx/{model_id}",
        "messages": [
            {"role": "system", "content": "You are an evaluation judge. Respond only with valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
    }

    response = requests.post(chat_url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()

    result = response.json()
    choices = result.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "")
    raise RuntimeError(f"Unexpected response: {result}")


def llm_judge_evaluate(question: str, expected_answer: str,
                       agent_response: str, model_id: str,
                       token: str, instance_url: str) -> Dict[str, Any]:
    """Use LLM as a Judge — exact same prompt as commcloud_eval.py."""
    judge_prompt = f"""You are an expert evaluation judge for a health benefits Q&A system.
Your task is to evaluate if an AI agent's response correctly answers a question about employee benefits.

EVALUATION CRITERIA:
1. CORRECTNESS: Does the response contain factually correct information matching the expected answer?
2. COMPLETENESS: Does it cover the key points from the expected answer?
3. RELEVANCE: Does it directly address the question asked?
4. NO HALLUCINATION: Does it avoid making up false information not in the expected answer?

IMPORTANT - NUMERIC EQUIVALENCES:
When comparing numbers and time periods, recognize these as EQUIVALENT:
- "15 days" = "2 weeks and 1 day" (7+7+1=15)
- "31 days" = "about one month" = "approximately 1 month"
- "10 days" = "2 weeks" (business days) OR "10 calendar days"
- Different numeric expressions of the SAME value are CORRECT, not partially correct

QUESTION:
{question}

EXPECTED ANSWER (Ground Truth):
{expected_answer}

AGENT'S ACTUAL RESPONSE:
{agent_response}

INSTRUCTIONS:
- Compare the AGENT'S RESPONSE against the EXPECTED ANSWER
- The agent doesn't need to match word-for-word, but must convey the same key information
- NUMERIC VALUES: If the numbers are mathematically equivalent (e.g., "15 days" vs "2 weeks + 1 day"), mark as CORRECT
- If the agent provides additional helpful context beyond the expected answer, that's OK
- If the agent contradicts or omits key information from the expected answer, mark as incorrect

Respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{{
    "verdict": "CORRECT" or "PARTIALLY_CORRECT" or "INCORRECT",
    "score": <float between 0.0 and 1.0>,
    "correctness": <float 0-1>,
    "completeness": <float 0-1>,
    "relevance": <float 0-1>,
    "reasoning": "<brief explanation of your evaluation>"
}}
"""

    result_text = ""
    try:
        result_text = _call_gateway_llm(judge_prompt, model_id, token, instance_url)

        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]

        json_start = result_text.find("{")
        json_end = result_text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            result_text = result_text[json_start:json_end]

        result = json.loads(result_text)

        result.setdefault("verdict", "INCORRECT")
        result.setdefault("score", 0.0)
        result.setdefault("reasoning", "No reasoning provided")

        # Pass criteria: CORRECT, or PARTIALLY_CORRECT with score >= 0.7
        verdict = result["verdict"]
        score = float(result["score"])
        passed = (verdict == "CORRECT") or (verdict == "PARTIALLY_CORRECT" and score >= 0.7)

        return {
            "success": True,
            "passed": passed,
            "verdict": verdict,
            "score": score,
            "correctness": float(result.get("correctness", score)),
            "completeness": float(result.get("completeness", score)),
            "relevance": float(result.get("relevance", score)),
            "reasoning": result["reasoning"],
        }

    except json.JSONDecodeError as e:
        return {
            "success": False, "passed": False, "verdict": "ERROR", "score": 0.0,
            "reasoning": f"Failed to parse LLM response: {e}",
            "raw_response": result_text[:500] if result_text else None,
        }
    except Exception as e:
        return {
            "success": False, "passed": False, "verdict": "ERROR", "score": 0.0,
            "reasoning": f"LLM Judge error: {e}",
        }


# ---------------------------------------------------------------------------
# Root Cause Analysis (RCA) via Orchestrate Gateway
# ---------------------------------------------------------------------------

def _summarize_messages(messages: List[Dict], max_len: int = 800) -> str:
    """Build a short summary of the conversation for RCA context."""
    parts = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = msg.get("message", msg) if "message" in msg else msg
        if not isinstance(m, dict):
            continue
        role = m.get("role", m.get("type", ""))
        content = m.get("content", "")
        if isinstance(content, str) and content.strip():
            parts.append(f"[{role}] {content[:200]}")
    summary = "\n".join(parts)
    return summary[:max_len] if len(summary) > max_len else summary


def _parse_json_response(text: str) -> Dict[str, Any]:
    """Extract JSON object from LLM response text (handles markdown fences)."""
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    json_start = text.find("{")
    json_end = text.rfind("}") + 1
    if json_start >= 0 and json_end > json_start:
        text = text[json_start:json_end]

    return json.loads(text)


def generate_tool_rca(expected_tool: str, actual_tool: Optional[str],
                      verdict: str, question: str,
                      messages_summary: str,
                      model_id: str, token: str,
                      instance_url: str) -> Dict[str, Any]:
    """Generate LLM-based root cause analysis for any tool call verdict.

    For failures: explains what went wrong and how to fix it.
    For correct calls: provides optimization recommendations.
    """
    if verdict == "correct":
        prompt = f"""You are an expert at analyzing AI agent tool call behavior.

CONTEXT:
- Question: "{question}"
- Tool called: "{expected_tool}" (CORRECT — matched expected)

CONVERSATION SUMMARY:
{messages_summary}

The agent correctly selected this tool. Analyze the tool usage and provide
recommendations for improvement. Consider:
1. Was the tool called at the right point in the conversation?
2. Could the agent have provided better parameters?
3. Are there any optimization opportunities?
4. Any potential edge cases where this tool selection might fail?

Respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{{
    "reason_tag": "correct",
    "severity": "info",
    "root_cause": "<1-2 sentence analysis of why this tool was correctly selected>",
    "suggestion": "<1-2 sentence optimization recommendation for the agent builder>"
}}"""
    else:
        prompt = f"""You are an expert at analyzing AI agent tool call failures.

CONTEXT:
- Question: "{question}"
- Expected tool: "{expected_tool}"
- Actual tool called: "{actual_tool or 'NONE - tool was never called'}"
- Failure type: {verdict}

CONVERSATION SUMMARY:
{messages_summary}

Analyze why this tool call failure occurred. Consider:
1. Did the agent misunderstand the user's intent?
2. Was the agent missing routing rules for this tool?
3. Did the agent call a related but wrong tool?
4. Was there an error earlier in the conversation that cascaded?

Respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{{
    "reason_tag": "{verdict}",
    "severity": "high" or "medium" or "low",
    "root_cause": "<1-2 sentence explanation of why the agent made this mistake>",
    "suggestion": "<1-2 sentence actionable fix for the agent builder>"
}}"""

    try:
        text = _call_gateway_llm(prompt, model_id, token, instance_url)
        result = _parse_json_response(text)

        # Ensure required fields
        result.setdefault("reason_tag", verdict)
        result.setdefault("severity", "unknown")
        result.setdefault("root_cause", "")
        result.setdefault("suggestion", "")
        return result

    except Exception as e:
        logger.warning(f"RCA generation failed for {expected_tool}: {e}")
        return {
            "reason_tag": verdict,
            "severity": "unknown",
            "root_cause": f"RCA generation failed: {e}",
            "suggestion": "",
        }


# ---------------------------------------------------------------------------
# Single Case Analysis
# ---------------------------------------------------------------------------

def analyze_single_case(enriched: Dict, messages: List[Dict],
                        token: str = None, instance_url: str = None,
                        model: str = "meta-llama/llama-3-3-70b-instruct",
                        skip_judge: bool = False,
                        skip_rca: bool = False) -> Dict[str, Any]:
    """Analyze a single test case.

    Uses:
    - Tool matching with ADK-style verdicts
    - LLM-based RCA for tool call failures (when skip_rca=False)
    - LLM-as-Judge via Orchestrate Gateway (from commcloud_eval.py)
    """
    question = enriched.get("starting_sentence", enriched.get("story", "?"))
    goal_details = enriched.get("goal_details", [])

    # Get expected answer from goal_details
    expected_answer = ""
    for d in goal_details:
        if isinstance(d, dict) and (d.get("type") == "text" or d.get("name") == "summarize"):
            expected_answer = d.get("response", "")
            break

    actual_calls = extract_actual_tool_calls(messages)
    actual_texts = extract_text_responses(messages)

    # Legacy tool matching (backward compat)
    tool_results, extra_calls_legacy = match_tool_calls(goal_details, actual_calls)
    correct = sum(1 for r in tool_results if r["matched"])
    missing = sum(1 for r in tool_results if not r["matched"])
    tool_pass = correct == len(tool_results) and len(tool_results) > 0

    # Enhanced tool matching with verdicts
    verdicts_result = match_tool_calls_with_verdicts(goal_details, actual_calls)

    # Generate RCA for ALL tool verdicts (failures + correct)
    # Failures get root cause analysis, correct calls get optimization recommendations
    rca_results = []
    if not skip_rca and token and instance_url:
        messages_summary = _summarize_messages(messages)
        for v in verdicts_result["tool_verdicts"]:
            rca = generate_tool_rca(
                expected_tool=v["expected"],
                actual_tool=v.get("actual"),
                verdict=v["verdict"],
                question=question,
                messages_summary=messages_summary,
                model_id=model, token=token, instance_url=instance_url,
            )
            v["rca"] = rca
            rca_results.append(rca)

        # Also generate RCA for extra tool calls
        for ec in verdicts_result["extra_calls"]:
            rca = generate_tool_rca(
                expected_tool="(none expected)",
                actual_tool=ec["tool_name"],
                verdict="extra tool call",
                question=question,
                messages_summary=messages_summary,
                model_id=model, token=token, instance_url=instance_url,
            )
            ec["rca"] = rca
            rca_results.append(rca)

    # LLM-as-Judge evaluation
    agent_response = " ".join(actual_texts).strip()
    llm_judge = None
    judge_passed = False

    if not skip_judge and token and instance_url and expected_answer and agent_response:
        llm_judge = llm_judge_evaluate(
            question=question,
            expected_answer=expected_answer,
            agent_response=agent_response,
            model_id=model,
            token=token,
            instance_url=instance_url,
        )
        judge_passed = llm_judge.get("passed", False)
    elif not agent_response:
        llm_judge = {
            "success": False, "passed": False, "verdict": "NO_RESPONSE",
            "score": 0.0, "reasoning": "Agent did not produce a text response.",
        }
    elif not expected_answer:
        # No expected answer — skip judge, auto-pass text
        judge_passed = True
        llm_judge = {
            "success": True, "passed": True, "verdict": "SKIPPED",
            "score": 1.0, "reasoning": "No expected answer provided.",
        }

    # Journey = tools pass AND judge passes
    journey_pass = tool_pass and judge_passed

    return {
        # Existing fields (backward compat)
        "question": question,
        "journey_success": journey_pass,
        "tool_matches": tool_results,
        "extra_calls": [{"tool_name": ec["tool_name"]} for ec in verdicts_result["extra_calls"]],
        "llm_judge": llm_judge,
        "agent_response": agent_response[:500] if agent_response else "",
        "expected_tool_count": len(tool_results),
        "actual_tool_count": len(actual_calls),
        "correct_tool_count": correct,
        "missing_tool_count": missing,
        "extra_tool_count": verdicts_result["counts"]["extra"],
        # New RCA fields
        "tool_verdicts": verdicts_result["tool_verdicts"],
        "rca": rca_results,
    }


# ---------------------------------------------------------------------------
# Full Run Analysis
# ---------------------------------------------------------------------------

def analyze_run(run_id: str, skip_judge: bool = False,
                skip_rca: bool = False,
                token: str = None, instance_url: str = None,
                model: str = "meta-llama/llama-3-3-70b-instruct") -> Dict[str, Any]:
    """Analyze all test cases for a given run.

    Uses LLM-as-Judge via Orchestrate Gateway (same as commcloud_eval.py).
    When skip_rca=False (default), generates per-tool-call RCA for failures.
    """
    import storage

    # Load enriched cases
    enriched_keys = storage.list_keys(f"enriched/{run_id}/", suffix=".json")
    if not enriched_keys:
        return {"error": f"No enriched test cases found for run {run_id}"}

    message_prefix = f"eval_results/{run_id}/messages/"

    cases = []
    start_time = datetime.utcnow()

    for ekey in enriched_keys:
        test_name = ekey.split("/")[-1].replace(".json", "")
        enriched = storage.read_json(ekey, {})
        if not enriched:
            continue

        msg_key = f"{message_prefix}{test_name}.messages.json"
        messages = storage.read_json(msg_key, [])
        if not isinstance(messages, list):
            messages = []

        result = analyze_single_case(
            enriched, messages, token=token, instance_url=instance_url,
            model=model, skip_judge=skip_judge, skip_rca=skip_rca,
        )
        result["test_name"] = test_name
        cases.append(result)

    duration = (datetime.utcnow() - start_time).total_seconds()

    # Aggregate metrics
    n = len(cases)
    journey_ok = sum(1 for c in cases if c["journey_success"])
    total_expected = sum(c["expected_tool_count"] for c in cases)
    total_correct = sum(c["correct_tool_count"] for c in cases)
    total_actual = sum(c["actual_tool_count"] for c in cases)

    # LLM Judge aggregation (matches commcloud_eval.py cmd_judge)
    judge_results = [c.get("llm_judge", {}) for c in cases if c.get("llm_judge")]
    judge_passed = sum(1 for j in judge_results if j.get("passed"))
    judge_correct = sum(1 for j in judge_results if j.get("verdict") == "CORRECT")
    judge_partial = sum(1 for j in judge_results if j.get("verdict") == "PARTIALLY_CORRECT")
    judge_incorrect = sum(1 for j in judge_results if j.get("verdict") == "INCORRECT")
    avg_score = (sum(j.get("score", 0) for j in judge_results) / len(judge_results)) if judge_results else 0

    # RCA aggregation
    all_rca = []
    for c in cases:
        all_rca.extend(c.get("rca", []))

    reason_counts = {}
    for r in all_rca:
        tag = r.get("reason_tag", "unknown")
        reason_counts[tag] = reason_counts.get(tag, 0) + 1

    rca_failures = [r for r in all_rca if r.get("reason_tag") != "correct"]
    rca_recommendations = [r for r in all_rca if r.get("reason_tag") == "correct"]

    rca_summary = {
        "total_analyzed": len(all_rca),
        "total_issues": len(rca_failures),
        "total_recommendations": len(rca_recommendations),
        "missing_tool_calls": sum(1 for r in all_rca if "missing" in r.get("reason_tag", "")),
        "incorrect_tool_calls": sum(1 for r in all_rca if "incorrect" in r.get("reason_tag", "")),
        "extra_tool_calls": sum(1 for r in all_rca if "extra" in r.get("reason_tag", "")),
        "top_reason_tags": dict(sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:5]),
    }

    report = {
        "summary": {
            "total_cases": n,
            "journey_success_rate": (journey_ok / n * 100) if n else 0,
            "tool_recall": (total_correct / total_expected * 100) if total_expected else 0,
            "tool_precision": (total_correct / total_actual * 100) if total_actual else 0,
            "llm_judge_pass_rate": (judge_passed / len(judge_results) * 100) if judge_results else 0,
            "llm_judge_avg_score": round(avg_score, 2),
            "llm_judge_correct": judge_correct,
            "llm_judge_partial": judge_partial,
            "llm_judge_incorrect": judge_incorrect,
            "rca_summary": rca_summary,
            "analysis_duration": round(duration, 1),
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "cases": cases,
    }

    # Save report
    storage.write_json(f"analyze/{run_id}/report.json", report)

    # Save HTML
    html = render_html(report)
    storage.write_text(f"analyze/{run_id}/report.html", html, content_type="text/html")

    return report


# ---------------------------------------------------------------------------
# HTML Report
# ---------------------------------------------------------------------------

def render_html(report: Dict) -> str:
    """Render a standalone HTML report with RCA details."""
    s = report["summary"]
    cases = report["cases"]
    rca_sum = s.get("rca_summary", {})

    def color(val):
        if val >= 90:
            return "#22c55e"
        if val >= 70:
            return "#f59e0b"
        return "#ef4444"

    def verdict_color(verdict):
        if verdict == "correct":
            return "pass"
        if "missing" in verdict:
            return "fail"
        if "extra" in verdict:
            return "warn"
        return "fail"

    def severity_color(sev):
        if sev == "high":
            return "#ef4444"
        if sev == "medium":
            return "#f59e0b"
        if sev == "info":
            return "#3b82f6"
        return "#64748b"

    # RCA summary cards
    rca_card_html = ""
    if rca_sum.get("total_analyzed", 0) > 0:
        issues = rca_sum.get("total_issues", 0)
        recs = rca_sum.get("total_recommendations", 0)
        issue_color = "#ef4444" if issues > 0 else "#22c55e"
        rca_card_html = f'''<div class="card"><div class="value" style="color:{issue_color}">{issues}</div><div class="label">RCA Issues</div></div>'''
        rca_card_html += f'''<div class="card"><div class="value" style="color:#3b82f6">{recs}</div><div class="label">Recommendations</div></div>'''

    case_html = ""
    for c in cases:
        status = "pass" if c["journey_success"] else "fail"
        label = "PASS" if c["journey_success"] else "FAIL"

        # Tool verdicts table (enhanced with RCA)
        tool_verdicts = c.get("tool_verdicts", [])
        extra_calls = c.get("extra_calls", [])
        has_verdicts = bool(tool_verdicts) or bool(extra_calls)

        verdict_rows = ""
        rca_details = ""
        if has_verdicts:
            for tv in tool_verdicts:
                cls = verdict_color(tv["verdict"])
                actual = tv.get("actual") or "-"
                verdict_label = tv["verdict"]
                if tv["verdict"] == "correct":
                    verdict_label = "&#10003; correct"
                elif "missing" in tv["verdict"]:
                    verdict_label = "&#10007; missing tool call"
                verdict_rows += f'<tr class="{cls}"><td>{tv["expected"]}</td><td>{actual}</td><td>{verdict_label}</td></tr>'

                # RCA detail for this verdict
                rca = tv.get("rca")
                if rca:
                    sev = rca.get("severity", "unknown")
                    css_cls = "recommendation" if tv["verdict"] == "correct" else "failure"
                    label = "Analysis" if tv["verdict"] == "correct" else "Root Cause"
                    rca_details += f'''<div class="rca-item {css_cls}" style="border-left:3px solid {severity_color(sev)}">
                        <strong>{tv["expected"]}</strong> — <span style="color:{severity_color(sev)}">{sev.upper()}</span><br>
                        <strong>{label}:</strong> {rca.get("root_cause", "")}<br>
                        <strong>Suggestion:</strong> {rca.get("suggestion", "")}
                    </div>'''

            for ec in extra_calls:
                verdict_rows += f'<tr class="warn"><td>-</td><td>{ec["tool_name"]}</td><td>&#9888; extra tool call</td></tr>'
                rca = ec.get("rca")
                if rca:
                    sev = rca.get("severity", "unknown")
                    rca_details += f'''<div class="rca-item" style="border-left:3px solid {severity_color(sev)}">
                        <strong>{ec["tool_name"]}</strong> (extra) — <span style="color:{severity_color(sev)}">{sev.upper()}</span><br>
                        <strong>Root Cause:</strong> {rca.get("root_cause", "")}<br>
                        <strong>Suggestion:</strong> {rca.get("suggestion", "")}
                    </div>'''
        else:
            # Fallback to legacy tool_matches
            for tm in c.get("tool_matches", []):
                cls = "pass" if tm["matched"] else "fail"
                actual = tm["actual"] or "-"
                result = f"&#10003; {tm['type']}" if tm["matched"] else "&#10007; missing"
                verdict_rows += f'<tr class="{cls}"><td>{tm["expected"]}</td><td>{actual}</td><td>{result}</td></tr>'
            for ec in c.get("extra_calls", []):
                verdict_rows += f'<tr class="warn"><td>-</td><td>{ec["tool_name"]}</td><td>extra</td></tr>'

        judge = c.get("llm_judge", {})
        verdict = judge.get("verdict", "N/A")
        score = judge.get("score", 0)
        reasoning = judge.get("reasoning", "")[:200]
        judge_cls = "pass" if judge.get("passed") else "fail"
        judge_html = f'''<div class="text-match {judge_cls}">
            <strong>Verdict:</strong> {verdict} (score: {score:.2f})<br>
            <strong>Reasoning:</strong> {reasoning}
        </div>'''

        response = c.get("agent_response", "")[:300]
        response_html = f'<div style="margin:0.5rem 0;padding:0.5rem;background:#f1f5f9;border-radius:4px;font-size:0.85rem;">{response}</div>' if response else ""

        # RCA section (always shown when RCA data exists)
        rca_section = ""
        if rca_details:
            rca_section = f'''<h4>Root Cause Analysis &amp; Recommendations</h4>
                <div class="rca-container">{rca_details}</div>'''

        case_html += f'''
        <details {"open" if not c["journey_success"] else ""}>
            <summary class="test-header {status}">
                <span class="status">{label}</span>
                <span class="name">{c.get("test_name", "?")}: {c["question"][:60]}</span>
                <span class="badges">Tools: {c["correct_tool_count"]}/{c["expected_tool_count"]} | Judge: {verdict} ({score:.1f})</span>
            </summary>
            <div class="test-body">
                <h4>Agent Response</h4>
                {response_html}
                <h4>LLM Judge Evaluation</h4>
                {judge_html}
                <h4>Tool Call Verdicts</h4>
                <table><tr><th>Expected</th><th>Actual</th><th>Verdict</th></tr>{verdict_rows}</table>
                {rca_section}
            </div>
        </details>'''

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Sample Evaluation Report</title>
<style>
:root {{ --pass: #22c55e; --fail: #ef4444; --warn: #f59e0b; --bg: #f8fafc; --card: #fff; --text: #1e293b; --muted: #64748b; --border: #e2e8f0; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); padding: 2rem; max-width: 1200px; margin: 0 auto; }}
header {{ text-align: center; margin-bottom: 2rem; }}
header h1 {{ font-size: 1.5rem; }} header p {{ color: var(--muted); font-size: 0.9rem; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
.card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.2rem; text-align: center; }}
.card .value {{ font-size: 1.8rem; font-weight: bold; }} .card .label {{ color: var(--muted); font-size: 0.85rem; margin-top: 0.3rem; }}
details {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 0.5rem; }}
.test-header {{ padding: 1rem; cursor: pointer; display: flex; align-items: center; gap: 1rem; }}
.test-header.pass .status {{ background: var(--pass); color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: bold; }}
.test-header.fail .status {{ background: var(--fail); color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: bold; }}
.test-header .name {{ flex: 1; }} .test-header .badges {{ color: var(--muted); font-size: 0.85rem; }}
.test-body {{ padding: 0 1rem 1rem; }} .test-body h4 {{ margin: 1rem 0 0.5rem; color: var(--muted); font-size: 0.9rem; text-transform: uppercase; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 1rem; }}
th, td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid var(--border); }}
th {{ font-weight: 600; color: var(--muted); }}
tr.pass td:last-child {{ color: var(--pass); }} tr.fail td:last-child {{ color: var(--fail); }} tr.warn td:last-child {{ color: var(--warn); }}
.text-match {{ padding: 0.5rem; background: #f1f5f9; border-radius: 4px; font-size: 0.9rem; margin-bottom: 0.5rem; }}
.text-match.pass {{ border-left: 3px solid var(--pass); }} .text-match.fail {{ border-left: 3px solid var(--fail); }}
.rca-container {{ display: flex; flex-direction: column; gap: 0.5rem; }}
.rca-item {{ padding: 0.75rem; border-radius: 4px; font-size: 0.85rem; }}
.rca-item.failure {{ background: #fef2f2; }}
.rca-item.recommendation {{ background: #eff6ff; }}
footer {{ text-align: center; color: var(--muted); margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border); font-size: 0.8rem; }}
</style></head>
<body>
<header>
    <h1>Sample Agent Evaluation Report</h1>
    <p>Generated {s['timestamp']} | {s['total_cases']} test cases | {s['analysis_duration']}s</p>
</header>
<div class="cards">
    <div class="card"><div class="value" style="color:{color(s['journey_success_rate'])}">{s['journey_success_rate']:.0f}%</div><div class="label">Journey Success</div></div>
    <div class="card"><div class="value" style="color:{color(s['tool_recall'])}">{s['tool_recall']:.0f}%</div><div class="label">Tool Recall</div></div>
    <div class="card"><div class="value" style="color:{color(s['llm_judge_pass_rate'])}">{s['llm_judge_pass_rate']:.0f}%</div><div class="label">LLM Judge Pass Rate</div></div>
    <div class="card"><div class="value">{s['llm_judge_avg_score']:.2f}</div><div class="label">Avg Judge Score</div></div>
    {rca_card_html}
</div>
<h2 style="margin-bottom:1rem">Test Cases</h2>
{case_html}
<footer>Sample Eval Pipeline v2 | LLM Judge + RCA: Orchestrate Gateway</footer>
</body></html>'''
