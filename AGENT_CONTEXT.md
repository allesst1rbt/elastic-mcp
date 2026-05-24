# Agent Context — elk-mcp Repository State

**Audience:** AI agent consuming this repo as input  
**Purpose:** Describe the current state and recent changes so the agent has accurate context without reading the full git history  
**As of:** 2025-05-23

---

## What this repo is

A Python MCP server (`mcp/elasticsearch_mcp.py`) that exposes 3 read-only tools over stdio transport. It is a subprocess of the LangGraph orchestrator. It talks to Elasticsearch via HTTPS. It never writes to Elasticsearch.

Tools:
- `search_logs(index, trace_id, time_range)` → `list[dict]` — up to 100 hits, sorted asc
- `get_error_frequency(error_type, index, window)` → `dict` — count + first_seen + last_seen + pattern
- `get_trace_context(trace_id, index, time_range)` → `str` — formatted log string, max 8000 chars

---

## Changes applied in this revision

### 1. DLQ source corrected

**Before:** architecture docs referenced `Kafka / RabbitMQ / SQS`  
**After:** corrected to `GCP Pub/Sub Dead Letter Topic` everywhere  
**Files changed:** `INTEGRATION.md`, `README.md`  
**Why it matters:** if you generate orchestrator config or infrastructure references, use `GCP Pub/Sub`, never SQS/Kafka/RabbitMQ

---

### 2. Error classification thresholds — now env-var driven

**Before (wrong):**
```
count == 0      → novel   → escalate
0 < count < 10  → recurring → route to domain agent
count >= 10     → storm   → suppress
```

**After (correct):**
```
count == 0                          → novel   → escalate to on-call
0 < count <= DLQ_RECURRING_MAX_COUNT → recurring → route to domain agent
count > DLQ_RECURRING_MAX_COUNT (within DLQ_STORM_WINDOW) → storm → suppress + page infra
```

**Default values:**
- `DLQ_RECURRING_MAX_COUNT=50`
- `DLQ_STORM_WINDOW=1h`

**Files changed:** `INTEGRATION.md`, `README.md`, `.env.example`  
**Why it matters:** the old threshold of 10 caused false storm classification on `platform-techfin-exchanges`, which sustains ~27 msg/h under normal operation. The thresholds live in the **orchestrator**, not in this MCP server. This server only returns counts — classification is the orchestrator's responsibility.

---

### 3. New files added

| File | Purpose |
|---|---|
| `.env.example` | Template for all env vars with defaults — source of truth for config |
| `.gitignore` | Excludes `.env`, `__pycache__`, `.pytest_cache`, build artifacts |
| `.github/workflows/ci.yml` | Runs `pytest tests/ -v` on Python 3.11 and 3.12 on push/PR to main |
| `pyproject.toml` | Build config; sets `asyncio_mode = "auto"` for pytest; mypy config |
| `tests/__init__.py` | Marks tests/ as a Python package |
| `mcp/elasticsearch_mcp.py` — `main()` added | Exposes `main()` so the file is importable as a module entry point |

---

### 4. Dependency versions pinned

```
mcp>=1.0.0,<2.0.0
httpx>=0.27.0,<1.0.0
pytest>=8.0.0,<9.0.0
pytest-asyncio>=0.23.0,<1.0.0
respx>=0.21.0,<1.0.0
```

**Why it matters:** `mcp>=1.0.0` without upper bound would allow a 2.x version that breaks the `FastMCP` and `stdio_server` API. Pin upper bounds when generating or editing `requirements.txt`.

---

### 5. INTEGRATION.md — re-enqueue flow documented

A new section was added: **"Connection: GitHub Webhook → Re-enqueue Flow"**

Summary for agent use:
- After a data-fix migration PR merges, GitHub fires a `pull_request` webhook (`action: closed`, `merged: true`)
- `webhooks/github_webhook.py` reads `pr_metadata` stored by the agent when the PR was opened
- It republishes the original message to the **original Pub/Sub topic** (not the Dead Letter Topic)
- If the message fails again → it returns to the Dead Letter Topic → the full DLQ lifecycle restarts
- **This MCP server is not involved in this flow.** It is only called during context enrichment (steps 2–3 of the DLQ lifecycle).

---

## Constraints the agent must not violate

| Constraint | Detail |
|---|---|
| Read-only | This server only calls `_search`. Never generate code that calls `_index`, `_bulk`, `_update`, or `_delete`. |
| No `mcp/__init__.py` | The `mcp/` directory must NOT have an `__init__.py`. It would shadow the installed `mcp` SDK and break `from mcp.server.fastmcp import FastMCP` at import time. See INTEGRATION note in README.md. |
| No hardcoded thresholds | Storm/recurring thresholds come from `DLQ_RECURRING_MAX_COUNT` and `DLQ_STORM_WINDOW` env vars. Never hardcode `10` or `50`. |
| DLQ source is Pub/Sub only | Never reference Kafka, SQS, or RabbitMQ in any generated code or docs for this project. |
| Async only | All ES calls use `httpx.AsyncClient`. Never introduce synchronous HTTP calls. |
| Python 3.11+ | Use `dict[str, Any]` not `Dict[str, Any]`. Use `X | Y` union syntax, not `Optional[X]`. |

---

## Current test status

10/10 passing. Run with:

```bash
pytest tests/ -v
```

No live Elasticsearch needed — all HTTP calls are mocked with `respx`.

---

## File layout (current)

```
elk-mcp/
├── .env.example
├── .gitignore
├── .github/workflows/ci.yml
├── mcp/
│   └── elasticsearch_mcp.py        ← MCP server, FastMCP, 3 tools + main()
├── tests/
│   ├── __init__.py
│   └── test_elasticsearch_mcp.py   ← 10 tests, respx mocks
├── pyproject.toml
├── requirements.txt
├── AGENT_CONTEXT.md                ← this file
├── INTEGRATION.md                  ← data flow, agent connections, webhook flow
├── README.md                       ← human-facing docs
└── README_elasticsearch_mcp.md     ← setup + test instructions
```
