#!/usr/bin/env python3
"""Export Codex hook input to the local OTLP collector."""

import json
import os
import sys
import time
import urllib.request

payload = json.load(sys.stdin)
received_at = time.time()
project = os.environ.get("CODEX_PROJECT") or payload.get("cwd") or ""
event = payload.get("hook_event_name", "")

attributes = {
    "event_name": f"codex.hook.{event or 'unknown'}",
    "session_id": payload.get("session_id", ""),
    "cwd": payload.get("cwd", ""),
    "project": project,
    "model": payload.get("model", ""),
    "hook_event_name": event,
    # Event ordering belongs in Loki recording rules, not in the hook
    # exporter. Keep the timestamp as raw telemetry so rules can determine
    # which event is current after a session goes through /cd or resumes.
    "observed_timestamp": int(received_at * 1_000_000_000),
}

def otel_value(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


body = json.dumps(payload, separators=(",", ":"))
record = {
    "timeUnixNano": str(int(received_at * 1_000_000_000)),
    "severityText": "INFO",
    "body": {"stringValue": body},
    "attributes": [
        {"key": key, "value": otel_value(value)}
        for key, value in attributes.items()
        if value != ""
    ],
}

endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318").rstrip("/")
if not endpoint.endswith("/v1/logs"):
    endpoint += "/v1/logs"

request = urllib.request.Request(
    endpoint,
    data=json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "codex-hook-observer"}},
                            {"key": "project", "value": {"stringValue": project}},
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "codex-hook-observer"},
                            "logRecords": [record],
                        }
                    ],
                }
            ]
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=3):
        pass
except Exception as exc:
    print(f"codex hook OTLP export failed: {exc}", file=sys.stderr)
