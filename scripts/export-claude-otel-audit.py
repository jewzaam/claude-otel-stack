#!/usr/bin/env python3
"""Export Claude Code session usage from Loki as portable session JSON.

This pull-based export works after an ephemeral sandbox has been reaped and
includes sessions which have not stopped.  It uses api_request log records,
which carry exact per-request token values, rather than Prometheus counters.
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


def fetch_events(loki_url, start, end):
    endpoint = loki_url.rstrip("/") + "/loki/api/v1/query_range"
    window_start, finish = int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)
    records = []
    # Bound each request before applying a result limit: Loki otherwise has to
    # scan the entire 30-day range and can time out before pagination begins.
    while window_start <= finish:
        window_end = min(window_start + DAY_NS - 1, finish)
        day = datetime.fromtimestamp(window_start / 1e9, tz=timezone.utc).date()
        print(f"Fetching {day}...", file=sys.stderr, flush=True)
        cursor = window_start
        window_records = 0
        while cursor <= window_end:
            params = urllib.parse.urlencode({"query": '{service_name="claude-code"} | event_name =~ "api_request|user_prompt|tool_result|skill_activated|subagent_completed"', "start": cursor, "end": window_end, "limit": PAGE_SIZE, "direction": "forward"})
            try:
                with urllib.request.urlopen(f"{endpoint}?{params}", timeout=30) as response:
                    data = json.load(response)
            except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
                day = datetime.fromtimestamp(window_start / 1e9, tz=timezone.utc).date()
                raise RuntimeError(f"Loki query failed for {day}: {exc}") from exc
            page = [(int(timestamp), item["stream"]) for item in data.get("data", {}).get("result", []) for timestamp, _body in item.get("values", [])]
            if not page:
                break
            page.sort(key=lambda item: item[0])
            records.extend(page)
            window_records += len(page)
            if len(page) < PAGE_SIZE:
                break
            cursor = page[-1][0] + 1
        print(f"  {window_records} events", file=sys.stderr, flush=True)
        window_start = window_end + 1
    return records


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def session_template(session_id):
    return {"session_id": session_id, "title": "Untitled", "project": "unknown", "date": None, "duration_ms": 0, "model": "unknown", "models": {}, "effort": "unknown", "efforts": {}, "user_turns": 0, "llm_turns": 0, "total_turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0, "skills": [], "skill_tokens_total": 0, "freeform_tokens_total": 0, "skill_tokens": defaultdict(lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0}), "skill_invocations": Counter(), "tool_calls": defaultdict(lambda: {"count": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0}), "custom_script_count": 0, "context_sizes_per_user_turn": [], "max_context_size": 0, "compaction_count": 0, "subagent_count": 0, "subagent_tokens": 0, "_contexts": [], "_first": None, "_last": None, "_models": Counter(), "_efforts": Counter(), "_requests": set(), "_subagents": set(), "_profile": "local"}


def sessions_from_events(events, project_filter=None):
    sessions = {}
    for timestamp, attrs in events:
        session_id = attrs.get("session_id")
        if not session_id:
            continue
        session = sessions.setdefault(session_id, session_template(session_id))
        session["_first"] = min(session["_first"] or timestamp, timestamp)
        session["_last"] = max(session["_last"] or timestamp, timestamp)
        project = attrs.get("project")
        if project:
            session["project"] = os.path.basename(project.rstrip("/")) or "unknown"
        session["_profile"] = attrs.get("sandbox_profile") or "local"
        event = attrs.get("event_name", "")
        if event == "user_prompt":
            if session["_contexts"]:
                session["context_sizes_per_user_turn"].append(max(session["_contexts"]))
                session["_contexts"] = []
            session["user_turns"] += 1
            if attrs.get("command_source") == "custom" and attrs.get("command_name"):
                session["skill_invocations"][attrs["command_name"]] += 1
        elif event == "tool_result":
            name = attrs.get("tool_name", "unknown")
            session["tool_calls"][name]["count"] += 1
        elif event == "skill_activated" and attrs.get("skill_name"):
            session["skill_invocations"][attrs["skill_name"]] += 1
        elif event == "subagent_completed":
            child = attrs.get("agent_id") or attrs.get("subagent_id") or str(timestamp)
            session["_subagents"].add(child)
        if event != "api_request":
            continue
        request_id = attrs.get("request_id")
        if request_id and request_id in session["_requests"]:
            continue
        if request_id:
            session["_requests"].add(request_id)
        values = {"input": as_int(attrs.get("input_tokens")), "output": as_int(attrs.get("output_tokens")), "cache_write": as_int(attrs.get("cache_creation_tokens")), "cache_read": as_int(attrs.get("cache_read_tokens"))}
        total = sum(values.values())
        session["llm_turns"] += 1
        session["duration_ms"] += as_int(attrs.get("duration_ms"))
        for key, value in values.items():
            session[f"{key}_tokens"] += value
        session["total_tokens"] += total
        session["_contexts"].append(values["input"] + values["cache_write"] + values["cache_read"])
        if attrs.get("model"):
            session["_models"][attrs["model"]] += 1
        if attrs.get("effort"):
            session["_efforts"][attrs["effort"]] += 1
        skill = attrs.get("skill_name")
        if skill:
            bucket = session["skill_tokens"][skill]
            for key, value in values.items(): bucket[key] += value
            bucket["total"] += total
        if "compact" in attrs.get("query_source", ""):
            session["compaction_count"] += 1
    output = []
    for session in sessions.values():
        if project_filter and project_filter.lower() not in session["project"].lower():
            continue
        session["date"] = datetime.fromtimestamp(session["_first"] / 1e9, tz=timezone.utc).strftime("%Y-%m-%d") if session["_first"] else None
        session["model"] = session["_models"].most_common(1)[0][0] if session["_models"] else "unknown"
        session["effort"] = session["_efforts"].most_common(1)[0][0] if session["_efforts"] else "unknown"
        session["total_turns"] = session["user_turns"] + session["llm_turns"]
        if session["_contexts"]:
            session["context_sizes_per_user_turn"].append(max(session["_contexts"]))
        session["max_context_size"] = max(session["context_sizes_per_user_turn"], default=0)
        session["models"] = dict(session["_models"])
        session["efforts"] = dict(session["_efforts"])
        session["skill_tokens"] = dict(session["skill_tokens"])
        session["skill_invocations"] = dict(session["skill_invocations"])
        session["tool_calls"] = dict(session["tool_calls"])
        session["skills"] = sorted(set(session["skill_tokens"]) | set(session["skill_invocations"]))
        session["skill_tokens_total"] = sum(item["total"] for item in session["skill_tokens"].values())
        session["freeform_tokens_total"] = session["total_tokens"] - session["skill_tokens_total"]
        session["subagent_count"] = len(session["_subagents"])
        session["title"] = f"{session['title']} (profile={session['_profile']})"
        for key in ("_contexts", "_first", "_last", "_models", "_efforts", "_requests", "_subagents", "_profile"):
            session.pop(key)
        output.append(session)
    return sorted(output, key=lambda item: item["date"] or "", reverse=True)


def report(sessions):
    daily, weekly = defaultdict(lambda: {"sessions": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0}), defaultdict(lambda: {"sessions": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0})
    models, projects = defaultdict(lambda: {"sessions": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0}), defaultdict(lambda: {"sessions": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0})
    skills, tools = defaultdict(lambda: {"total": 0, "sessions": 0, "invocations": 0}), defaultdict(lambda: {"count": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0})
    for item in sessions:
        week = datetime.strptime(item["date"], "%Y-%m-%d").strftime("%G-W%V") if item["date"] else "unknown"
        for bucket in (daily[item["date"] or "unknown"], weekly[week], models[item["model"]], projects[item["project"]]):
            bucket["sessions"] += 1
            for key, source in (("input", "input_tokens"), ("output", "output_tokens"), ("cache_write", "cache_write_tokens"), ("cache_read", "cache_read_tokens"), ("total", "total_tokens")):
                bucket[key] += item[source]
        for name, value in item["skill_tokens"].items():
            skills[name]["total"] += value["total"]
            skills[name]["sessions"] += 1
        for name, count in item["skill_invocations"].items(): skills[name]["invocations"] += count
        for name, value in item["tool_calls"].items():
            for key in ("count", "input", "output", "cache_write", "cache_read", "total"): tools[name][key] += value[key]
    rows = lambda values, name: [{name: key, **value} for key, value in sorted(values.items())]
    dates = [item["date"] for item in sessions if item["date"]]
    total = sum(item["total_tokens"] for item in sessions)
    cache_read = sum(item["cache_read_tokens"] for item in sessions)
    cache_denom = sum(item["input_tokens"] + item["cache_write_tokens"] + item["cache_read_tokens"] for item in sessions)
    skill_total = sum(item["skill_tokens_total"] for item in sessions)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "machine_uuid": "otel-export", "source": "claude", "summary": {"total_sessions": len(sessions), "total_tokens": total, "avg_tokens_per_session": total // len(sessions) if sessions else 0, "cache_hit_rate": round(cache_read / cache_denom * 100, 1) if cache_denom else 0, "skill_tokens_total": skill_total, "freeform_tokens_total": total - skill_total, "total_custom_scripts": sum(item["custom_script_count"] for item in sessions), "total_compactions": sum(item["compaction_count"] for item in sessions), "date_range": {"from": min(dates) if dates else None, "to": max(dates) if dates else None}}, "sessions": sessions, "aggregations": {"daily": rows(daily, "date"), "weekly": rows(weekly, "week"), "by_model": rows(models, "model"), "by_project": rows(projects, "project"), "by_skill": [{"skill": key, **value, "cost_per_call": round(value["total"] / value["invocations"]) if value["invocations"] else None} for key, value in sorted(skills.items())], "by_tool": [{"tool": key, "is_mcp": False, **value} for key, value in sorted(tools.items())], "by_mcp_tool": [], "cache_effectiveness": [{"date": key, "cache_hit_rate": round(value["cache_read"] / (value["input"] + value["cache_write"] + value["cache_read"]) * 100, 1) if value["input"] + value["cache_write"] + value["cache_read"] else 0} for key, value in sorted(daily.items())], "top_sessions": sorted(sessions, key=lambda item: item["total_tokens"], reverse=True)[:20], "skill_vs_freeform": {"skill": skill_total, "freeform": total - skill_total}}}


def main():
    parser = argparse.ArgumentParser(description="Export retained Claude OTEL sessions as portable session JSON")
    parser.add_argument("--loki-url", default=os.environ.get("LOKI_URL", DEFAULT_LOKI_URL))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--project")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.days <= 0: parser.error("--days must be positive")
    now = datetime.now(timezone.utc)
    sessions = sessions_from_events(fetch_events(args.loki_url, now - timedelta(days=args.days), now), args.project)
    path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report(sessions), indent=2) + "\n")
    print(f"Exported {len(sessions)} Claude sessions from Loki to {path}")


if __name__ == "__main__": main()
