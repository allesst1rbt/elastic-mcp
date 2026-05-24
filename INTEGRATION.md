# Elasticsearch MCP Server — Integration & Agent Connections

## System Overview

The Elasticsearch MCP server is a read-only data enrichment layer inside the **Harness DLQ Multi-Agent System**. It sits between the LangGraph orchestrator and the Elasticsearch cluster, exposing three focused tools that agents call to retrieve log context before making routing or analysis decisions.

```
┌─────────────────────────────────────────────────────────────────┐
│                    DLQ Event Source                             │
│            (GCP Pub/Sub Dead Letter Topic)                      │
└────────────────────────┬────────────────────────────────────────┘
                         │  raw DLQ message
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                  LangGraph Orchestrator                         │
│                  orchestrator/orchestrator.py                   │
│                                                                 │
│  1. Receives DLQ message                                        │
│  2. Calls MCP tools to enrich context        ◄──────────────┐  │
│  3. Routes enriched payload to domain agent                  │  │
└──────────────┬──────────────────────────────────────────────┘  │
               │ stdio (MCP protocol)                             │
               ▼                                                  │
┌─────────────────────────────────────────────────────────────────┤
│              Elasticsearch MCP Server                           │
│              mcp/elasticsearch_mcp.py                           │
│                                                                 │
│   • search_logs          → raw log entries by trace_id          │
│   • get_error_frequency  → recurrence count + first/last seen   │
│   • get_trace_context    → LLM-ready formatted trace string     │
└──────────────┬──────────────────────────────────────────────────┘
               │ HTTPS / ApiKey auth
               ▼
┌─────────────────────────────────────────────────────────────────┐
│               Elasticsearch Cluster                             │
│               (read-only — _search only)                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Connection: Orchestrator → MCP Server

The orchestrator launches the MCP server as a **subprocess** and communicates over stdin/stdout using the MCP protocol (JSON-RPC 2.0 framed messages). No HTTP port is opened; the transport is entirely local.

```python
# orchestrator/orchestrator.py
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server_params = StdioServerParameters(
    command="python",
    args=["mcp/elasticsearch_mcp.py"],
    env={
        "ELASTICSEARCH_URL": os.environ["ELASTICSEARCH_URL"],
        "ELASTICSEARCH_API_KEY": os.environ["ELASTICSEARCH_API_KEY"],
    },
)

async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        logs = await session.call_tool("search_logs", {
            "index": "platform-loans-*",
            "trace_id": dlq_message["trace_id"],
            "time_range": {"from": "-30m", "to": "now"},
        })
```

### What the orchestrator does with each tool

| Tool | When it is called | What the orchestrator does with the result |
|---|---|---|
| `search_logs` | Immediately after receiving a DLQ message | Checks for upstream errors or retries in the trace; uses the hit count to decide retry vs. escalate |
| `get_error_frequency` | After parsing the error type from the DLQ payload | Compares count against thresholds to classify the error as novel, recurring, or storm |
| `get_trace_context` | Before calling any domain sub-agent | Injects the returned string directly into the sub-agent's system or user prompt |

---

## Connection: MCP Server → Elasticsearch

All three tools make POST requests to `/{index}/_search` using `httpx.AsyncClient`. The server is **read-only by design** — it never calls `_index`, `_bulk`, `_update`, or `_delete`.

```
MCP Server                        Elasticsearch
    │                                   │
    │  POST /platform-loans-*/_search   │
    │  Authorization: ApiKey <key>      │
    │  Content-Type: application/json   │
    │──────────────────────────────────►│
    │                                   │
    │  200 OK { hits: [...] }           │
    │◄──────────────────────────────────│
```

**Auth**: A single API key is shared across all three tools. The key should be scoped to `read` privilege on the relevant index patterns only.

**Index patterns**: Index names are passed by the orchestrator per call — they are never hardcoded in the MCP server. The orchestrator derives them from the DLQ message metadata (e.g., `platform-{domain}-*`).

---

## Connection: MCP Server → Domain Sub-Agents (indirect)

The MCP server does not call domain agents directly. Its output flows through the orchestrator, which injects it into the agent's prompt or state before invocation.

```
get_trace_context result
        │
        ▼
┌────────────────────────────────────────────┐
│  Orchestrator builds agent input           │
│                                            │
│  system: "You are the Loans agent..."      │
│  user:   <DLQ payload>                     │
│          <trace context string injected>   │
└──────────────────┬─────────────────────────┘
                   │
       ┌───────────┼───────────┐
       ▼           ▼           ▼
  Loans Agent  Orders Agent  Auth Agent
  (sub-agent)  (sub-agent)   (sub-agent)
```

The orchestrator selects the sub-agent based on the DLQ message topic/queue name and the error classification returned by `get_error_frequency`.

---

## Data Flow: Full DLQ Message Lifecycle

```
1. DLQ message arrives
      │
      ├─► extract: trace_id, error_type, domain, index_pattern
      │
2. search_logs(index, trace_id, time_range="-30m..now")
      │   returns: list of log dicts
      │
3. get_error_frequency(error_type, index, window="24h")
      │   returns: {count, first_seen, last_seen, pattern}
      │
4. classify (thresholds read from orchestrator env vars):
      │   count == 0                                    → novel error → escalate to on-call
      │   0 < count <= DLQ_RECURRING_MAX_COUNT          → recurring   → route to domain agent
      │   count > DLQ_RECURRING_MAX_COUNT (in window)  → error storm → suppress + page infra
      │
      │   defaults: DLQ_RECURRING_MAX_COUNT=50, DLQ_STORM_WINDOW=1h
      │   override via env to tune per-queue (e.g. platform-techfin-exchanges
      │   has steady ~27 msg/h and should NOT trigger storm at default threshold)
      │
5. get_trace_context(trace_id, index, time_range)
      │   returns: formatted string ≤ 8000 chars
      │
6. build domain agent prompt:
      │   system: domain instructions
      │   user:   DLQ payload + trace context
      │
7. call domain sub-agent → remediation decision
```

---

## Environment Variables by Component

| Variable | Set by | Consumed by |
|---|---|---|
| `ELASTICSEARCH_URL` | deployment / CI secret | MCP server (required) |
| `ELASTICSEARCH_API_KEY` | deployment / CI secret | MCP server (required) |
| `ELASTICSEARCH_INDEX_PREFIX` | orchestrator config | orchestrator (used to build index pattern strings passed to tools) |
| `DLQ_RECURRING_MAX_COUNT` | orchestrator config | orchestrator (default: `50`) — upper bound for "recurring" classification |
| `DLQ_STORM_WINDOW` | orchestrator config | orchestrator (default: `1h`) — Elasticsearch date math window for storm detection |

The MCP server reads `ELASTICSEARCH_URL` and `ELASTICSEARCH_API_KEY` at startup. If either is missing, the process exits immediately with `KeyError` before serving any requests.

---

## Error Propagation to the Orchestrator

MCP tool errors surface as `McpError` exceptions on the client side. The orchestrator should handle these categories:

| MCP server raises | Orchestrator should |
|---|---|
| `PermissionError` (401/403) | Halt pipeline, alert ops — API key is invalid or expired |
| `TimeoutError` | Retry with exponential backoff (max 3 attempts), then escalate |
| `RuntimeError` (5xx) | Retry once, then route DLQ message to a dead-letter-of-last-resort queue |
| Empty result (`[]` / `count: 0`) | Continue normally — treat as no prior context available |

---

## Scaling Considerations

- The MCP server is **stateless** — one subprocess per orchestrator worker is safe.
- For high-throughput DLQ pipelines, run multiple orchestrator workers, each with its own MCP subprocess. No shared state.
- `get_trace_context` truncates output at 8000 characters. For traces longer than that, the orchestrator can call `search_logs` directly and paginate by adjusting `time_range`.
- The 100-hit cap in `search_logs` is intentional to protect context window size. If the orchestrator needs more, it should narrow the `time_range` rather than raise the cap.

---

## Connection: GitHub Webhook → Re-enqueue Flow

After a data-fix Alembic migration PR is merged, the re-enqueue flow runs:

1. GitHub fires `pull_request` webhook with `action: closed` + `merged: true`
2. `webhooks/github_webhook.py` identifies the DLQ message via `pr_metadata`
   stored by the agent when opening the PR
3. Webhook handler calls MCP GCloud to republish the original message
   to the original Pub/Sub topic (NOT the Dead Letter Topic)
4. Pub/Sub consumer reprocesses the message with the migration already applied
5. If the message fails again → returns to Dead Letter Topic → system restarts from step 2

The Elasticsearch MCP server is **not involved** in this flow.
It is only called during context enrichment (steps 2–3 of the DLQ lifecycle above).
