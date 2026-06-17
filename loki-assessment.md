# Loki Recording Rules — Sub-Agent Status Bleeding Assessment

## TL;DR

**The concern is real, but only for one of the three rules.** `claude_session_permission` can be cleared by sub-agent `tool_result` events, making the dashboard think permission was granted when the main thread is still blocked. The other two rules (`claude_session_working`, `claude_session_ready`) behave correctly — sub-agent events either contribute useful signal or are naturally resolved by the auto-wake Stop cycle.

## How the rules work today

All three rules group by `session_id`. Sub-agent events carry the **same `session_id`** as the parent session (confirmed in `otel-native-telemetry.md`). Sub-agents are distinguishable only via `query_source` (e.g., `agent:builtin:general-purpose`), which the rules do not filter on.

| Rule | Left side | Right side | Expression |
|------|-----------|------------|------------|
| `claude_session_working` | — | Count of activity events in [60s] | count > 0 = WORKING |
| `claude_session_ready` | Max Stop timestamp [30m] | Max activity timestamp [30m] | Stop > activity = READY |
| `claude_session_permission` | Max PermissionRequest timestamp [30m] | Max tool_result/user_prompt timestamp [30m] | Permission > activity = PERMISSION |

## Rule-by-rule analysis

### 1. `claude_session_working` — NO PROBLEM

Counts `api_request|tool_decision|tool_result|skill_activated|subagent_completed` in the last 60s.

Sub-agent events count toward WORKING. **This is correct.** If a sub-agent is running, work IS happening on that session. The user would also see the session as active.

When all agents finish and no main-thread activity exists, the count drops to 0 within 60s. No bleed.

### 2. `claude_session_ready` — NO PROBLEM

Compares: `max(Stop timestamp)` > `max(activity timestamp)`.

Two scenarios:

**Foreground agents**: Main thread waits for agent → agent finishes → `subagent_completed` → main thread resumes → eventually Stop fires. Stop timestamp is always AFTER all agent activity. READY detects correctly.

**Background agents**: Agent runs after main thread's Stop. Agent `api_request` events have timestamps > Stop. READY evaluates to FALSE (not ready). **This is correct** — the session is not truly idle while an agent is running.

When each background agent finishes, `SubagentStop` triggers an auto-wake cycle: `UserPromptSubmit → Stop`. The new Stop timestamp supersedes the agent's last activity. READY correctly detects the session as ready after the final agent completes.

### 3. `claude_session_permission` — **BUG: SUB-AGENT EVENTS CLEAR MAIN-THREAD PERMISSION**

Compares: `max(PermissionRequest timestamp)` > `max(tool_result|user_prompt timestamp)`.

**Failure scenario:**

1. Main thread hits a permission prompt → `PermissionRequest` fires (no `agent_id`)
2. A previously-launched background agent is still running
3. Background agent executes a tool → `tool_result` event fires (same `session_id`, `query_source=agent:builtin:...`)
4. Agent's `tool_result` timestamp > main thread's `PermissionRequest` timestamp
5. Rule evaluates to FALSE → **permission state incorrectly cleared**
6. Dashboard shows WORKING (from agent activity via `claude_session_working`) instead of PERMISSION_REQUIRED

**Impact:** The user sees a permission prompt in their terminal, but the dashboard shows the session as "Working" (blue) instead of "Permission Required" (orange). If the user is monitoring multiple sessions via the dashboard and relying on the orange state to know when to act, they miss the permission prompt.

**How likely is this scenario?** It requires:
- A session running background agents (`run_in_background=true`)
- Main thread hitting a permission prompt while a background agent is still executing tools
- This is a realistic scenario with `superpowers:dispatching-parallel-agents` or any workflow that spawns background agents and then continues working

## Validation queries

### Query 1: Find sessions with concurrent permission + agent activity

Run this in Grafana Explore against Loki. Look for sessions where a `PermissionRequest` event and an agent `tool_result` event appear within the same time window with overlapping timestamps.

```logql
{service_name="claude-code"}
| event_name = "hook_execution_complete"
| hook_event = "PermissionRequest"
```

Note the `session_id` and `observed_timestamp` values. Then check for agent tool_results in the same session around that time:

```logql
{service_name="claude-code"}
| session_id = "<SESSION_ID_FROM_ABOVE>"
| event_name = "tool_result"
| query_source =~ "agent:.*"
```

If any agent `tool_result` has `observed_timestamp` > the `PermissionRequest` timestamp AND before a main-thread `tool_result`, the bug is confirmed.

### Query 2: Prometheus recording rule values at the time of permission events

Run this in Grafana Explore against Prometheus to see if `claude_session_permission` ever drops to 0 while a PermissionRequest was the most recent main-thread event:

```promql
claude_session_permission
```

Compare the time-series with the Loki PermissionRequest events to spot false negatives.

### Query 3: Direct Loki comparison (what the rule evaluates)

This replicates the PERMISSION rule logic but adds `query_source` visibility so you can see WHAT is clearing the permission state:

```logql
# Most recent PermissionRequest
max_over_time(
  {service_name="claude-code"}
  | event_name = "hook_execution_complete"
  | hook_event = "PermissionRequest"
  | unwrap observed_timestamp [30m]
) by (session_id)
```

vs.

```logql
# Most recent tool_result — split by query_source to see who clears it
max_over_time(
  {service_name="claude-code"}
  | event_name = "tool_result"
  | query_source !~ "away_summary|compact|generate_session_title"
  | unwrap observed_timestamp [30m]
) by (session_id, query_source)
```

If the `query_source` on the clearing `tool_result` starts with `agent:`, that confirms the sub-agent bleed.

### Grafana Explore Link Template

Replace `<GRAFANA_HOST>` with your Grafana URL. This opens an Explore split view with PermissionRequest events on top and agent tool_results on bottom:

```
<GRAFANA_HOST>/explore?schemaVersion=1&panes={"left":{"datasource":"P8E80F9AEF21F6940","queries":[{"expr":"{service_name=\"claude-code\"} | event_name = \"hook_execution_complete\" | hook_event = \"PermissionRequest\"","refId":"A"}]},"right":{"datasource":"P8E80F9AEF21F6940","queries":[{"expr":"{service_name=\"claude-code\"} | event_name = \"tool_result\" | query_source =~ \"agent:.*\"","refId":"A"}]}}&orgId=1
```

## Foreground vs background agent visibility

**Telemetry does not distinguish foreground from background agents.** Available fields:

| Field | Values | Present on |
|-------|--------|-----------|
| `agent_id` | UUID | Hook events (absent on main thread) |
| `agent_type` | `"general-purpose"` (only observed value) | Hook events |
| `query_source` | `agent:builtin:general-purpose`, `agent:builtin:Explore`, etc. | Activity events only |

`run_in_background` is a parameter to the Agent tool but is **not emitted in telemetry**. No `agent_mode`, no `background` flag.

**Can agents surface permission requests?** Yes. The agent state machine includes `AgentWorking → Permission Required`. Denial fires `SubagentStop` (agent dies). Approval returns to `AgentWorking`. Both main thread and agents can trigger `PermissionRequest` events.

**Why the bug still requires concurrency (background agents):** With foreground agents, the main thread is blocked waiting for the agent. It cannot fire `PermissionRequest` while a foreground agent runs. The concurrent scenario — main thread at permission prompt while agent executes tools — only happens with background agents.

## Proposed fix

The fix must handle two permission scenarios:

1. **Main-thread permission** — only main-thread `tool_result` should clear it (not agent `tool_result`)
2. **Agent permission** — agents clear their own permission via their own `tool_result`, or die via `SubagentStop`

Since telemetry doesn't expose foreground/background and `query_source` only appears on activity events (not hook events), the cleanest approach filters the clearing side to exclude agent activity:

```yaml
# Current — vulnerable to agent tool_result clearing permission
| event_name =~ "tool_result|user_prompt"
| query_source !~ "away_summary|compact|generate_session_title"

# Fixed — only main-thread events can clear permission
| event_name =~ "tool_result|user_prompt"
| query_source = "repl_main_thread"
```

Full corrected rule:

```yaml
- record: claude_session_permission
  expr: |
    max by (session_id, host_name, project) (
      max_over_time(
        {service_name="claude-code"}
        | event_name = "hook_execution_complete"
        | hook_event = "PermissionRequest"
        | unwrap observed_timestamp [30m]
      ) by (session_id, host_name, project)
    )
    >
    (
      max by (session_id, host_name, project) (
        max_over_time(
          {service_name="claude-code"}
          | event_name =~ "tool_result|user_prompt"
          | query_source = "repl_main_thread"
          | unwrap observed_timestamp [30m]
        ) by (session_id, host_name, project)
      )
      or vector(0)
    )
```

### Tradeoff: agent permission detection

This fix correctly prevents agent `tool_result` from clearing main-thread permission. But it also means agent-only permission requests (where the main thread is idle) will only be cleared when the main thread fires a `tool_result`. In practice this is acceptable because:

1. Agent permission prompts appear in the same terminal as the main session — the user sees them regardless
2. When the user grants/denies agent permission, the agent either resumes (fires its own `tool_result`) or dies (`SubagentStop` → auto-wake → main `Stop`). The auto-wake fires a main-thread `UserPromptSubmit` which would show on the clearing side
3. The `PermissionRequest` left side still picks up agent permission events (hook events carry `agent_id` but the rule doesn't filter by it), so the session correctly shows orange when an agent needs permission

The only gap: if an agent's permission is granted and it resumes working, the rule stays truthy (permission still active) until the main thread fires `tool_result` or `user_prompt`. This is a brief false positive — the session shows "Permission Required" slightly longer than necessary. Acceptable vs the current false negative (missing permission entirely).

### Should READY also be fixed?

No. The READY rule's current behavior is correct:
- If agents are still running after Stop, READY=false → correct, session is not idle
- When all agents finish, the auto-wake Stop timestamp supersedes → READY=true → correct

Including agent activity in the READY comparison is the right behavior.

## What about `claude_session_working`?

Also correct as-is. Agent events contributing to WORKING is desirable — the session IS doing work. The dashboard showing WORKING while agents run is accurate.

## Summary

| Rule | Sub-agent bleed? | Correct behavior? | Fix needed? |
|------|-----------------|-------------------|-------------|
| `claude_session_working` | Yes (by design) | Yes — agents = work | No |
| `claude_session_ready` | Yes (by design) | Yes — agents block readiness | No |
| `claude_session_permission` | **Yes (bug)** | **No — agents clear main permission** | **Yes — filter to `repl_main_thread`** |

## Telemetry gaps

| Missing field | Impact |
|---------------|--------|
| `run_in_background` on agent events | Cannot distinguish fg/bg agents in recording rules |
| `agent_id` on activity events | Cannot correlate `tool_result` to specific agent (only `query_source` type) |
| `query_source` on hook events | Cannot filter `PermissionRequest` by main vs agent on the hook side |
