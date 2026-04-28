# wxo-agent-evaluator

A conversational evaluation pipeline for **watsonx Orchestrate** agents.

You chat with an evaluator agent. It walks you through configuring an evaluation, running the pipeline, scoring results with three independent signals (tool routing, journey, LLM-as-Judge), surfacing root causes, and even running adversarial red-team campaigns — all without leaving the chat.

```
You:    Hi, I want to evaluate an agent
Agent:  Which agent? Give me the exact name.
You:    my_agent
Agent:  Got it. Upload your test cases here: <link>
        Type "done" when uploaded.
You:    [upload questions.xlsx] done
Agent:  Running pipeline... [results table appears]
You:    analyze
Agent:  [severity breakdown + root causes + suggestions]
You:    red team
Agent:  [adversarial campaign results]
```

---

## What's in the Box

- **AWS backend** — Step Functions pipeline (Generate → Evaluate → Enrich → Analyze) running on Lambda + S3 + DynamoDB
- **Four WxO agents** — supervisor + 3 collaborators (pipeline, analyze, red team)
- **Twelve OpenAPI tools** — the connective tissue between WxO agents and the AWS pipeline
- **Composite severity scoring** — final verdict is the *worst* of three independent signals (answer quality, journey success, tool routing)
- **Tool-level RCA** — every wrong/missing/extra tool call gets an LLM-generated root cause and concrete fix suggestion
- **Red-team module** — adversarial attack campaigns (jailbreak, instruction override, encoded input, role-play, and more)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          User (chat UI)                          │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                    agent_evaluator (supervisor)                  │
│                          ~20 line prompt                         │
└──────┬───────────────────────┬──────────────────────────┬───────┘
       │                       │                          │
       ▼                       ▼                          ▼
┌──────────────┐      ┌───────────────┐         ┌──────────────────┐
│ pipeline     │      │ analyze       │         │ red_team         │
│ agent        │      │ agent         │         │ agent            │
│ (run eval)   │      │ (RCA + sev)   │         │ (attacks)        │
└──────┬───────┘      └───────┬───────┘         └─────────┬────────┘
       │                      │                            │
       └──────────────────────┴────────────────────────────┘
                              │
                              ▼  HTTPS (OpenAPI tools)
┌─────────────────────────────────────────────────────────────────┐
│                  AWS API Gateway (HTTP)                          │
└──────────────────────────────┬──────────────────────────────────┘
                               │
              ┌────────────────┴─────────────────┐
              ▼                                  ▼
    ┌───────────────────┐              ┌────────────────────┐
    │ wxo-eval-api      │              │ wxo-eval-pipeline  │
    │ (request handler) │              │ (Step Functions    │
    │                   │              │  step dispatcher)  │
    └─────────┬─────────┘              └──────────┬─────────┘
              │                                   │
              ▼                                   ▼
    ┌──────────────────┐               ┌──────────────────────┐
    │ DynamoDB         │               │ Step Functions       │
    │ wxo-eval-        │               │   1. Generate        │
    │ sessions         │               │   2. Evaluate (Map)  │
    └──────────────────┘               │   3. Enrich          │
                                       │   4. Analyze         │
                                       └──────────┬───────────┘
                                                  │
                                                  ▼
                                         ┌──────────────────┐
                                         │ S3               │
                                         │ wxo-eval-        │
                                         │ pipeline         │
                                         │  uploads/        │
                                         │  test_data/      │
                                         │  eval_results/   │
                                         │  enriched/       │
                                         │  analyze/        │
                                         │  redteam/        │
                                         └──────────────────┘
```

---

## Prerequisites

| What | Why | How to get it |
|------|-----|---------------|
| AWS account | Hosts the backend pipeline | https://aws.amazon.com |
| AWS CLI configured | Used by `deploy.sh` | `aws configure` |
| Python 3.12 + pip | For Lambda packaging | https://www.python.org |
| watsonx Orchestrate instance | Where the WxO agents live | https://www.ibm.com/products/watsonx-orchestrate |
| `orchestrate` ADK CLI | To import agents/tools | `pip install ibm-watsonx-orchestrate` |
| `zip` and `bash` | Used by build scripts | Pre-installed on macOS / most Linux |

---

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/baacha123/wxo-agent-evaluator.git
cd wxo-agent-evaluator
cp .env.example .env
# Edit .env with your AWS region, WxO instance URL, and WxO API key
```

### 2. Deploy AWS backend

```bash
bash deploy.sh
```

This creates:
- 3 Lambda functions (`wxo-eval-api`, `wxo-eval-pipeline`, `wxo-eval-s3-trigger`)
- 1 API Gateway (`wxo-eval-api`)
- 1 DynamoDB table (`wxo-eval-sessions`)
- 2 Step Functions state machines (eval pipeline + red team)
- 1 S3 bucket (`wxo-eval-pipeline`)
- IAM roles + EventBridge S3 upload notifications

When it's done, the script prints your **API Gateway URL**. Copy it.

### 3. Patch the tool YAMLs with your API Gateway URL

```bash
API_GATEWAY_URL=https://<your-id>.execute-api.<region>.amazonaws.com/prod \
  ./scripts/patch_tools.sh
```

### 4. Authenticate to your WxO environment

```bash
orchestrate env activate <your-env-name>
# Provide your WxO API key when prompted
```

### 5. Deploy the WxO agents and tools

```bash
./scripts/deploy_wxo.sh
```

This imports all 12 tools and the 4 agents (collaborators first, then the supervisor).

### 6. Test it

Open your watsonx Orchestrate chat UI, find **`agent_evaluator`**, and start a conversation:

```
> Hi, I want to evaluate an agent
> <name of any agent in your WxO instance>
> [upload samples/questions.xlsx]
> done
> analyze
> red team
```

---

## How Scoring Works

A test case is rated on three independent dimensions. **The final severity is the worst of the three.**

| Dimension | What it measures | HIGH | MEDIUM | GOOD |
|-----------|-----------------|------|--------|------|
| **Answer** | LLM-as-Judge score | < 0.3 | 0.3–0.7 | >= 0.7 |
| **Journey** | Did the conversation complete? | FAIL | — | PASS |
| **Tools** | Were the right tools called? | missing/incorrect | extra call | all correct |

This catches the dangerous case where an agent calls completely wrong tools but the LLM happens to produce a passable answer from training data — the answer score might be 0.85 but the tool dimension flags HIGH, so the overall verdict is HIGH.

---

## Repo Layout

```
.
├── README.md                  ← you are here
├── deploy.sh                  ← deploys the AWS backend (Lambdas, API GW, etc.)
├── .env.example               ← config template
├── requirements.txt           ← Python deps for the Lambdas
│
├── api_handler.py             ← API Gateway → Lambda handler
├── pipeline_handler.py        ← Step Functions step dispatcher
├── s3_trigger_handler.py      ← S3 upload event handler
├── auth.py                    ← WxO token fetch / exchange
├── storage.py                 ← S3 helpers
├── session_store.py           ← DynamoDB session helpers
│
├── pipeline/
│   ├── generate.py            ← Stage 1: Excel → test case JSONs
│   ├── evaluate.py            ← Stage 2: run target agent, capture transcript
│   ├── enrich.py              ← Stage 3: extract tool call chain
│   ├── analyze.py             ← Stage 4: LLM-as-Judge + tool-level RCA
│   └── redteam.py             ← red team attack runner
│
├── agents/                    ← WxO agent YAMLs
│   ├── eval_supervisor_agent.yaml
│   ├── eval_pipeline_agent.yaml
│   ├── eval_analyze_agent.yaml
│   └── eval_redteam_agent.yaml
│
├── tools/v2/                  ← OpenAPI tool YAMLs (12 total)
│   ├── eval_session_start.yaml
│   ├── eval_session_config.yaml
│   ├── eval_upload.yaml
│   ├── eval_start.yaml
│   ├── eval_status.yaml
│   ├── eval_results.yaml
│   ├── eval_explain.yaml
│   ├── eval_reanalyze.yaml
│   ├── eval_redteam.yaml
│   ├── eval_redteam_start.yaml
│   ├── eval_redteam_results.yaml
│   └── eval_redteam_list.yaml
│
├── samples/
│   └── questions.xlsx         ← 5-row sample test set
│
├── scripts/
│   ├── deploy_wxo.sh          ← imports tools + agents to active WxO env
│   └── patch_tools.sh         ← injects API Gateway URL into tool YAMLs
│
└── docs/
    ├── ARCHITECTURE.md        ← deeper dive
    ├── DEPLOYMENT.md          ← step-by-step deployment notes
    └── TROUBLESHOOTING.md     ← common errors and fixes
```

---

## Test Data Format

`samples/questions.xlsx` shows the expected format. Two columns:

| Question | Expected Answer |
|----------|-----------------|
| Are our 1099 forms available online? | Yes, your 1099 forms are available online... |
| How do I add a dependent after marriage? | You have 31 days from your marriage date... |

The pipeline lowercases column names and converts spaces to underscores, so `Question` / `question` / `QUESTION` all work; same for `Expected Answer` / `expected_answer`.

---

## Documentation

- **[Architecture](docs/ARCHITECTURE.md)** — how the pipeline and agents fit together
- **[Deployment](docs/DEPLOYMENT.md)** — step-by-step deployment notes, configuration, model selection
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** — common errors and how to fix them

---

## License

MIT
