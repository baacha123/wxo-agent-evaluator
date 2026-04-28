# Architecture

## Two Layers

The system has two distinct layers that talk to each other over HTTP via a thin API Gateway.

### Layer 1 — The AWS Backend (the engine)

This is where evaluation actually happens. It's deployed as serverless infrastructure in your AWS account.

| Component | Purpose |
|-----------|---------|
| `wxo-eval-api` (Lambda) | Handles all incoming HTTP requests from the WxO tools |
| `wxo-eval-pipeline` (Lambda) | Step Functions step dispatcher — runs each pipeline stage |
| `wxo-eval-s3-trigger` (Lambda) | Auto-starts a pipeline run when a user uploads a test file to S3 |
| `wxo-eval-pipeline-sfn` (Step Functions) | Orchestrates Generate → Evaluate (Map) → Enrich → Analyze |
| `wxo-eval-redteam-sfn` (Step Functions) | Orchestrates the red-team campaign |
| `wxo-eval-sessions` (DynamoDB) | Tracks evaluation session state (config, current step, run_id) |
| `wxo-eval-pipeline` (S3 bucket) | Stores uploads, generated test cases, raw transcripts, enriched data, reports |
| API Gateway | Public HTTP front door for the WxO tools |

### Layer 2 — The WxO Conversational Agents

This is where the user actually interacts with the system. Four agents on watsonx Orchestrate:

| Agent | Lines of prompt | Tools | Purpose |
|-------|-----------------|-------|---------|
| `agent_evaluator` (supervisor) | ~20 | none | Routes user messages to the right collaborator |
| `eval_pipeline_agent` | ~80 | session_start, session_config, upload, start, status, results | Drives the eval flow end-to-end |
| `eval_analyze_agent` | ~70 | explain, reanalyze, status | RCA + composite severity breakdown |
| `eval_redteam_agent` | ~60 | redteam_start, redteam_results, redteam_list, status | Adversarial campaigns |

The supervisor pattern was an explicit design choice. We started with one giant agent (270+ lines of prompt) and watched it struggle with intent disambiguation as we added capabilities. Splitting it into a thin router + focused specialists made every agent more reliable.

---

## The Pipeline (Stage by Stage)

Stages run on **AWS Step Functions**. Each stage's output is the next stage's input. State persists in S3.

### Stage 1 — Generate

**Input:** an Excel file with `Question` and `Expected Answer` columns
**Output:** one JSON test case per row, written to `s3://<bucket>/test_data/<run_id>/test_NNN.json`

Each row becomes a structured test case with the question, the expected answer, and metadata downstream stages need (e.g. expected agent, expected tool chain hints).

### Stage 2 — Evaluate

**Input:** the test case JSONs from Stage 1
**Output:** raw conversation transcripts at `s3://<bucket>/eval_results/<run_id>/messages/test_NNN.messages.json`

This is a **Map state** — every test case runs in parallel against the target agent via WxO's `/v1/orchestrate/runs` API. We capture the full conversation: every tool call, every response, every thread message. No LLM reasoning yet, just observation.

### Stage 3 — Enrich

**Input:** raw transcripts from Stage 2
**Output:** structured tool call chains at `s3://<bucket>/enriched/<run_id>/test_NNN.json`

Parses each transcript and extracts the actual tool call chain: which tools were invoked, in what order, with what arguments. This is what lets us compare *what the agent did* against *what it should have done* in Stage 4.

### Stage 4 — Analyze

**Input:** the enriched test cases from Stage 3
**Output:** a structured report at `s3://<bucket>/analyze/<run_id>/report.json`

Two independent evaluations per test case:

1. **LLM-as-Judge** — calls a separate LLM (default: a 70b-class instruct model via the WxO Orchestrate Gateway) to score the agent's final answer for correctness, completeness, and relevance on a 0–1 scale. Returns a verdict (CORRECT, PARTIALLY_CORRECT, INCORRECT) and a reasoning trace.

2. **Tool-level RCA** — compares the expected tool chain against the actual tool chain. For each expected tool, classifies the corresponding actual call as `correct`, `missing`, `incorrect`, or `extra`. For every mismatch, asks the LLM to generate a concrete root cause and a fix suggestion (e.g. "the agent called `password_reset` but should have called `account_lookup` first — add a routing rule that gates `password_reset` behind authentication").

The report.json is what the WxO agent uses to render the final tables and the per-test severity breakdown.

---

## Composite Severity (the interesting part)

A test case has three independent signals. We scored each one separately at first, then realized that **a single combined severity is more useful than three columns of green/yellow/red**.

```
severity = WORST(answer_severity, journey_severity, tools_severity)
```

| Dimension | HIGH | MEDIUM | GOOD |
|-----------|------|--------|------|
| **Answer** (judge score) | < 0.3 | 0.3–0.7 | >= 0.7 |
| **Journey** (flow) | FAIL | — | PASS |
| **Tools** (routing) | missing or incorrect | extra | all correct |

The dangerous case this catches: agent calls **completely wrong tools** but the underlying LLM produces a passable answer from its training data. Score-only severity says GOOD. Composite severity says HIGH — because in production, that luck runs out and the wrong tools fail.

The analyze view always shows the per-dimension breakdown so you can see *why* a test was flagged: "Journey HIGH, Tools GOOD, Answer GOOD" tells you the routing is fine but the conversation didn't complete.

---

## Why a Supervisor Pattern

We started with one monolithic eval agent. It worked. Then we added analyze. Then red team. Then alternate display modes. By the time the prompt was ~270 lines, we started seeing intent-routing failures: typing "analyze" would trigger red team, typing a number would route to the wrong handler.

The fix was a supervisor + collaborators pattern. The supervisor's only job is intent detection. Each collaborator owns one focused area (pipeline, analyze, red team). Each prompt is now 60–80 lines instead of 270+.

Session state lives in DynamoDB (shared across all four agents), so context isn't lost during handoffs. When you say "analyze" after a pipeline run finishes, the supervisor routes to the analyze agent, which reads the session, finds the run_id, and loads the report — no context lost.

---

## Talking to Your Target Agent

The pipeline calls your target agent via WxO's REST API:

```
POST {WXO_INSTANCE_URL}/v1/orchestrate/runs
  agent_id: <looked up from agent_name>
  message: { role: user, content: <question> }
```

Agent lookup is **fuzzy** — it tries:
1. Exact match on `name`
2. Exact match on `display_name`
3. Case-insensitive match on either
4. Normalized match (strip `_-`)
5. Normalized prefix match (catches WxO's auto-suffixes like `_18ABC`)

So users can say "my-agent", "my_agent", "MyAgent", "my-agent-prefix-of-the-real-name" and it still finds the right one.
