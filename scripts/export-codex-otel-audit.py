#!/usr/bin/env python3
"""Export Codex session activity from Loki as portable session JSON.

Designed for ephemeral OpenShell sandboxes. Sessions are rebuilt from durable
hook logs, so neither SessionEnd nor Stop is required. Native Codex token
metrics omit session_id, therefore per-session token fields stay unavailable
rather than being wrongly assigned when sessions overlap.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_LOKI_URL = "http://127.0.0.1:3100"
PAGE_SIZE = 1000
DAY_NS = 86_400_000_000_000


def get_json(url, params):
    try:
        with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=30) as response:
            return json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Loki query failed: {exc}") from exc


def fetch_events(loki_url, start, end):
    """Get every retained hook event in time order, paging the Loki API."""
    window_start = int(start.timestamp() * 1_000_000_000)
    finish = int(end.timestamp() * 1_000_000_000)
    endpoint = loki_url.rstrip("/") + "/loki/api/v1/query_range"
    records = []
    while window_start <= finish:
        window_end = min(window_start + DAY_NS - 1, finish)
        day = datetime.fromtimestamp(window_start / 1e9, tz=timezone.utc).date()
        print(f"Fetching {day}...", file=sys.stderr, flush=True)
        cursor = window_start
        window_records = 0
        while cursor <= window_end:
            response = get_json(endpoint, {"query": '{service_name=~"codex-hook-observer|codex_cli_rs"}', "start": cursor, "end": window_end, "limit": PAGE_SIZE, "direction": "forward"})
            if response.get("status") != "success":
                raise RuntimeError(response.get("error", "unsuccessful Loki response"))
            page = []
            for result in response.get("data", {}).get("result", []):
                for timestamp, body in result.get("values", []):
                    try:
                        payload = json.loads(body)
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    page.append((int(timestamp), result.get("stream", {}), payload))
            if not page:
                break
            page.sort(key=lambda item: item[0])
            records.extend(page)
            window_records += len(page)
            if len(page) < PAGE_SIZE:
                break
            cursor = page[-1][0] + 1  # range boundaries are inclusive
        print(f"  {window_records} events", file=sys.stderr, flush=True)
        window_start = window_end + 1
    return records


def empty_session(session_id):
    return {"session_id": session_id, "title": "Untitled", "project": "unknown", "project_path": None, "date": None, "timestamp_first": None, "timestamp_last": None, "duration_ms": 0, "model": "unknown", "effort": "unknown", "user_turns": 0, "llm_turns": 0, "total_turns": 0, "command_count": 0, "web_search_count": 0, "reasoning_event_count": 0, "agent_message_count": 0, "compaction_count": 0, "error_count": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0, "token_usage_available": False, "event_types": Counter(), "_models": Counter(), "_tool_ids": set(), "_profile": "local"}


def iso_timestamp(nanoseconds):
    return datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=timezone.utc).isoformat()


def sessions_from_events(events, project_filter=None):
    sessions = {}
    # Native Codex logs have conversation_id and response.completed usage. A
    # hook log is only a fallback for older sessions; mixing both double-counts
    # prompts and tool calls.
    native_sessions = {
        stream.get("conversation_id")
        for _timestamp, stream, _payload in events
        if stream.get("service_name") == "codex_cli_rs" and stream.get("conversation_id")
    }
    for timestamp, stream, payload in events:
        native = stream.get("service_name") == "codex_cli_rs"
        session_id = stream.get("conversation_id") if native else (stream.get("session_id") or payload.get("session_id"))
        if not session_id:
            continue
        if not native and session_id in native_sessions:
            continue
        session = sessions.setdefault(session_id, empty_session(session_id))
        observed = iso_timestamp(timestamp)
        session["timestamp_first"] = min(session["timestamp_first"] or observed, observed)
        session["timestamp_last"] = max(session["timestamp_last"] or observed, observed)
        project_path = stream.get("project") or payload.get("cwd")
        if project_path:
            session["project_path"] = project_path
            session["project"] = os.path.basename(project_path.rstrip("/")) or "unknown"
        session["_profile"] = stream.get("sandbox_profile") or "local"
        model = stream.get("model") or payload.get("model")
        if model:
            session["_models"][model] += 1
        if native:
            event = stream.get("event_name", "unknown")
            session["event_types"][event] += 1
            effort = stream.get("model_reasoning_effort")
            if effort:
                session["effort"] = effort
            if event == "codex.user_prompt":
                session["user_turns"] += 1
            elif event == "codex.tool_result":
                tool = stream.get("tool_name", "unknown")
                session["command_count"] += 1 if tool in ("exec", "exec_command", "shell", "bash") else 0
                session["web_search_count"] += 1 if "web" in tool or "search" in tool else 0
                session["error_count"] += 1 if stream.get("success") == "false" else 0
            elif event == "codex.sse_event":
                kind = stream.get("event_kind", "")
                if kind == "response.completed":
                    # One completion event represents one model response. Its
                    # total is authoritative: input includes cached/context
                    # components and must not be recomputed by summing fields.
                    session["llm_turns"] += 1
                    session["input_tokens"] += int(stream.get("input_token_count", 0))
                    session["output_tokens"] += int(stream.get("output_token_count", 0))
                    session["cache_read_tokens"] += int(stream.get("cached_token_count", 0))
                    session["cache_write_tokens"] += int(stream.get("cache_write_token_count", 0))
                    session["reasoning_tokens"] += int(stream.get("reasoning_token_count", 0))
                    session["total_tokens"] += int(stream.get("tool_token_count", 0))
                    session["token_usage_available"] = True
                elif "reasoning" in kind:
                    session["reasoning_event_count"] += 1
            elif event in ("codex.task_compact", "codex.task.compact"):
                session["compaction_count"] += 1
            elif event in ("codex.multi_agent_spawn", "codex.multi_agent.spawn"):
                session["agent_message_count"] += 1
            continue
        event = stream.get("hook_event_name") or payload.get("hook_event_name") or "unknown"
        session["event_types"][event] += 1
        if event == "UserPromptSubmit":
            session["user_turns"] += 1
        elif event == "PreCompact":
            session["compaction_count"] += 1
        elif event == "SubagentStart":
            session["agent_message_count"] += 1
        elif event == "PreToolUse":
            tool_id = payload.get("tool_use_id")
            if tool_id and tool_id in session["_tool_ids"]:
                continue
            if tool_id:
                session["_tool_ids"].add(tool_id)
            tool_name = payload.get("tool_name", "").lower()
            if tool_name in ("bash", "shell", "exec", "exec_command"):
                session["command_count"] += 1
            if "web" in tool_name or "search" in tool_name:
                session["web_search_count"] += 1
        elif event == "PostToolUse" and stream.get("detected_level") == "error":
            session["error_count"] += 1

    output = []
    for session in sessions.values():
        if project_filter and project_filter.lower() not in session["project"].lower():
            continue
        session["date"] = session["timestamp_first"][:10] if session["timestamp_first"] else None
        if session["timestamp_first"] and session["timestamp_last"]:
            session["duration_ms"] = int((datetime.fromisoformat(session["timestamp_last"]) - datetime.fromisoformat(session["timestamp_first"])).total_seconds() * 1000)
        session["model"] = session["_models"].most_common(1)[0][0] if session["_models"] else "unknown"
        session["total_turns"] = session["user_turns"] + session["llm_turns"]
        session["title"] = f"{session['title']} (profile={session['_profile']})"
        session["event_types"] = dict(session["event_types"])
        session.pop("_models")
        session.pop("_tool_ids")
        session.pop("_profile")
        output.append(session)
    return sorted(output, key=lambda item: item["timestamp_first"] or "", reverse=True)


def aggregations(sessions):
    fields = ("user_turns", "llm_turns", "total_turns", "command_count", "web_search_count", "duration_ms", "total_tokens")
    def bucket(): return {"sessions": 0, **{field: 0 for field in fields}}
    daily, models, projects, event_types = defaultdict(bucket), defaultdict(bucket), defaultdict(bucket), Counter()
    for session in sessions:
        for target in (daily[session["date"] or "unknown"], models[session["model"]], projects[session["project"]]):
            target["sessions"] += 1
            for field in fields: target[field] += session[field]
        event_types.update(session["event_types"])
    def rows(values, name): return sorted(({name: key, **value} for key, value in values.items()), key=lambda value: value[name])
    return {"daily": rows(daily, "date"), "weekly": [], "by_model": rows(models, "model"), "by_effort": [{"effort": "unknown", "sessions": len(sessions)}] if sessions else [], "by_project": rows(projects, "project"), "by_event_type": [{"event_type": key, "count": value} for key, value in event_types.most_common()], "top_sessions": []}


def make_report(sessions):
    dates = [item["date"] for item in sessions if item["date"]]
    clean_sessions = []
    for session in sessions:
        item = dict(session)
        # Omit local-path internals from the portable output.
        for key in ("project_path", "timestamp_first", "timestamp_last", "event_types"):
            item.pop(key, None)
        clean_sessions.append(item)
    total_turns = sum(item["total_turns"] for item in sessions)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "machine_uuid": "otel-export", "source": "codex", "summary": {"total_sessions": len(sessions), "total_user_turns": sum(item["user_turns"] for item in sessions), "total_llm_turns": sum(item["llm_turns"] for item in sessions), "total_turns": total_turns, "avg_turns_per_session": total_turns // len(sessions) if sessions else 0, "total_commands": sum(item["command_count"] for item in sessions), "total_web_searches": sum(item["web_search_count"] for item in sessions), "total_reasoning_events": sum(item["reasoning_event_count"] for item in sessions), "total_duration_ms": sum(item["duration_ms"] for item in sessions), "total_tokens": sum(item["total_tokens"] for item in sessions), "token_usage_available": any(item["token_usage_available"] for item in sessions), "date_range": {"from": min(dates) if dates else None, "to": max(dates) if dates else None}}, "sessions": clean_sessions, "aggregations": aggregations(sessions)}


def main():
    parser = argparse.ArgumentParser(description="Export retained Codex OTEL sessions as portable session JSON")
    parser.add_argument("--loki-url", default=os.environ.get("LOKI_URL", DEFAULT_LOKI_URL))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--project")
    parser.add_argument("--output", required=True, metavar="FILE")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    end = datetime.now(timezone.utc)
    try:
        sessions = sessions_from_events(fetch_events(args.loki_url, end - timedelta(days=args.days), end), args.project)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(make_report(sessions), indent=2) + "\n")
    print(f"Exported {len(sessions)} Codex sessions from Loki to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
