import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

ELASTICSEARCH_URL = os.environ["ELASTICSEARCH_URL"]
ELASTICSEARCH_API_KEY = os.environ["ELASTICSEARCH_API_KEY"]

logger = logging.getLogger("elasticsearch_mcp")

mcp = FastMCP("elasticsearch-dlq")


def get_es_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=ELASTICSEARCH_URL,
        headers={
            "Authorization": f"ApiKey {ELASTICSEARCH_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def _handle_es_error(response: httpx.Response, context: str) -> None:
    status = response.status_code
    if status in (401, 403):
        logger.error("[%s] Auth error %d: %s", context, status, response.text)
        raise PermissionError(
            f"Elasticsearch auth error {status} in {context}: {response.text}"
        )
    if status >= 500:
        logger.error("[%s] Server error %d: %s", context, status, response.text)
        raise RuntimeError(
            f"Elasticsearch server error {status} in {context}: {response.text}"
        )
    if status >= 400:
        logger.error("[%s] Client error %d: %s", context, status, response.text)
        raise RuntimeError(
            f"Elasticsearch client error {status} in {context}: {response.text}"
        )


async def _execute_search(
    client: httpx.AsyncClient, index: str, query: dict[str, Any], context: str
) -> dict[str, Any] | None:
    """Execute _search. Returns None on 404. Raises on auth/server errors."""
    url = f"/{index}/_search"
    logger.debug("[%s] POST %s", context, url)
    start = time.monotonic()
    try:
        response = await client.post(url, json=query)
    except httpx.TimeoutException:
        logger.error("[%s] Request timed out", context)
        raise TimeoutError("Elasticsearch timeout after 30s")
    except Exception:
        logger.error("[%s] Unexpected HTTP error", context, exc_info=True)
        raise

    latency_ms = (time.monotonic() - start) * 1000
    status = response.status_code

    if status == 404:
        logger.warning("[%s] Index not found (404): %s", context, index)
        return None

    _handle_es_error(response, context)

    body = response.json()
    hit_count = body.get("hits", {}).get("total", {}).get("value", 0)
    logger.debug(
        "[%s] Response %d hits=%d latency=%.1fms", context, status, hit_count, latency_ms
    )
    return body


def _ms_epoch_to_iso(ms: float) -> str:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@mcp.tool()
async def search_logs(
    index: str,
    trace_id: str,
    time_range: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Returns a list of log entries matching the trace_id within the time range.
    Each entry is a dict with at minimum: timestamp, level, message, service, trace_id.
    Returns at most 100 hits, sorted by timestamp asc.
    Returns empty list if nothing found (never raises on 0 results).
    """
    if not trace_id:
        return []

    query = {
        "query": {
            "bool": {
                "must": [
                    {
                        "bool": {
                            "should": [
                                {"term": {"trace_id.keyword": trace_id}},
                                {"term": {"correlation_id.keyword": trace_id}},
                            ]
                        }
                    },
                    {
                        "range": {
                            "@timestamp": {
                                "gte": time_range["from"],
                                "lte": time_range["to"],
                            }
                        }
                    },
                ]
            }
        },
        "sort": [{"@timestamp": {"order": "asc"}}],
        "size": 100,
    }

    async with get_es_client() as client:
        body = await _execute_search(client, index, query, "search_logs")

    if body is None:
        return []

    hits = body.get("hits", {}).get("hits", [])
    if not hits:
        logger.warning(
            "search_logs: no results for trace_id=%s in index=%s", trace_id, index
        )
        return []

    results: list[dict[str, Any]] = []
    for hit in hits:
        src = hit.get("_source", {})
        results.append({
            "timestamp": src.get("@timestamp"),
            "level": src.get("level") or src.get("log", {}).get("level"),
            "message": src.get("message"),
            "service": src.get("service") or src.get("service.name"),
            "trace_id": src.get("trace_id") or src.get("correlation_id"),
            **{k: v for k, v in src.items() if k not in {"@timestamp", "message"}},
        })

    return results


@mcp.tool()
async def get_error_frequency(
    error_type: str,
    index: str,
    window: str,
) -> dict[str, Any]:
    """
    Returns frequency metadata for the given error type.
    Output shape:
    {
        "count": int,
        "first_seen": str,   # ISO 8601
        "last_seen": str,    # ISO 8601
        "pattern": str       # most common message template found, or the error_type itself
    }
    Returns {"count": 0, "first_seen": null, "last_seen": null, "pattern": error_type}
    if no results found. Never raises on 0 results.
    """
    empty: dict[str, Any] = {
        "count": 0,
        "first_seen": None,
        "last_seen": None,
        "pattern": error_type,
    }

    query = {
        "query": {
            "bool": {
                "must": [
                    {"match": {"message": error_type}},
                    {
                        "range": {
                            "@timestamp": {
                                "gte": f"now-{window}",
                                "lte": "now",
                            }
                        }
                    },
                ]
            }
        },
        "sort": [{"@timestamp": {"order": "asc"}}],
        "size": 1,
        "aggs": {
            "last_occurrence": {"max": {"field": "@timestamp"}},
            "first_occurrence": {"min": {"field": "@timestamp"}},
            "top_messages": {
                "terms": {
                    "field": "message.keyword",
                    "size": 1,
                }
            },
        },
    }

    async with get_es_client() as client:
        body = await _execute_search(client, index, query, "get_error_frequency")

    if body is None:
        return empty

    total = body.get("hits", {}).get("total", {}).get("value", 0)
    if total == 0:
        return empty

    aggs = body.get("aggregations", {})
    first_seen_ms = aggs.get("first_occurrence", {}).get("value")
    last_seen_ms = aggs.get("last_occurrence", {}).get("value")

    top_buckets = aggs.get("top_messages", {}).get("buckets", [])
    pattern = top_buckets[0]["key"] if top_buckets else error_type

    return {
        "count": total,
        "first_seen": _ms_epoch_to_iso(first_seen_ms) if first_seen_ms is not None else None,
        "last_seen": _ms_epoch_to_iso(last_seen_ms) if last_seen_ms is not None else None,
        "pattern": pattern,
    }


@mcp.tool()
async def get_trace_context(
    trace_id: str,
    index: str,
    time_range: dict[str, str],
) -> str:
    """
    Returns a formatted string with the complete trace context, suitable for
    inclusion in an LLM prompt.

    Format:
    [2025-05-10T14:32:01Z] [ERROR] [service-name] Message text here
    [2025-05-10T14:32:02Z] [INFO]  [service-name] Message text here
    ...

    If no logs found, returns: "No logs found for trace_id: <trace_id>"
    """
    logs = await search_logs(index=index, trace_id=trace_id, time_range=time_range)

    if not logs:
        return f"No logs found for trace_id: {trace_id}"

    lines: list[str] = []
    for entry in logs:
        ts = entry.get("timestamp") or ""
        level = (entry.get("level") or "UNKNOWN").upper()
        service = entry.get("service") or "unknown"
        message = entry.get("message") or ""
        lines.append(f"[{ts}] [{level:<5}] [{service}] {message}")

    output = "\n".join(lines)
    if len(output) > 8000:
        output = output[:8000]

    return output


if __name__ == "__main__":
    mcp.run(transport="stdio")
