# Troubleshooting

## "No enriched test cases found for run pipe_..."

The pipeline got past Generate but produced no transcripts in Evaluate.

**Almost always one of:**

1. **The target agent name doesn't exist on your WxO env.** Check the pipeline Lambda logs — there will be a "Agent X not found. Available: [...]" warning listing every deployed agent. Pick the right name.

2. **Your WxO API key expired or is invalid.** The pipeline calls `/v1/orchestrate/runs` with a bearer token derived from `WXO_API_KEY`. Refresh the env var on the Lambda:
   ```bash
   aws lambda update-function-configuration --function-name wxo-eval-pipeline \
     --environment "Variables={...,WXO_API_KEY=<new-key>,...}"
   ```

3. **The target agent's underlying LLM was deprecated.** This shows up as `404 model not found` errors when the pipeline tries to send messages to your agent. Update the agent's LLM:
   ```yaml
   # in your agent's YAML
   llm: meta-llama/llama-3-3-70b-instruct  # or another supported model
   ```
   Then re-import: `orchestrate agents import -f your_agent.yaml`

---

## "API Gateway URL still has `API_GATEWAY_URL_PLACEHOLDER`"

You forgot Step 3 of the README. Run:

```bash
API_GATEWAY_URL=https://<your-id>.execute-api.<region>.amazonaws.com/prod \
  ./scripts/patch_tools.sh
```

Then re-import the affected tools:

```bash
./scripts/deploy_wxo.sh
```

---

## "spawn uvx ENOENT" or similar uvx-related errors

These come from the **watsonx Orchestrate ADK MCP server**, not from this repo. Install `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

The MCP server is unrelated to running the evaluator — it's only needed if you're using IBM Bob (or another MCP-aware coding agent) to *create* WxO agents. The evaluator itself doesn't depend on it.

---

## "The token found for environment 'X' is missing or expired"

WxO MCSP tokens expire after about 60 minutes. Refresh:

```bash
orchestrate env activate <your-env-name>
```

You'll need your WxO API key handy.

---

## "Lambda timeout" on long evaluations

Default pipeline Lambda timeout is 15 minutes. For test sets of >50 rows, the LLM-as-Judge stage can run longer. Either:

1. Trim the test set — most useful eval signals come from 5–20 well-chosen tests, not 50+ similar ones
2. Run with `skip_judge: true` to skip LLM-as-Judge and only get tool routing + journey metrics

To set `skip_judge`, add it to the eval session config when starting:
```
> skip the LLM judge for now
```

The eval pipeline agent will pass `skip_judge: true` through to the backend.

---

## "Forbidden" 403 from API Gateway

Two possibilities:

1. **Account SCP blocking public Lambda URLs.** This repo uses API Gateway HTTP APIs by default (not Lambda Function URLs), so this shouldn't apply. If you swapped to Function URLs and hit this, switch back.

2. **Wrong API Gateway URL in tool YAMLs.** Check that the URL matches the one `deploy.sh` printed:
   ```bash
   grep "url:" tools/v2/eval_session_start.yaml
   # should match: deploy.sh's printed API Gateway URL
   ```

---

## "Pipeline still running on step 1/4" forever

Step 1 is Generate (Excel parsing). It should take <10 seconds for a small file.

If it hangs:

1. Check the Excel file has the right columns: `Question` (or `question`), `Expected Answer` (or `expected_answer`)
2. Check there's no merged cells, no images, no formulas — the parser uses `openpyxl` and prefers boring tabular data
3. Check the pipeline Lambda's CloudWatch logs for the actual exception

---

## How do I see Lambda logs?

```bash
# Recent logs from the pipeline Lambda
aws logs tail /aws/lambda/wxo-eval-pipeline --follow

# Just errors
aws logs filter-log-events --log-group-name /aws/lambda/wxo-eval-pipeline \
  --filter-pattern ERROR \
  --start-time $(($(date +%s)*1000 - 3600000))
```

---

## Agent name fuzzy matching

The evaluator does fuzzy matching on agent names. If you say "my-agent" and the actual deployed name is `my_agent_18ABC`, it'll find it. Match order:

1. Exact match on `name`
2. Exact match on `display_name`
3. Case-insensitive on either
4. Normalized (strip `_` and `-`, lowercase) match on either
5. Normalized prefix match (catches WxO auto-generated suffixes)

If all five fail, the warning log lists every deployed agent so you can copy the right name.

---

## Costs

Rough order of magnitude per evaluation run (5-row test set):

| Resource | Per run | Notes |
|----------|---------|-------|
| Lambda invocations | ~$0.00001 | Free tier covers this for years |
| Step Functions transitions | ~$0.0001 | Usually free tier |
| S3 storage | <1 MB per run | Negligible |
| DynamoDB | <100 ops per run | Free tier on-demand pricing |
| **WxO LLM-as-Judge calls** | **The main cost** | One judge call per test row, plus one RCA call per failed tool. Cost depends on your WxO plan and the model you select. |

For a 5-row eval set with 0–5 tool failures, expect ~5–10 LLM calls per run. For 50 rows, ~50–150 calls. Budget accordingly.
