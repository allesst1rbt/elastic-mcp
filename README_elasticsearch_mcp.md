# Elasticsearch MCP Server

A standalone Python MCP server that exposes 3 read-only Elasticsearch tools for
DLQ error analysis. Used by the LangGraph orchestrator via stdio transport.

---

## Requirements

- Python 3.11+
- Dependencies in `requirements.txt`

```bash
pip install -r requirements.txt
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ELASTICSEARCH_URL` | yes | Base URL of your ES cluster, e.g. `https://es.internal:9200` |
| `ELASTICSEARCH_API_KEY` | yes | Base64-encoded API key (`id:api_key` encoded) |
| `ELASTICSEARCH_INDEX_PREFIX` | no | Default prefix for index patterns (default: `platform`) |

Export them before running:

```bash
export ELASTICSEARCH_URL=https://your-elasticsearch-host:9200
export ELASTICSEARCH_API_KEY=your_base64_api_key_here
```

---

## Running the Server

The server communicates over stdio (used by the MCP client in the orchestrator):

```bash
python mcp/elasticsearch_mcp.py
```

---

## Tools Exposed

| Tool | Description |
|---|---|
| `search_logs` | Search raw log entries by `trace_id` within a time range |
| `get_error_frequency` | Count occurrences of an error string in the last N hours |
| `get_trace_context` | Return all correlated logs as a formatted string for LLM injection |

---

## Running Tests

```bash
pytest tests/test_elasticsearch_mcp.py -v
```

All 10 tests use `respx` to mock HTTP calls — no live Elasticsearch needed.

---

## Orchestrator Integration

The LangGraph orchestrator launches this server as a subprocess via `StdioServerParameters`:

```python
from mcp import StdioServerParameters

server_params = StdioServerParameters(
    command="python",
    args=["mcp/elasticsearch_mcp.py"],
    env={
        "ELASTICSEARCH_URL": os.environ["ELASTICSEARCH_URL"],
        "ELASTICSEARCH_API_KEY": os.environ["ELASTICSEARCH_API_KEY"],
    },
)
```
