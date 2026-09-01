#!/usr/bin/env python3
"""Export Codex hook input to the local OTLP collector."""

import json
import os
import sys
import time
import urllib.request

def parse_resource_attributes(raw):
    """Parse OTEL_RESOURCE_ATTRIBUTES (W3C Baggage-ish: comma-separated k=v)."""
    attributes = {}
    for item in raw.split(","):
        key, _, value = item.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and value:
            attributes[key] = value
    return attributes


payload = json.load(sys.stdin)
received_at = time.time()
event = payload.get("hook_event_name", "")

# Identity comes from OTEL_RESOURCE_ATTRIBUTES when the environment supplies it.
# In an OpenShell sandbox that is host knowledge written to /sandbox/.env and
# exported by the shell: host.name (the creating machine, never the container),
# sandbox.source, sandbox.openshell_name, sandbox.profile. Without them the
# recording rules cannot join a Codex session to a sandbox row, and
# claude-dashboard shows the sandbox at Unknown forever.
#
# Parsing the variable rather than reading a list of SANDBOX_* names keeps this
# in step with whatever the host decides to put in it.
resource = parse_resource_attributes(os.environ.get("OTEL_RESOURCE_ATTRIBUTES", ""))
resource["service.name"] = "codex-hook-observer"

# The environment's project wins: bin/codex-wrapper.sh appends CODEX_PROJECT to
# OTEL_RESOURCE_ATTRIBUTES, and in a sandbox .env carries the host-side sandbox
# directory. The local fallback is for a bare `codex` with no wrapper, where the
# payload cwd is all there is.
project = resource.get("project") or os.environ.get("CODEX_PROJECT") or payload.get("cwd") or ""
resource["project"] = project

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
                            {"key": key, "value": otel_value(value)}
                            for key, value in resource.items()
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
