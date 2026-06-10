# Agentic AI Projects

Hands-on agent engineering, built from scratch against raw APIs — no agent frameworks. The goal of this repo is to demonstrate, with working code, how I'd think through an agentic deployment for a real customer: where the intelligence lives, where humans stay in the loop, what can go wrong, and how the system knows it's working.

The centerpiece is the **Notion Migration Agent** — a ten-stage pipeline that migrates a CSV into a Notion database with LLM-assisted schema inference, evidence-based human checkpoints, coercion preview, and an adaptive error policy. The two earlier projects (`agent.py`, `workflow_agent.py`) are the foundations it was built on.

All projects use the Anthropic API directly (Claude Sonnet) and, where relevant, the Notion API. Python was chosen for ecosystem richness; the patterns are framework- and language-agnostic.

---

## Background

I built this repo to close a specific gap: real, from-scratch agent-building experience, with enough depth to walk a customer through an agentic deployment — what the agent's job is, what it needs access to, what can go wrong, where a human stays in the loop, and how you know it's working. Every design decision in this code is one I can defend, because each one was made deliberately before it was implemented.

---

## Notion Migration Agent (`research-agent/notion_migration_agent.py`)

**The scenario:** a customer hands you a messy CSV — typos, inconsistent casing, mixed date formats, missing values — and wants it migrated into a Notion database they can trust. The agent handles the full lifecycle, with a human approving every consequential decision before it happens.

### The pipeline

1. **Structural validation** — deterministic checks that the CSV is even parseable (encoding, header, row consistency). Fails fast with customer-actionable errors.
2. **Profile** — two-phase: deterministic Python computes per-column statistics (types, nulls, distinct values); an LLM then reasons over those statistics to infer Notion property types and flag semantic issues (typos, casing, format inconsistencies). The LLM never sees raw rows — only bounded summaries — so cost scales with column count, not row count.
3. **Schema + mapping proposal** — two separate, independently editable artifacts: the shape of the destination, and the flow of data into it. Canonical select options are derived deterministically from the profile (distinct values minus flagged suspicious values) — no second LLM call for work already done upstream.
4. **Human checkpoint 1** — structured command interface (remove / add / edit_type / edit_options / edit_mapping / preview / enter / abort). Edits apply to a working copy as they're typed; cascading consequences (removing a property drops its mappings) are announced explicitly; every edit lands in a timestamped audit history.
5. **Coercion preview** — before anything is written, every mapped value is test-converted against the approved schema. Per-column success/failure counts with row-level examples. A schema can look perfect on paper and still not fit the data — this stage is where that surfaces, with a configurable failure threshold that rejects the run back to the checkpoint with the evidence attached.
6. **Sample selection** — risk-driven, not random: one clean row, rows containing flagged suspicious values, coercion-failure rows, the longest row, the row with the most nulls. Each selection carries its reason.
7. **Sample write** — first contact with the live Notion API: creates the database, writes the sample rows, records per-row outcomes with URLs.
8. **Human checkpoint 2 (in-situ review)** — the human reviews real rendered pages *in Notion*, because some issues (row ordering, rendering, default-view behavior) only exist there. Rejection is a routing decision (back to schema, back to sampling, or abort), and cleanup of sample artifacts is explicitly confirmed, never silent.
9. **Bulk write** — skip-and-continue with guardrails: halts on a failure-rate threshold (gated on minimum sample size) or a consecutive-failure streak. Halts are informational, never destructive — written pages stay written; the human decides what happens next.
10. **Reconciliation** — runs unconditionally, success or halt. Groups failures by cause, pairs each pattern with a suggested fix, and emits the retry seed (failed ∪ unattempted rows) that a future retry pass would consume. Migrations converge across passes; each pass should be cheaper than the last.

### Design principles the code embodies

- **Deterministic code computes; LLMs judge.** Statistics, parsing, coercion, and pattern grouping are pure Python. The LLM is reserved for genuine judgment (type inference, semantic anomaly detection) and always operates on bounded summaries, never raw data.
- **Approval without evidence is theater.** Every checkpoint presents the artifact *and* the evidence behind it — the profile behind the schema, the coercion results behind the type choices, real rendered pages behind the final go/no-go.
- **Nodes do work; edges decide where to go.** No node halts the pipeline itself; nodes write control signals into state and the orchestrator routes. Rejections carry their rewind target.
- **Structured LLM output via forced tool use.** Type inference uses a tool `input_schema` with `tool_choice` forced — the API enforces the response shape, so there is no JSON string parsing anywhere in the pipeline.
- **Adaptive policy over binary choice.** Error handling is neither halt-on-anything nor skip-everything; it watches the failure pattern and distinguishes random noise from systematic problems. Thresholds are named constants, deliberately positioned as per-deployment configuration.
- **State carries artifacts; configuration carries policy; signals carry intent.** The full run — proposals, edits, outcomes, reports — is reconstructable from state.

### Real-world debugging this project absorbed

- **Notion's 2025 API transition to multi-source databases** broke database creation mid-build (properties silently dropped; the response shape changed from `properties` to `data_sources`). Diagnosed via a minimal isolation script (`test_notion_create.py`), resolved by pinning `notion-client==2.2.1` to match the API version the code targets — header-based version pinning alone was insufficient because the SDK's internal request shaping is coupled to its own version.
- **A silent SDK no-op on database archiving** — the SDK reported success while filtering the `archived` parameter out of the request. Diagnosed by dropping below the SDK to raw HTTP (`test_archive.py`); the agent now uses a raw `requests` call for that one operation and the SDK for everything else.
- **A non-idempotent bulk write invoked twice** (an orchestration mistake) produced duplicate pages while the agent's own audit showed a clean 100% — a concrete demonstration that state records what the agent did, not what exists in the destination. The production fix (idempotency keys on written rows) is documented below as future work.

### Running it

```
git clone https://github.com/Huang-Victor/agentic-ai-projects.git
cd agentic-ai-projects/research-agent
python -m venv venv
.\venv\Scripts\Activate        # Windows PowerShell
pip install -r requirements.txt
```

Create a `.env` in the project directory:

```
ANTHROPIC_API_KEY=<your key>
NOTION_API_KEY=<your integration token>
NOTION_PAGE_ID=<parent page id>
```

In Notion, share the parent page with your integration (page menu → Connections). Then:

```
python notion_migration_agent.py
```

Test CSVs (clean, typo-laden, mixed-format, missing-value) are in `Test Files/`. Point `csv_path` in `__main__` at any of them.

### Known limitations (deliberate scoping, not oversights)

- **Interactive checkpoints.** The CLI checkpoint assumes operator and reviewer are the same person in one session. The production design is suspend/resume — serialize state, exit, review asynchronously, resume by run ID.
- **Rejection halts rather than loops.** `rejection_return_stage` carries the rewind target, but the orchestrator currently halts on rejection instead of re-entering the target stage. The convergence loop is designed for, not wired.
- **No collective-validity check at checkpoint 1.** Individually valid edits can produce collectively invalid state (e.g., unmapping the title's source). These surface downstream rather than at `enter`.
- **Select options derive from a capped 20-value sample.** A column with more than 20 legitimate options would lose some. The fix is deriving from the full distinct set, which the profiler already computes.
- **No value normalization before coercion.** Known typo variants (flagged with their canonical value during profiling) fail coercion rather than being auto-mapped. The profile already produces the mapping a normalization step would consume.
- **Bulk write is not idempotent.** Re-invocation duplicates rows. Production fix: stamp pages with a source-row identifier and convert "write row N" into "ensure row N exists."
- **Reconciliation audits agent actions, not destination state.** Detecting drift between the two requires a verification pass that queries Notion and compares — the natural stage after reconciliation.
- **Pinned to Notion's pre-data-sources API** (`notion-client==2.2.1`, API `2022-06-28`). Upgrading to the 2025-09-03 multi-source model is a deliberate migration project across database creation, page writes, and the schema translator.

---

## Earlier projects

**`research-agent/agent.py` — ReAct agent from scratch.** Tool-calling loop built on raw Anthropic API calls: tool definitions as schemas, `while stop_reason == "tool_use"`, multiple simultaneous tool calls, capped iterations. Web search via DuckDuckGo.

**`research-agent/workflow_agent.py` — multi-node research workflow.** Specialized nodes (planner → researcher → synthesizer → writer → Notion writer) over typed shared state, a human approval checkpoint, structured output via forced tool use, and block-based Notion page writing. The architectural template the migration agent grew from.

