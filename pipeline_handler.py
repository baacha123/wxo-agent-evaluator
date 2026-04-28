"""
Pipeline Handler (Lambda)
==========================
Step Functions step dispatcher. Each invocation runs one pipeline step.

Called by Step Functions with:
  {
    "session_id": "sess_...",
    "run_id": "pipe_...",
    "config": { "agent_name": "...", "excel_key": "...", ... },
    "step": "generate | evaluate_one | enrich | analyze",
    "test_case": { ... }  // only for evaluate_one (Map state)
  }
"""

import json
import os
import logging
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Main entry point — dispatches to the correct pipeline step."""
    step = event.get("step", "")
    session_id = event.get("session_id", "")
    run_id = event.get("run_id", "")
    config = event.get("config", {})

    logger.info(f"[Pipeline] step={step} session={session_id} run={run_id}")

    try:
        if step == "generate":
            return handle_generate(event, config, run_id, session_id)
        elif step == "evaluate_one":
            return handle_evaluate_one(event, config, run_id)
        elif step == "enrich":
            return handle_enrich(event, run_id, session_id)
        elif step == "analyze":
            return handle_analyze(event, run_id, session_id)
        elif step == "redteam":
            return handle_redteam(event, run_id, session_id)
        elif step == "redteam_plan":
            return handle_redteam_plan(event, config, run_id, session_id)
        elif step == "redteam_run_one":
            return handle_redteam_run_one(event, config, run_id)
        elif step == "redteam_evaluate":
            return handle_redteam_evaluate(event, run_id, session_id)
        elif step == "update_status":
            return handle_update_status(event)
        else:
            raise ValueError(f"Unknown step: {step}")
    except Exception as e:
        logger.error(f"[Pipeline] Step {step} failed: {e}", exc_info=True)
        # Update session with error
        try:
            import session_store
            session_store.set_status(session_id, "failed", step=step, error=str(e))
        except Exception:
            pass
        raise


def handle_generate(event: dict, config: dict, run_id: str, session_id: str) -> dict:
    """Generate test cases from Excel."""
    import session_store
    from pipeline.generate import generate_from_excel
    from auth import get_wxo_credentials

    session_store.set_status(
        session_id, "running", step="generate",
        progress="Step 1/4: Generating test cases from Excel..."
    )

    token, instance_url = get_wxo_credentials()
    excel_key = config.get("excel_key", "uploads/questions.xlsx")
    agent_name = config.get("agent_name", "your_target_agent")
    limit = config.get("limit")
    model_id = config.get("model_id", "meta-llama/llama-3-3-70b-instruct")

    tool_name = config.get("tool_name")

    result = generate_from_excel(
        excel_key=excel_key,
        agent_name=agent_name,
        run_id=run_id,
        model_id=model_id,
        token=token,
        instance_url=instance_url,
        tool_name=tool_name if tool_name else None,
        limit=int(limit) if limit else None,
    )

    # Return test keys for the Map state
    test_keys = result.get("test_keys", [])

    session_store.set_status(
        session_id, "running", step="generate",
        progress=f"Step 1/4: Generated {len(test_keys)} test cases."
    )

    return {
        "session_id": session_id,
        "run_id": run_id,
        "config": config,
        "test_count": len(test_keys),
        "test_keys": test_keys,
    }


def handle_evaluate_one(event: dict, config: dict, run_id: str) -> dict:
    """Evaluate a single test case (called from Map state)."""
    import storage
    from pipeline.evaluate import _get_agent_id, _send_message, _wait_for_run, _get_messages
    from auth import get_wxo_credentials
    import time

    test_key = event.get("test_key", "")
    test_name = test_key.split("/")[-1].replace(".json", "")

    token, instance_url = get_wxo_credentials()
    instance_url = instance_url.rstrip("/")

    # Load test case
    tc = storage.read_json(test_key)
    if not tc:
        return {"test_name": test_name, "status": "failed", "error": f"Test not found: {test_key}"}

    agent_name = config.get("agent_name", tc.get("agent", "your_target_agent"))
    question = tc.get("starting_sentence", "")

    try:
        agent_id = _get_agent_id(instance_url, token, agent_name)
        if not agent_id:
            return {"test_name": test_name, "status": "failed", "error": f"Agent not found: {agent_name}"}

        thread_id, wxo_run_id = _send_message(instance_url, token, agent_id, question)
        if not thread_id or not wxo_run_id:
            return {"test_name": test_name, "status": "failed", "error": "No thread/run from WxO"}

        run_status = _wait_for_run(instance_url, token, wxo_run_id)
        if run_status.get("status", "").lower() != "completed":
            return {"test_name": test_name, "status": "failed",
                    "error": f"Run ended with: {run_status.get('status')}"}

        time.sleep(1)
        messages = _get_messages(instance_url, token, thread_id)

        storage.write_json(
            f"eval_results/{run_id}/messages/{test_name}.messages.json",
            messages,
        )

        return {
            "test_name": test_name,
            "status": "completed",
            "message_count": len(messages),
        }

    except Exception as e:
        logger.error(f"[Pipeline] evaluate_one {test_name} failed: {e}")
        return {"test_name": test_name, "status": "failed", "error": str(e)}


def handle_enrich(event: dict, run_id: str, session_id: str) -> dict:
    """Enrich all test cases with discovered tool calls."""
    import session_store
    from pipeline.enrich import enrich_run

    # Get test data prefix from generate output
    gen_run_id = run_id  # test_data is stored under the same run_id

    session_store.set_status(
        session_id, "running", step="enrich",
        progress="Step 3/4: Extracting tool calls from responses..."
    )

    result = enrich_run(run_id, source_test_prefix=f"test_data/{gen_run_id}/")

    session_store.set_status(
        session_id, "running", step="enrich",
        progress=f"Step 3/4: Enriched {result.get('enriched_count', 0)} test cases."
    )

    return {
        "session_id": session_id,
        "run_id": run_id,
        "enriched_count": result.get("enriched_count", 0),
    }


def handle_analyze(event: dict, run_id: str, session_id: str) -> dict:
    """Run LLM-as-judge analysis on enriched test cases."""
    import session_store
    import storage
    from pipeline.analyze import analyze_run
    from auth import get_wxo_credentials

    config = event.get("config", {})
    skip_judge = config.get("skip_judge", False)
    skip_rca = config.get("skip_rca", False)

    session_store.set_status(
        session_id, "running", step="analyze",
        progress="Step 4/4: Running LLM-as-Judge evaluation + RCA..."
    )

    token, instance_url = get_wxo_credentials()
    model_id = config.get("model_id", "meta-llama/llama-3-3-70b-instruct")

    report = analyze_run(
        run_id=run_id,
        skip_judge=skip_judge,
        skip_rca=skip_rca,
        token=token,
        instance_url=instance_url,
        model=model_id,
    )

    # Check if analyze returned an error (no enriched cases)
    if report.get("error"):
        session_store.set_status(
            session_id, "failed", step="analyze",
            error=report["error"],
        )
        raise RuntimeError(report["error"])

    summary = report.get("summary", {})
    results_key = f"analyze/{run_id}/report.json"

    # Update session as completed
    rca_issues = summary.get("rca_summary", {}).get("total_issues", 0)
    progress_msg = (
        f"Pipeline complete! Journey: {summary.get('journey_success_rate', 0):.0f}% | "
        f"Tools: {summary.get('tool_recall', 0):.0f}% | "
        f"Judge: {summary.get('llm_judge_pass_rate', 0):.0f}%"
    )
    if rca_issues > 0:
        progress_msg += f" | RCA Issues: {rca_issues}"

    session_store.set_status(
        session_id, "completed", step="done",
        progress=progress_msg,
        results_key=results_key,
    )

    # Also update S3 run status for backward compatibility
    storage.save_run_status(run_id, {
        "run_id": run_id,
        "status": "completed",
        "step": "done",
        "summary": summary,
        "completed_at": datetime.utcnow().isoformat(),
    })

    return {
        "session_id": session_id,
        "run_id": run_id,
        "summary": summary,
        "results_key": results_key,
    }


def handle_redteam(event: dict, run_id: str, session_id: str) -> dict:
    """Legacy red team handler (backward compat for async Lambda invocations).

    Routes to the new attack-based red team system when possible.
    Falls back to a simple evaluate_all + generate_report for the old
    /eval/redteam API route that invokes this via Lambda async.
    """
    import session_store
    import storage
    from pipeline.redteam import evaluate_all, generate_report
    from auth import get_wxo_credentials

    config = event.get("config", {})
    model_id = config.get("model_id", "meta-llama/llama-3-3-70b-instruct")

    if session_id:
        try:
            session_store.set_status(
                session_id, "running", step="redteam",
                progress="Running red team security analysis..."
            )
        except Exception:
            pass

    token, instance_url = get_wxo_credentials()

    # Check if there are already attack results for this run (new flow)
    result_keys = storage.list_keys(
        f"redteam/{run_id}/results/", suffix=".result.json"
    )

    if result_keys:
        # New flow: evaluate existing attack results
        evaluations = evaluate_all(
            run_id=run_id, model_id=model_id,
            token=token, instance_url=instance_url,
        )
        report = generate_report(
            run_id=run_id, evaluations=evaluations,
            model_id=model_id, token=token, instance_url=instance_url,
        )
    else:
        # No attack results — return empty report
        report = {
            "summary": {"total_attacks": 0, "succeeded": 0},
            "attacks": [],
            "recommendations": [],
            "error": f"No attack results found for run {run_id}. Use the new /eval/redteam/start flow.",
        }
        storage.write_json(f"redteam/{run_id}/report.json", report)

    summary = report.get("summary", {})

    if session_id:
        try:
            session_store.set_status(
                session_id, "completed", step="done",
                progress=(
                    f"Red team complete: {summary.get('succeeded', 0)}/{summary.get('total_attacks', 0)} "
                    f"attacks succeeded"
                ),
            )
        except Exception:
            pass

    return {
        "session_id": session_id,
        "run_id": run_id,
        "summary": summary,
    }


def handle_redteam_plan(event: dict, config: dict, run_id: str, session_id: str) -> dict:
    """Generate red team attack plans."""
    import session_store
    from pipeline.redteam import plan_attacks
    from auth import get_wxo_credentials

    session_store.set_status(
        session_id, "running", step="redteam_plan",
        progress="Red team: Planning attack scenarios..."
    )

    token, instance_url = get_wxo_credentials()
    model_id = config.get("model_id", "meta-llama/llama-3-3-70b-instruct")
    attacks = config.get("attacks", "all")
    agent_name = config.get("agent_name", "your_target_agent")
    max_variants = config.get("max_variants")

    plan_keys = plan_attacks(
        attacks=attacks,
        agent_name=agent_name,
        run_id=run_id,
        token=token,
        instance_url=instance_url,
        model_id=model_id,
        max_variants=int(max_variants) if max_variants else None,
    )

    session_store.set_status(
        session_id, "running", step="redteam_plan",
        progress=f"Red team: Planned {len(plan_keys)} attack scenarios."
    )

    return {
        "session_id": session_id,
        "run_id": run_id,
        "config": config,
        "plan_count": len(plan_keys),
        "plan_keys": plan_keys,
    }


def handle_redteam_run_one(event: dict, config: dict, run_id: str) -> dict:
    """Run a single attack from a plan (called from Map state)."""
    from pipeline.redteam import run_single_attack

    plan_key = event.get("plan_key", "")
    result = run_single_attack(plan_key, config)

    return {
        "plan_key": plan_key,
        "status": result.get("status", "failed"),
        "attack_name": result.get("attack_name", ""),
        "turns": result.get("turns", 0),
    }


def handle_redteam_evaluate(event: dict, run_id: str, session_id: str) -> dict:
    """Evaluate all attack results and generate final report."""
    import session_store
    import storage
    from pipeline.redteam import evaluate_all, generate_report
    from auth import get_wxo_credentials

    config = event.get("config", {})
    model_id = config.get("model_id", "meta-llama/llama-3-3-70b-instruct")

    session_store.set_status(
        session_id, "running", step="redteam_evaluate",
        progress="Red team: Evaluating attack results..."
    )

    token, instance_url = get_wxo_credentials()

    evaluations = evaluate_all(
        run_id=run_id, model_id=model_id, token=token, instance_url=instance_url,
    )

    report = generate_report(
        run_id=run_id, evaluations=evaluations,
        model_id=model_id, token=token, instance_url=instance_url,
    )

    summary = report.get("summary", {})
    succeeded = summary.get("succeeded", 0)
    total = summary.get("total_attacks", 0)

    session_store.set_status(
        session_id, "completed", step="done",
        progress=(
            f"Red team complete: {succeeded}/{total} attacks succeeded "
            f"({summary.get('success_rate', 0):.0f}% success rate)"
        ),
    )

    # Also save run status for backward compat
    storage.save_run_status(run_id, {
        "run_id": run_id,
        "status": "completed",
        "step": "done",
        "summary": summary,
        "completed_at": datetime.utcnow().isoformat(),
    })

    return {
        "session_id": session_id,
        "run_id": run_id,
        "summary": summary,
    }


def handle_update_status(event: dict) -> dict:
    """Update session status (used by Step Functions Pass states)."""
    import session_store

    session_id = event.get("session_id", "")
    status = event.get("new_status", "running")
    step = event.get("new_step")
    progress = event.get("new_progress")

    session_store.set_status(session_id, status, step=step, progress=progress)

    return {
        "session_id": session_id,
        "status": status,
        "step": step,
    }
