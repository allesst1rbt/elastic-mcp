import os
import sys
import pathlib

# Set env vars before the module is imported (it reads them at import time).
os.environ.setdefault("ELASTICSEARCH_URL", "https://es.test:9200")
os.environ.setdefault("ELASTICSEARCH_API_KEY", "dGVzdDp0ZXN0")

# Add the local mcp/ directory to sys.path so that elasticsearch_mcp can be
# imported directly without conflicting with the installed `mcp` SDK package.
_mcp_dir = str(pathlib.Path(__file__).parent.parent / "mcp")
if _mcp_dir not in sys.path:
    sys.path.insert(0, _mcp_dir)

import elasticsearch_mcp as es_mcp  # noqa: E402

import pytest
import respx
import httpx

SEARCH_URL = "https://es.test:9200"
TIME_RANGE = {"from": "-30m", "to": "now"}


def _make_hits(n: int) -> dict:
    hits = [
        {
            "_source": {
                "@timestamp": f"2025-05-10T14:32:{i:02d}Z",
                "level": "ERROR",
                "message": f"Something went wrong #{i}",
                "service": "loans-service",
                "trace_id": "abc123",
            }
        }
        for i in range(n)
    ]
    return {
        "hits": {
            "total": {"value": n},
            "hits": hits,
        }
    }


def _make_aggs_response(count: int, first_ms: float, last_ms: float, pattern: str) -> dict:
    return {
        "hits": {
            "total": {"value": count},
            "hits": [],
        },
        "aggregations": {
            "first_occurrence": {"value": first_ms},
            "last_occurrence": {"value": last_ms},
            "top_messages": {
                "buckets": [{"key": pattern, "doc_count": count}]
            },
        },
    }


# ---------------------------------------------------------------------------
# search_logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_search_logs_returns_hits():
    respx.post(f"{SEARCH_URL}/platform-loans-*/_search").mock(
        return_value=httpx.Response(200, json=_make_hits(3))
    )
    result = await es_mcp.search_logs(
        index="platform-loans-*",
        trace_id="abc123",
        time_range=TIME_RANGE,
    )
    assert len(result) == 3
    assert result[0]["trace_id"] == "abc123"


@pytest.mark.asyncio
async def test_search_logs_empty_trace_id():
    # Must not make any HTTP call — no respx mock needed
    result = await es_mcp.search_logs(
        index="platform-loans-*",
        trace_id="",
        time_range=TIME_RANGE,
    )
    assert result == []


@pytest.mark.asyncio
@respx.mock
async def test_search_logs_index_not_found():
    respx.post(f"{SEARCH_URL}/platform-loans-*/_search").mock(
        return_value=httpx.Response(404, json={"error": "index_not_found_exception"})
    )
    result = await es_mcp.search_logs(
        index="platform-loans-*",
        trace_id="abc123",
        time_range=TIME_RANGE,
    )
    assert result == []


# ---------------------------------------------------------------------------
# get_error_frequency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_error_frequency_with_results():
    first_ms = 1_715_347_921_000.0   # 2024-05-10T14:32:01Z
    last_ms = 1_715_347_940_000.0    # ~19s later
    respx.post(f"{SEARCH_URL}/platform-customers-*/_search").mock(
        return_value=httpx.Response(
            200,
            json=_make_aggs_response(42, first_ms, last_ms, "NullPointerException in checkout"),
        )
    )
    result = await es_mcp.get_error_frequency(
        error_type="NullPointerException",
        index="platform-customers-*",
        window="24h",
    )
    assert result["count"] == 42
    assert result["first_seen"] is not None
    assert result["last_seen"] is not None
    assert "NullPointerException" in result["pattern"]


@pytest.mark.asyncio
@respx.mock
async def test_get_error_frequency_no_results():
    respx.post(f"{SEARCH_URL}/platform-customers-*/_search").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": {"total": {"value": 0}, "hits": []},
                "aggregations": {
                    "first_occurrence": {"value": None},
                    "last_occurrence": {"value": None},
                    "top_messages": {"buckets": []},
                },
            },
        )
    )
    result = await es_mcp.get_error_frequency(
        error_type="UnknownError",
        index="platform-customers-*",
        window="1h",
    )
    assert result["count"] == 0
    assert result["first_seen"] is None
    assert result["last_seen"] is None
    assert result["pattern"] == "UnknownError"


# ---------------------------------------------------------------------------
# get_trace_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_get_trace_context_formats_correctly():
    respx.post(f"{SEARCH_URL}/platform-loans-*/_search").mock(
        return_value=httpx.Response(200, json=_make_hits(2))
    )
    result = await es_mcp.get_trace_context(
        trace_id="abc123",
        index="platform-loans-*",
        time_range=TIME_RANGE,
    )
    assert result.startswith("[")
    assert "loans-service" in result


@pytest.mark.asyncio
@respx.mock
async def test_get_trace_context_truncates_at_8000_chars():
    # Build a payload with 500 entries; each log line will be well over 16 chars
    hits = [
        {
            "_source": {
                "@timestamp": "2025-05-10T14:32:00Z",
                "level": "ERROR",
                "message": "x" * 100,
                "service": "big-service",
                "trace_id": "abc123",
            }
        }
        for _ in range(500)
    ]
    body = {"hits": {"total": {"value": 500}, "hits": hits}}
    respx.post(f"{SEARCH_URL}/platform-loans-*/_search").mock(
        return_value=httpx.Response(200, json=body)
    )
    result = await es_mcp.get_trace_context(
        trace_id="abc123",
        index="platform-loans-*",
        time_range=TIME_RANGE,
    )
    assert len(result) <= 8000


@pytest.mark.asyncio
@respx.mock
async def test_get_trace_context_no_logs():
    respx.post(f"{SEARCH_URL}/platform-loans-*/_search").mock(
        return_value=httpx.Response(200, json=_make_hits(0))
    )
    result = await es_mcp.get_trace_context(
        trace_id="missing-trace",
        index="platform-loans-*",
        time_range=TIME_RANGE,
    )
    assert result == "No logs found for trace_id: missing-trace"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_es_401_raises_permission_error():
    respx.post(f"{SEARCH_URL}/platform-loans-*/_search").mock(
        return_value=httpx.Response(401, json={"error": "missing authentication"})
    )
    with pytest.raises(PermissionError, match="401"):
        await es_mcp.search_logs(
            index="platform-loans-*",
            trace_id="abc123",
            time_range=TIME_RANGE,
        )


@pytest.mark.asyncio
@respx.mock
async def test_es_timeout_raises_timeout_error():
    respx.post(f"{SEARCH_URL}/platform-loans-*/_search").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    with pytest.raises(TimeoutError, match="30s"):
        await es_mcp.search_logs(
            index="platform-loans-*",
            trace_id="abc123",
            time_range=TIME_RANGE,
        )
