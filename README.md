# elk-mcp

A lightweight, read-only MCP (Model Context Protocol) server that wraps the Elasticsearch REST API with three tools purpose-built for DLQ error analysis. Designed to be consumed by a LangGraph orchestrator as part of the Harness DLQ multi-agent system.

![CI](https://github.com/your-org/elk-mcp/actions/workflows/ci.yml/badge.svg)

---

## What it does

When a message lands in a GCP Pub/Sub Dead Letter Topic, the orchestrator needs log context before it can route the message to the right domain agent. This server provides that context through three focused tools:

| Tool | Purpose |
|---|---|
| `search_logs` | Fetch raw log entries by `trace_id` within a time window |
| `get_error_frequency` | Count how often an error type has occurred and when it was first and last seen |
| `get_trace_context` | Return a complete, LLM-ready formatted trace string (truncated at 8000 chars) |

All tools are read-only. The server never writes to Elasticsearch.

---

## Architecture

```
GCP Pub/Sub Dead Letter Topic
        │
        ▼
LangGraph Orchestrator
        │  stdio (MCP protocol)
        ▼
Elasticsearch MCP Server   ←── this repo
        │  HTTPS / ApiKey
        ▼
Elasticsearch Cluster
```

The orchestrator spawns the MCP server as a subprocess and communicates over stdin/stdout using the MCP protocol. No HTTP port is exposed.

For a detailed breakdown of data flow, agent connections, error propagation, and the GitHub webhook re-enqueue flow, see [INTEGRATION.md](./INTEGRATION.md).

---

## Requirements

- Python 3.11+
- An Elasticsearch cluster with an API key scoped to `read` on the target index patterns

---

## Installation

```bash
git clone https://github.com/your-org/elk-mcp.git
cd elk-mcp
pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

### Required

| Variable | Description |
|---|---|
| `ELASTICSEARCH_URL` | Base URL of your Elasticsearch cluster, e.g. `https://es.internal:9200` |
| `ELASTICSEARCH_API_KEY` | Base64-encoded API key (`id:api_key` encoded) |

The server exits immediately at startup if either variable is missing.

### Optional

| Variable | Default | Description |
|---|---|---|
| `ELASTICSEARCH_INDEX_PREFIX` | `platform` | Used by the orchestrator to build index pattern strings |
| `ELASTICSEARCH_MAX_HITS` | `100` | Maximum hits returned by `search_logs` |
| `ELASTICSEARCH_TRACE_MAX_CHARS` | `8000` | Maximum characters returned by `get_trace_context` |
| `DLQ_RECURRING_MAX_COUNT` | `50` | Error count threshold above which a message is classified as a storm |
| `DLQ_STORM_WINDOW` | `1h` | Elasticsearch date math window used alongside `DLQ_RECURRING_MAX_COUNT` for storm detection |

`DLQ_RECURRING_MAX_COUNT` and `DLQ_STORM_WINDOW` are consumed by the orchestrator, not the MCP server directly. Tune them per deployment to avoid false positives on queues with naturally high volume (e.g. `platform-techfin-exchanges` sustains ~27 msg/h under normal operation).

---

## Running the server

The server communicates over stdio and is normally launched by the orchestrator. To start it manually:

```bash
python mcp/elasticsearch_mcp.py
```

---

## Orchestrator integration

The LangGraph orchestrator launches the server as a subprocess:

```python
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

        context = await session.call_tool("get_trace_context", {
            "trace_id": "abc-123",
            "index": "platform-loans-*",
            "time_range": {"from": "-30m", "to": "now"},
        })
```

---

## Tool reference

### `search_logs`

```python
search_logs(
    index: str,           # e.g. "platform-loans-*"
    trace_id: str,        # trace_id or correlation_id value
    time_range: dict,     # {"from": "-30m", "to": "now"}
) -> list[dict]
```

Returns up to 100 log entries sorted by timestamp ascending. Returns `[]` for empty `trace_id` or missing index — never raises on zero results.

---

### `get_error_frequency`

```python
get_error_frequency(
    error_type: str,   # error string or exception class name
    index: str,
    window: str,       # Elasticsearch date math, e.g. "24h", "7d"
) -> dict
```

Returns:

```json
{
  "count": 42,
  "first_seen": "2025-05-10T08:00:00Z",
  "last_seen": "2025-05-10T14:32:01Z",
  "pattern": "NullPointerException in checkout flow"
}
```

Returns `count: 0` and null timestamps when no results are found.

---

### `get_trace_context`

```python
get_trace_context(
    trace_id: str,
    index: str,
    time_range: dict,
) -> str
```

Returns a formatted string ready to be injected into an LLM prompt:

```
[2025-05-10T14:32:01Z] [ERROR] [loans-service] Failed to process payment: timeout
[2025-05-10T14:32:02Z] [INFO ] [loans-service] Retrying transaction abc-123
[2025-05-10T14:32:03Z] [ERROR] [loans-service] Max retries exceeded
```

Truncated at 8000 characters. Returns `"No logs found for trace_id: <id>"` when the trace has no log entries.

---

## Error handling

| Condition | Behavior |
|---|---|
| Index not found (404) | Returns empty result, logs a warning |
| Auth failure (401/403) | Raises `PermissionError` |
| Server error (5xx) | Raises `RuntimeError` with status and body |
| Request timeout | Raises `TimeoutError("Elasticsearch timeout after 30s")` |
| Empty `trace_id` | Returns empty result immediately, no HTTP call made |

---

## Tests

All tests mock HTTP calls with `respx`. **No live Elasticsearch instance is required.**

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pip install pytest-cov
pytest tests/ -v --cov=mcp --cov-report=term-missing
```

```
tests/test_elasticsearch_mcp.py::test_search_logs_returns_hits                   PASSED
tests/test_elasticsearch_mcp.py::test_search_logs_empty_trace_id                 PASSED
tests/test_elasticsearch_mcp.py::test_search_logs_index_not_found                PASSED
tests/test_elasticsearch_mcp.py::test_get_error_frequency_with_results           PASSED
tests/test_elasticsearch_mcp.py::test_get_error_frequency_no_results             PASSED
tests/test_elasticsearch_mcp.py::test_get_trace_context_formats_correctly        PASSED
tests/test_elasticsearch_mcp.py::test_get_trace_context_truncates_at_8000_chars  PASSED
tests/test_elasticsearch_mcp.py::test_get_trace_context_no_logs                  PASSED
tests/test_elasticsearch_mcp.py::test_es_401_raises_permission_error             PASSED
tests/test_elasticsearch_mcp.py::test_es_timeout_raises_timeout_error            PASSED
```

CI runs the full suite against Python 3.11 and 3.12 on every push and pull request.

---

## Project structure

```
elk-mcp/
├── .env.example                       # environment variable template
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml                     # CI: Python 3.11 + 3.12 matrix
├── mcp/
│   └── elasticsearch_mcp.py           # MCP server — all 3 tools
├── tests/
│   ├── __init__.py
│   └── test_elasticsearch_mcp.py      # 10 tests, fully mocked
├── pyproject.toml                     # build config, pytest + mypy settings
├── requirements.txt                   # pinned dependencies
├── INTEGRATION.md                     # agent connections and data flow
└── README.md
```

---

## Changelog

### 2025-05-23

#### Fixes

**DLQ source corrected to GCP Pub/Sub**
The architecture diagram and documentation previously referenced Kafka, RabbitMQ, and SQS. The stack uses GCP Pub/Sub exclusively. All references have been updated to `GCP Pub/Sub Dead Letter Topic`.

**Error classification thresholds made configurable**
The orchestrator's error classification logic previously used hardcoded thresholds (`count >= 10` → storm). These values conflicted with the real playbook and caused false positives on high-volume queues. Thresholds are now controlled by two environment variables:

- `DLQ_RECURRING_MAX_COUNT` (default: `50`) — max count before a message is classified as a storm
- `DLQ_STORM_WINDOW` (default: `1h`) — time window for the storm count

**Dependency versions pinned**
`requirements.txt` previously used open-ended lower bounds (`mcp>=1.0.0`). All dependencies now have upper bounds to prevent silent breaking changes from major version bumps:

```
mcp>=1.0.0,<2.0.0
httpx>=0.27.0,<1.0.0
pytest>=8.0.0,<9.0.0
pytest-asyncio>=0.23.0,<1.0.0
respx>=0.21.0,<1.0.0
```

**`main()` entrypoint added to server**
`mcp/elasticsearch_mcp.py` now exposes a `main()` function in addition to the `if __name__ == "__main__"` block, making it importable and callable by packaging tooling.

#### Additions

- `.env.example` — documents all environment variables with defaults and descriptions
- `.gitignore` — covers `.env`, `__pycache__`, `.pytest_cache`, build artifacts
- `.github/workflows/ci.yml` — GitHub Actions CI running tests on Python 3.11 and 3.12
- `pyproject.toml` — standardised build config; sets `asyncio_mode = "auto"` for pytest-asyncio so `@pytest.mark.asyncio` decorators are no longer required on every test; includes mypy config
- `tests/__init__.py` — marks the tests directory as a Python package
- `INTEGRATION.md` — new section documenting the GitHub webhook → re-enqueue flow that runs after a data-fix migration PR is merged

#### Known constraint

The `mcp/` directory cannot have an `__init__.py`. The directory name collides with the installed `mcp` SDK package: adding `__init__.py` causes Python to resolve `from mcp.server.fastmcp import FastMCP` to the local directory instead of the SDK, breaking the server at import time. The directory is intentionally kept as a namespace (no `__init__.py`). If a proper package entrypoint is needed in future, rename the directory to `es_server/` or similar.

---

## License

MIT
