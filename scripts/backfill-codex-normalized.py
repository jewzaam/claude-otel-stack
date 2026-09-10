#!/usr/bin/env python3
"""Backfill codex_normalized_tokens_by_project_v2 from Loki into Prometheus.

The script deliberately rebuilds the metric from raw events.  It is safe to
run with --dry-run first; no Prometheus request is made in that mode.

Example:
  python3 scripts/backfill-codex-normalized.py \
    --loki-url http://loki:3100 \
    --prometheus-url http://prometheus:9090 \
    --start '2026-09-01T00:00:00Z' --end '2026-09-10T00:00:00Z' \
    --dry-run

Prometheus remote-write accepts historical samples only when its configured
out-of-order window permits them.  The script never deletes existing data.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable


MODEL_RATES = {
    "gpt-5.6-sol": (4.0, 0.4, 5.0, 20.0),
    "gpt-5.6-terra": (2.0, 0.2, 2.5, 10.0),
    "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.0),
}
TOKEN_FIELDS = (
    "input_token_count",
    "cached_token_count",
    "cache_write_token_count",
    "output_token_count",
)
DEFAULT_METRIC = "codex_normalized_tokens_by_project_v2"


def parse_time(value: str) -> int:
    """Return a UTC nanosecond timestamp from ISO-8601 or epoch seconds."""
    try:
        return int(float(value) * 1_000_000_000)
    except ValueError:
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)


def clean_project(project: str, sandbox_source: str) -> str:
    if sandbox_source:
        return f"~/sandboxes/{sandbox_source}"
    result = re.sub(r"^(/home/[^/]+|/Users/[^/]+)", "~", project)
    return re.sub(r"/[^/]*worktree[^/]*/.*$", "", result)


def post_loki(url: str, query: str, start: int, end: int, limit: int) -> list[dict]:
    endpoint = url.rstrip("/") + "/loki/api/v1/query_range"
    form = urllib.parse.urlencode(
        {
            "query": query,
            "start": str(start),
            "end": str(end),
            "limit": str(limit),
            "direction": "forward",
        }
    ).encode()
    request = urllib.request.Request(endpoint, data=form, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"Loki query failed: {payload}")
    return payload.get("data", {}).get("result", [])


def iter_events(url: str, query: str, start: int, end: int, chunk_ns: int) -> Iterable[tuple[dict, int]]:
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + chunk_ns, end)
        streams = post_loki(url, query, cursor, chunk_end, 5000)
        for stream in streams:
            labels = stream.get("stream", {})
            for timestamp, _line in stream.get("values", []):
                yield labels, int(timestamp)
        cursor = chunk_end


def varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def field_bytes(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def label(name: str, value: str) -> bytes:
    return field_bytes(1, name.encode()) + field_bytes(2, value.encode())


def sample(value: float, timestamp_ms: int) -> bytes:
    return varint(9) + struct.pack("<d", value) + varint(16) + varint(timestamp_ms)


def timeseries(labels: dict[str, str], samples: list[tuple[float, int]]) -> bytes:
    payload = bytearray()
    for name, value in sorted(labels.items()):
        payload += field_bytes(1, label(name, value))
    for value, timestamp in samples:
        payload += field_bytes(2, sample(value, timestamp))
    return bytes(payload)


def snappy_literal(data: bytes) -> bytes:
    """Encode a byte string as a valid raw Snappy block of literals only."""
    output = bytearray(varint(len(data)))
    offset = 0
    while offset < len(data):
        length = min(len(data) - offset, 65536)
        n = length - 1
        extra = max(0, (n.bit_length() + 7) // 8 - 1)
        if length <= 60:
            output.append(n << 2)
        else:
            output.append((59 + extra + 1) << 2)
            output.extend(n.to_bytes(extra + 1, "little"))
        output.extend(data[offset : offset + length])
        offset += length
    return bytes(output)


def remote_write(url: str, series: list[bytes]) -> None:
    body = bytearray()
    for item in series:
        body += field_bytes(1, item)
    request = urllib.request.Request(url.rstrip("/") + "/api/v1/write", data=snappy_literal(body), method="POST")
    request.add_header("Content-Type", "application/x-protobuf")
    request.add_header("Content-Encoding", "snappy")
    request.add_header("X-Prometheus-Remote-Write-Version", "0.1.0")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status >= 300:
                raise RuntimeError(f"Prometheus remote write failed: HTTP {response.status}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Prometheus remote write failed: HTTP {error.code}: {detail or error.reason}"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loki-url", required=True)
    parser.add_argument("--prometheus-url")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", default=str(time.time()))
    parser.add_argument("--metric", default=DEFAULT_METRIC)
    parser.add_argument("--chunk-hours", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.prometheus_url:
        parser.error("--prometheus-url is required unless --dry-run is used")

    start = parse_time(args.start)
    end = parse_time(args.end)
    chunk_ns = int(args.chunk_hours * 3_600 * 1_000_000_000)
    project_by_conversation: dict[str, str] = {}
    hook_query = '{service_name="codex-hook-observer"} | conversation_id != ""'
    for labels, _timestamp in iter_events(args.loki_url, hook_query, start, end, chunk_ns):
        conversation = labels.get("conversation_id", "")
        project = clean_project(labels.get("project", ""), labels.get("sandbox_source", ""))
        if conversation and project:
            project_by_conversation[conversation] = project

    buckets: defaultdict[tuple[str, str, int], float] = defaultdict(float)
    codex_query = '{service_name="codex_cli_rs"} | event_name="codex.sse_event" | event_kind="response.completed"'
    event_count = 0
    for labels, timestamp_ns in iter_events(args.loki_url, codex_query, start, end, chunk_ns):
        model = labels.get("model", "")
        rates = MODEL_RATES.get(model)
        if not rates:
            continue
        conversation = labels.get("conversation_id", "")
        project = clean_project(labels.get("project", ""), labels.get("sandbox_source", ""))
        if not project:
            project = project_by_conversation.get(conversation, "")
        if not project or not conversation:
            continue
        normalized = sum(float(labels.get(field, 0) or 0) * rate / 4.0 for field, rate in zip(TOKEN_FIELDS, rates))
        minute_ms = (timestamp_ns // 60_000_000_000) * 60_000
        buckets[(project, conversation, minute_ms)] += normalized
        event_count += 1

    grouped: defaultdict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    for (project, conversation, minute_ms), value in sorted(buckets.items()):
        grouped[(project, conversation)].append((value, minute_ms))
    series = [
        timeseries({"__name__": args.metric, "project_clean": project, "conversation_id": conversation}, samples)
        for (project, conversation), samples in grouped.items()
    ]
    total = sum(value for samples in grouped.values() for value, _timestamp in samples)
    print(f"events={event_count} series={len(series)} samples={sum(map(len, grouped.values()))}")
    print(f"normalized_tokens={total:.2f} usd={total * 4 / 1_000_000:.6f}")
    if not args.dry_run:
        remote_write(args.prometheus_url, series)
        print("remote_write=ok")
    else:
        print("remote_write=skipped (dry-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
