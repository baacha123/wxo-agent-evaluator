# Deployment Guide

End-to-end deployment of the eval system, in detail. For a quick path, see the README.

---

## Prerequisites

### AWS

- An AWS account with permissions to create: Lambda, API Gateway, Step Functions, DynamoDB, S3, IAM roles, EventBridge rules
- AWS CLI installed and configured (`aws configure`)
- The default region in your CLI matches the region you want to deploy to

### Python (for Lambda packaging)

- Python 3.12 (required — the deploy script pins this for cross-platform Lambda compatibility)
- `pip` and `zip` available on PATH

### watsonx Orchestrate

- An active WxO instance (any plan that supports custom agents)
- A WxO API key
- The `orchestrate` ADK CLI installed:
  ```bash
  pip install ibm-watsonx-orchestrate
  ```

### LLM access

The Analyze stage uses the WxO Orchestrate Gateway to call an LLM for LLM-as-Judge and RCA generation. Make sure your WxO instance has access to one of:

- `meta-llama/llama-3-3-70b-instruct` (recommended default)
- `openai/gpt-oss-120b`
- `mistralai/mistral-large-2512`

Or any other chat-completions-compatible model your gateway supports. Set the model in `.env` (`JUDGE_MODEL_ID=...`).

---

## Step-by-Step

### 1. Clone the repo and configure environment

```bash
git clone https://github.com/baacha123/wxo-agent-evaluator.git
cd wxo-agent-evaluator
cp .env.example .env
```

Edit `.env`:

```
AWS_REGION=us-east-1
WXO_INSTANCE_URL=https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/<your-instance-id>
WXO_API_KEY=<your-wxo-api-key>
WXO_ENV_NAME=us-south
JUDGE_MODEL_ID=meta-llama/llama-3-3-70b-instruct
```

### 2. Deploy the AWS backend

```bash
bash deploy.sh
```

What this script does (~3–5 minutes):

1. Validates AWS CLI is logged in
2. Creates the IAM execution role (`wxo-eval-role`) for the Lambdas
3. Creates the IAM role (`wxo-eval-sfn-role`) for Step Functions
4. Creates the S3 bucket (`wxo-eval-pipeline`) with EventBridge enabled
5. Creates the DynamoDB table (`wxo-eval-sessions`) with TTL on the `expires_at` field
6. Builds three deployment ZIPs (api, pipeline, s3-trigger) by `pip install`-ing requirements.txt and packaging the .py files
7. Creates or updates the three Lambda functions, with env vars from `.env`
8. Creates the Step Functions state machines (eval pipeline + red team)
9. Creates the API Gateway with routes for every endpoint
10. Wires the EventBridge rule that triggers a pipeline when a user uploads to S3

When it's done, it prints:

```
API Gateway URL: https://<api-id>.execute-api.<region>.amazonaws.com/prod
```

**Save this URL** — you need it in the next step.

### 3. Patch the tool YAMLs with your API Gateway URL

The tool YAMLs ship with `API_GATEWAY_URL_PLACEHOLDER` so you don't accidentally point your tools at someone else's backend. Patch them:

```bash
API_GATEWAY_URL=https://<api-id>.execute-api.<region>.amazonaws.com/prod \
  ./scripts/patch_tools.sh
```

This rewrites all 12 tool YAMLs in place. Re-running the script is a no-op (it only patches files that still have the placeholder).

### 4. Authenticate to your WxO environment

```bash
orchestrate env activate <your-env-name>
# Paste your WxO API key when prompted
```

If you're not sure what env name to use, run `orchestrate env list`.

### 5. Deploy WxO agents and tools

```bash
./scripts/deploy_wxo.sh
```

This imports:
- All 12 tool YAMLs (must come first — agents reference them)
- The 3 collaborator agents
- The supervisor agent (last — it references the 3 collaborators)

### 6. Smoke-test it

Open your watsonx Orchestrate chat UI, find **`agent_evaluator`**, and run:

```
You:    Hi, I want to evaluate an agent
Agent:  Which agent? Give me the exact name.
You:    <any agent in your WxO instance>
Agent:  Got it. Upload your test cases here: <link>
        Type "done" when uploaded.
```

Click the link, upload `samples/questions.xlsx`, then come back and type `done`.

If everything is wired up:
- The pipeline takes 1–3 minutes for a 5-row test set
- You see Tool Routing metrics, LLM Judge scores, and a per-test breakdown
- Typing `analyze` shows a per-test severity breakdown with RCA and fix suggestions
- Typing `red team` runs an adversarial campaign

---

## Configuration Reference

### Environment variables (Lambda)

The deploy script wires these into both the API Lambda and the Pipeline Lambda:

| Var | Required | Purpose |
|-----|----------|---------|
| `S3_BUCKET` | Yes | Bucket for pipeline data |
| `DYNAMODB_TABLE` | Yes | Session state table |
| `WXO_API_KEY` | Yes | Used to call WxO API and gateway |
| `WXO_INSTANCE_URL` | Yes | Your WxO instance URL |
| `JUDGE_MODEL_ID` | No | Defaults to `meta-llama/llama-3-3-70b-instruct` |
| `SFN_ARN` | Auto | Set by deploy.sh |
| `REDTEAM_SFN_ARN` | Auto | Set by deploy.sh |
| `API_ID` | Auto | Set by deploy.sh |

### Lambda settings

| Lambda | Timeout | Memory | Why |
|--------|---------|--------|-----|
| `wxo-eval-api` | 30s | 1024 MB | WxO has a 40s tool timeout; we use 30s to leave headroom |
| `wxo-eval-pipeline` | 900s (15m) | 1024 MB | The Analyze stage can take a few minutes for large test sets |
| `wxo-eval-s3-trigger` | 30s | 1024 MB | Just kicks off Step Functions |

### Choosing a different LLM-as-Judge model

Edit `.env`:

```
JUDGE_MODEL_ID=openai/gpt-oss-120b
```

Then redeploy: `bash deploy.sh` (it updates Lambda env vars in place — no IAM changes).

The model has to be accessible via your WxO Orchestrate Gateway. Check what's available:

```bash
orchestrate models list
```

---

## Updating

When you change Python code in this repo:

```bash
bash deploy.sh
```

It rebuilds the ZIPs and updates the Lambdas in place. AWS resources that already exist are reused. No agent/tool YAML changes are pushed by `deploy.sh` — that's `./scripts/deploy_wxo.sh`'s job.

When you change agent or tool YAMLs:

```bash
./scripts/deploy_wxo.sh
```

The orchestrate CLI does an idempotent upsert: existing agents/tools get updated in place.

---

## Tearing Down

There's no `teardown.sh` script — too easy to nuke the wrong account by accident. To remove what you deployed:

```bash
# WxO side
orchestrate agents remove --name agent_evaluator --kind native
orchestrate agents remove --name eval_pipeline_agent --kind native
orchestrate agents remove --name eval_analyze_agent --kind native
orchestrate agents remove --name eval_redteam_agent --kind native
for t in eval_session_start eval_session_config eval_upload eval_start \
         eval_status eval_results eval_explain eval_reanalyze \
         eval_redteam eval_redteam_start eval_redteam_results eval_redteam_list; do
  orchestrate tools remove --name $t
done

# AWS side (in this order)
aws lambda delete-function --function-name wxo-eval-api
aws lambda delete-function --function-name wxo-eval-pipeline
aws lambda delete-function --function-name wxo-eval-s3-trigger
aws apigatewayv2 delete-api --api-id <your-api-id>
aws stepfunctions delete-state-machine --state-machine-arn arn:aws:states:<region>:<account>:stateMachine:wxo-eval-pipeline-sfn
aws stepfunctions delete-state-machine --state-machine-arn arn:aws:states:<region>:<account>:stateMachine:wxo-eval-redteam-sfn
aws dynamodb delete-table --table-name wxo-eval-sessions
aws s3 rm s3://wxo-eval-pipeline --recursive
aws s3 rb s3://wxo-eval-pipeline
```

Don't forget to delete the IAM roles last:

```bash
aws iam detach-role-policy --role-name wxo-eval-role --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role-policy --role-name wxo-eval-role --policy-name <inline-policy-names>
aws iam delete-role --role-name wxo-eval-role
aws iam delete-role --role-name wxo-eval-sfn-role
```
