# Sample Test Data

`questions.xlsx` is a ready-to-use sample test set. Use it to smoke-test the evaluator end-to-end after deploying.

## Format

The pipeline expects an Excel workbook with **two columns**:

| Column | Required | What it is |
|--------|----------|-----------|
| **Question** | Yes | The question to send to the target agent |
| **Expected Answer** | Yes | The ground-truth answer the LLM-as-Judge will score the agent's actual response against |

Column header naming is flexible — the pipeline lowercases names and converts spaces to underscores, so all of these work:

- `Question`, `question`, `QUESTION`
- `Expected Answer`, `expected answer`, `expected_answer`, `Expected_Answer`

## Test Set Composition

The sample covers four categories, intentionally:

| Category | Count | Why |
|----------|-------|-----|
| Happy-path benefits/HR questions | 5 | Validates standard tool routing and answer quality |
| Life events / qualifying scenarios | 2 | Tests reasoning over more complex situations |
| Ambiguous or underspecified questions | 2 | Tests how the agent asks clarifying questions |
| Out-of-scope questions (weather, jokes) | 3 | Tests refusal behavior and scope boundaries |
| Adversarial / safety probes | 3 | Tests handling of bad input, prompt injection, gibberish |

You should expect a mix of PASS / FAIL on a real agent — that's what makes the metrics meaningful. A test set where everything passes isn't a useful test set.

## Using Your Own Test Set

Copy this format. Fifteen well-chosen tests are usually more useful than fifty similar ones. Aim for:

- **Coverage** of every tool the agent should be able to call
- **Variety** of phrasing — short, long, ambiguous, multi-intent
- A **mix of in-scope and out-of-scope** questions so you can measure refusal behavior
- A few **adversarial** probes (try-to-break-it questions)

## What the Evaluator Does With This

For each row in the file, the pipeline:

1. **Generate** — turns the row into a structured test case
2. **Evaluate** — sends the `Question` to your target agent and captures the full conversation
3. **Enrich** — extracts the actual tool-call chain
4. **Analyze** — scores the agent's response against `Expected Answer` using LLM-as-Judge, then runs tool-level RCA on any tool-call mismatches

Final output: a per-test breakdown with composite severity (HIGH / MEDIUM / GOOD), root causes, and concrete fix suggestions.
