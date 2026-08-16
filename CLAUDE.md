# Project: claude-otel-stack

Local OTEL stack for monitoring Claude Code sessions. Five containers via `podman-compose`: OTEL Collector, Prometheus, Loki, Tempo, Grafana.

**Repo:** https://github.com/jewzaam/claude-otel-stack

Claude Code is unaffected if the stack is offline — the exporter is fire-and-forget.

## Architecture

```
Claude Code → OTLP gRPC :4317 → OTEL Collector → Prometheus (metrics)
                                                → Loki (events/logs)
                                                → Tempo (traces)
                                                → Grafana (dashboards)
```

## Files

| Path | Purpose |
|------|---------|
| `docker-compose.yml` | All 5 services with `:z` SELinux bind mounts |
| `bin/claude-wrapper.sh` | Wrapper script — sources claude.env, execs claude with passthrough args |
| `bin/claude.env` | OTEL env vars including dynamic `project=$(pwd)` (host sessions only) |
| `bin/dashboard-sync.py` | Bidirectional sync between dashboard JSON files and Grafana API |
| `systemd/claude-otel-stack.service` | systemd user unit for autostart |
| `config/otel-collector-config.yaml` | OTLP receiver → prometheus + loki + tempo exporters |
| `config/prometheus.yml` | Scrape config for collector's prometheus exporter on `:8889` |
| `config/loki-config.yaml` | Loki config — ruler with remote write to Prometheus |
| `config/loki-rules/fake/rules.yaml` | Recording rules: derive session state from native OTEL events |
| `config/tempo.yaml` | Tempo trace storage (local backend) |
| `config/grafana-datasources.yaml` | Auto-provisions Prometheus, Loki, Tempo datasources |
| `dashboards/*.json` | Grafana dashboard JSON for import |
| `docs/tips.md` | Gotchas: SELinux, Loki structured metadata, LogQL patterns |
| `docs/thoughts.md` | Design rationale, architecture options, signal inventory |

## Network

All services run on the `otel` bridge network (`172.30.0.0/24`) with static IPs. Host ports bind to `127.0.0.1` only — not reachable via `host.containers.internal` from sandbox containers.

| Service | Static IP | Host Port |
|---------|-----------|-----------|
| otel-collector | 172.30.0.10 | 127.0.0.1:4317, :4318, :8889 |
| prometheus | 172.30.0.11 | 127.0.0.1:9090 |
| loki | 172.30.0.12 | 127.0.0.1:3100 |
| tempo | 172.30.0.13 | 127.0.0.1:3200 |
| grafana | 172.30.0.14 | 127.0.0.1:3000 |

Sandbox sessions connect to static IPs directly (e.g., `172.30.0.10:4318`). Access controlled by sandbox network policy — only policies with the IP:port entry can reach the services. Host sessions use `localhost` via the `127.0.0.1` binding.

## Key constraints

- **This repo is upstream for the k3s deployment** — `setup-k3s` generates its copies from here via `make build-claude-loki-rules` and `make build-claude-dashboards` (both under `make build-claude-otel`), which the user runs. Edit `config/loki-rules/` and `dashboards/` here only; `setup-k3s/gitops/apps/` is generated and gets overwritten. Nothing is live on the cluster until that sync runs and the Loki ruler reloads.

- **Dashboard changes require query validation** — after ANY dashboard JSON edit, validate every modified query against the OTEL stack (Prometheus, Loki, Tempo) if reachable. Check: correct separator per query language (`,` for PromQL, `|` for LogQL, `&&` for TraceQL), balanced braces/parens, include/exclude filter parity, and that queries actually return data. Do this automatically — never wait to be asked.

- **Loki structured metadata** — OTLP attributes are stored as structured metadata, NOT labels. Only `service_name` is a label. Filter with `| field="value"` after the stream selector, not inside `{}`. `unwrap` works on numeric structured metadata fields (e.g., `sum(sum_over_time({service_name="claude-code"} | event_name="api_request" | unwrap cost_usd [1h]))` verified against live Loki — older note in `docs/tips.md` claiming otherwise is wrong). Structured metadata fields support `=~` regex match operator, enabling Grafana variable substitution with "All" option (substitutes `.*`).
- **Loki `label_values()` only returns true labels** — `service_name` plus any k8s-injected labels. Structured metadata (`session_id`, `project`, `command_name`, `prompt_id`) is NOT enumerable via Grafana variable query type 1. Use Prometheus `label_values(claude_code_cost_usage_USD_total, project)` for template variables, or hardcode allowed values. The Loki `/loki/api/v1/detected_field/{name}/values` endpoint returns structured-metadata values but Grafana's dashboard JSON variable format does not expose it cleanly.
- **Prefer Loki for cost and token panels** — `increase(claude_code_cost_usage_USD_total[$__range])` produces oscillating values that mismatch Claude Code's statusline (sparse counter increments + extrapolation at series boundaries + ephemeral `session_id` label dimension). `sum_over_time({service_name="claude-code"} | event_name="api_request" | unwrap cost_usd [...])` is exact per-event sum with no extrapolation. Same applies to all 4 token types (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`).
- **Prometheus for numbers** — cost, tokens, active time come as proper metrics. Use Prometheus for numeric aggregation, Loki for counting and filtering events.
- **`:z` on all bind mounts** — required for rootless podman on SELinux (Fedora).
- **`project` label** — `$(pwd)` at launch for host sessions; sandbox sessions use the sandbox's host-side directory, injected as `SANDBOX_HOST_DIR` (in-container `$(pwd)` is `/sandbox/source/` for every sandbox). Only available on sessions launched via the wrapper.
- **Dashboard sync is bidirectional** — edits in Grafana UI write back to JSON files on disk within 10s via the dashboard-sync sidecar. Dashboards must NOT be provisioned (provisioned = read-only in Grafana, breaks bidirectional sync). No manual import needed. Adding a new UID→filename mapping in `dashboard-sync.py` requires restarting the container (`podman-compose restart dashboard-sync`); existing dashboard edits sync without restart.
- **`host.name` is always the machine, never the sandbox.** Host sessions get
  `$(hostname)` from `claude.env`. Sandbox sessions get the *creating host's*
  hostname, injected as `SANDBOX_HOST_NAME` into `/sandbox/.env` by
  `openshell-sandbox`, because in-container `hostname` is `sandbox-sb-<hash>` —
  the sandbox, not the machine. Sandbox identity lives in `sandbox_source`.
  **Use `sandbox_source` presence, never a `host_name` pattern or a `project`
  path, to tell sandbox from local.** Dashboards used `host_name=~"sandbox-.*"`
  and it silently stopped matching when this changed. `sandbox_source` is an
  injected env var, absent on host sessions no matter what directory they run
  in — including a host session inside `~/sandboxes/<name>/`, which a
  path-based test would misclassify. It is set for every sandbox regardless of
  profile, and was set before the change too, so it works across the retention
  boundary. Verified: `sandbox_source=~".+"` and `sandbox_source=""` partition
  api_request events exactly.
- **`sandbox_openshell_name`** (`sb-<hash>`) is the only label that joins a
  session to `openshell sandbox list`, its container, and the
  `openshell.ai/sandbox-name` podman label. `sandbox_source` is the friendly
  name and cannot. Not used by any dashboard yet.

## Loki deployment gotchas

- **Single-node ring config** — Loki defaults to Consul KV store on localhost:8500 for the ring. Single-node deployments need `common.ring.kvstore.store: inmemory` and `common.replication_factor: 1`. Without this, Loki fails with `unable to initialise ring state: Get "http://localhost:8500/v1/kv/collectors/ring"`.
- **schema_config is mandatory** — Loki panics on startup without `schema_config` section. `validateSchemaRequirements` panics with `index out of range [0] with length 0`. Cannot be omitted even when using defaults.
- **Ruler remote_write struct format** — Loki ruler `remote_write` is a struct (`enabled: true` + `client.url: ...`), NOT a Prometheus-style list (`- url: ...`). Using list syntax causes `cannot unmarshal !!seq into ruler.RemoteWriteConfig`.
- **Ruler WAL path** — Ruler WAL directory must be an absolute path writable by the container process. Default is relative `ruler-wal` which causes `mkdir ruler-wal: permission denied` in containers. Set `ruler.wal.dir: /loki/ruler-wal` (inside the data volume).
- **Do NOT use `common.path_prefix`** when adding config to a Loki that previously ran with defaults — it changes storage subdirectory paths and orphans existing index/chunk data. Set explicit paths instead: `storage_config.tsdb_shipper.active_index_directory`, `storage_config.filesystem.directory`, `ingester.wal.dir`, `compactor.working_directory`, `ruler.wal.dir`. Inspect the volume to find actual data paths: `podman run --rm -v <volume>:/loki:z alpine find /loki -maxdepth 3 -type d | sort`.
- **All writable paths must be absolute** — without `common.path_prefix`, several components default to relative paths (`wal`, `ruler-wal`, `/var/loki`) that fail with permission denied in containers. Every writable directory needs an explicit absolute path under the data volume.
- **Compactor `tables/` path isolation** — the Loki compactor panics (`slice bounds out of range [-2:]` in `ExtractIntervalFromTableName`) when non-table directories (`wal`, `uploader`, `multitenant`, `per_tenant`, `scratch`) exist in the TSDB index path. Fix: `path_prefix: tables/` in schema_config's index section isolates index tables in a `tables/` subdirectory under `filesystem.directory`. Compactor only scans that subdirectory — no junk entries.
- **`tables/` directory ownership** — when creating the `tables/` directory via alpine container for data migration, it's owned by root. Loki runs as UID 10001. Must `chown -R 10001:10001 /loki/chunks/tables/` after creation. One-time fix — subsequent tables created by Loki have correct ownership.
- **Loki entrypoint cleanup** — docker-compose uses an entrypoint wrapper that removes non-table dirs (`wal`, `per_tenant`, `scratch`, `uploader`, `multitenant`) from `tsdb-shipper-active`, `chunks/index`, and `compactor` before exec'ing Loki. Prevents compactor panic on restart when the TSDB shipper recreates operational dirs.

## Query language gotchas

- **TraceQL attribute prefixes** — resource attributes use `resource.` prefix (e.g., `resource.project`), span attributes use `span.` prefix (e.g., `span.session.id`). Do NOT backtick-quote dotted attribute names in Grafana — Tempo API accepts backticks but Grafana's TraceQL parser rejects them. Use `span.session.id` not `` span.`session.id` ``.
- **`label_replace` regex alternation** — `(group1)|(group2)` only fills `$1` when group1 matches; if group2 matches, `$1` is empty. Use single capture group with `$` anchor instead. Example for worktree path stripping: `(.*?)(/[^/]*worktree[^/]*/.*|$)`.
- **`count_over_time` on Loki** — returns one count per stream. Wrap with `sum()` for a single aggregate number in stat panels.
- **`increase(metric[$__range])`** — works on historical data in Prometheus; series persist in storage past the staleness window. Only instant queries on raw counters miss expired series. Use `increase(...[$__range])` for stat panels covering the full dashboard time range. `increase()` uses linear interpolation, producing float results even from integer counters — wrap with `round()` for panels where integer display is expected (e.g., lines of code).
- **Avoid fixed `decimals` on stat panels using `short` unit** — Grafana's `short` unit auto-scales with SI suffixes (k, M, G). Setting `decimals: 0` truncates the scaled value (e.g., `1.2k` becomes `1k`). Omit the `decimals` field to let Grafana auto-format.
- **Grafana expression queries for scalar math** — use `__expr__` datasource with `"type": "math"` for operations like `$A * 30` on Loki query results. Set `"hide": true` on intermediate Loki queries so only the expression result displays in stat panels.
- **`query_source` field absent on older Claude Code** — versions before ~2.1.146 do not emit `query_source` in api_request events. These show as "Value" (empty label) in Grafana pie charts grouped by `query_source`. The `"sdk"` value indicates subagent API calls from newer versions.
- **Model names differ between Prometheus and Loki** — Prometheus stores model names with version suffixes (e.g., `claude-opus-4-6[1m]`, `claude-sonnet-4-5@20250929`). Loki structured metadata stores shorter names (e.g., `claude-opus-4-6`, `claude-haiku-4-5-20251001`). Use Loki-style names when filtering Loki queries.
- **Loki `or` unions range-vector results** — combine with `label_format` to add a discriminator label per branch. Pattern for unifying slash-command counts and Skill-tool counts in one query:
  ```
  sum by (prompt_id, invocation_name, source) (
    count_over_time({...} | event_name="user_prompt" | command_source="custom" | label_format invocation_name=`/{{.command_name}}`,source="slash" [$__range])
    or
    count_over_time({...} | event_name="tool_result" | tool_name="Skill" | label_format invocation_name=`...regex...`,source="skill_tool" [$__range])
  )
  ```
- **Extract `skill_name` from `tool_parameters` JSON** — `tool_result` events for Skill tool invocations have `tool_parameters` as a JSON string like `{"skill_name":"superpowers:writing-plans"}`. Use LogQL `label_format` with Go template `regexReplaceAll` to extract:
  ```
  | label_format skill_name=`{{ regexReplaceAll "^.*\"skill_name\":\"([^\"]+)\".*$" .tool_parameters "$1" }}`
  ```

## Event field shapes

- **`api_request` event structured metadata** (verified live via curl against Loki): `cost_usd`, `cost_usd_micros`, `duration_ms`, `model`, `query_source`, `effort`, `speed`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `prompt_id`, `request_id`, plus standard session/project/host attributes. All 4 token types are present on events — Prometheus is not the only source for token counts.
- **`prompt_id` is the cross-event join key** in Loki. Same `prompt_id` appears on `user_prompt`, `api_request`, and `tool_result` events for a single interaction. For exact slash-command cost attribution in a Grafana table panel: query cost-per-prompt_id (`api_request` unwrap `cost_usd`) and command-per-prompt_id (`user_prompt` with `command_source="custom"`), then apply Grafana `joinByField` transformation on `prompt_id` and group by `command_name`.
- **Cost-by-skill attribution caveats**:
  - **Slash commands** (`/foo`) — exact attribution. The slash command IS the `user_prompt`, so all `api_request`s within that `prompt_id` belong to it. Sum cost per `prompt_id`.
  - **Skill tool invocations** (e.g., `superpowers:writing-plans`) — attribute full interaction cost to each Skill tool that ran in that interaction. If an interaction invokes multiple skills, total cost overcounts but average cost per invocation remains a useful signal.
  - **`skill_name` on api_requests** — Claude Code sets `skill_name` structured metadata on api_request events during skill execution, including on subagent calls. Best general-purpose attribution field: covers 73-100% for agent-based skills, but only ~20% for web-heavy skills (web_search_tool and web_fetch_apply don't propagate skill_name). Plugin-provided skills appear as `skill_name="third-party"`. Use the `claude_skill_cost_usd` recording rule (Prometheus) for dashboard panels instead of prompt_id-based joins.
  - **`trace_id` links subagent work** — subagents dispatched during a skill share the parent interaction's trace_id, even across different prompt_ids. However, trace grouping varies by skill implementation and continuation prompts' repl_main_thread calls get their own traces. Not reliable as sole attribution key.
- **Tempo `claude_code.interaction` span carries `user_prompt` as an attribute** (e.g., `user_prompt: "/commit"`). Useful for trace-side attribution of slash commands without joining to Loki events.
- **OTEL trace export tuning** — `OTEL_BSP_SCHEDULE_DELAY` default is 5000ms. Lowered in `bin/claude.env` to 2000ms with `OTEL_BSP_MAX_EXPORT_BATCH_SIZE=128` and `OTEL_LOGS_EXPORT_INTERVAL=2000` so completed child spans (`llm_request`, `tool`) and events surface faster in dashboards. Caveat: the root `claude_code.interaction` span does NOT close until the interaction ends; no config tweak changes this — long interactions remain invisible to "Interaction Traces" panels until completion.

## Development notes

- **ShellCheck Fedora package** — `sudo dnf install ShellCheck` (capital S, capital C).
- **Windows CRLF line endings break shell scripts** — shellcheck flags SC1017 on every line. Sourced env files (e.g., `bin/claude.env`) also break — exported vars get trailing `\r` (e.g., `OTEL_BSP_SCHEDULE_DELAY=2000\r`) which the OTEL SDK silently rejects or misparses. Strip with `sed -i 's/\r$//' <file>` or rewrite via editor with LF endings.

## Grafana datasource UIDs

| Datasource | UID |
|------------|-----|
| Prometheus | `PBFA97CFB590B2093` |
| Loki | `P8E80F9AEF21F6940` |
| Tempo | `P214B5B846CF3925F` |

Dashboard JSON files use these UIDs. If the stack is recreated, UIDs will change and dashboards need reimport.

## Prometheus metrics

| Metric | Labels beyond standard |
|--------|----------------------|
| `claude_code_cost_usage_USD_total` | model, query_source, effort |
| `claude_code_token_usage_tokens_total` | type, model, query_source, effort |
| `claude_code_active_time_seconds_total` | type (cli/user) |
| `claude_code_session_count_total` | start_type |
| `claude_code_lines_of_code_count_total` | type (added/removed) |
| `claude_code_code_edit_tool_decision_total` | tool_name, decision, source, language |

Standard labels on all: session_id, user_name, environment, terminal_type, service_version, project (if set).

## Recording rules (Loki ruler → Prometheus)

Loki recording rules in `config/loki-rules/fake/rules.yaml` derive session state and cost attribution from native Claude Code OTEL events and remote-write metrics to Prometheus. Used by `claude-dashboard` for state display and skill cost panels.

| Metric | Meaning | LogQL signal |
|--------|---------|-------------|
| `claude_session_working` | Activity count in last 60s | `api_request`, `tool_decision`, `tool_result`, `skill_activated` |
| `claude_session_ready` | Stop is most recent event | `hook_execution_complete` with `hook_event=Stop` timestamp > activity timestamp |
| `claude_session_permission` | PermissionRequest timestamp > last tool_result/user_prompt timestamp | `hook_execution_complete` with `hook_event=PermissionRequest` timestamp > activity timestamp |
| `claude_skill_cost_usd` | Cost attributed to skill (1m window) | `api_request` with `skill_name!=""`, unwrap `cost_usd` |

Labels on state metrics: `session_id`, `host_name`, `project`, `location`, `headless`, `sandbox_source`, `sandbox_openshell_name`.
Labels on skill cost: the same, plus `skill_name`.

### The `by` clause is the public API

A label missing from a rule's `by` clause **does not exist** for anyone
downstream — the rule is where it is erased, and Prometheus never sees it. You
cannot enumerate the consumers from this repo: `claude-dashboard` is a separate
app, Grafana alerts and saved queries live in Grafana's DB rather than
`dashboards/*.json`, and other machines ship to the same stack. Assume there is
a consumer you cannot see.

Include by default: a label 1:1 with `session_id` adds zero series, while
omitting one costs a later rule change plus a second label discontinuity —
existing series keep their old label set.

### Compare on `session_id`, never on the full label set

`claude_session_ready` and `claude_session_permission` run their timestamp
comparison keyed by `session_id` alone, then filter the label-carrying series
with `and on (session_id) (<comparison>)`. A plain `>` between two fully
labelled aggregations matches on *every* label, so the comparison only ever
sees activity carrying the identical label set — and label sets change under
you: adding a rule label, or a sandbox env refresh that starts emitting
`sandbox_openshell_name` mid-session, splits one session into two shapes. The
old shape then holds a PermissionRequest that no later event can supersede,
because every later event carries the new shape. Observed live: a session sat
at `permission_required` for a full 30m window while its new shape was
correctly `working`; only window expiry cleared it.

**Do not write `group_left ()` with an empty label list.** It is valid to the
query API but the ruler round-trips every rule through Loki's serializer,
which drops the empty parens — the following `(` is then read as the start of
the `group_left` label list and evaluation dies with
`parse error ... unexpected MAX, expecting IDENTIFIER or )`. Both rules
loaded and failed exactly this way. `and on (session_id)` needs no
`group_left`, so the trap does not arise; it also preserves the left side's
value (the event timestamp) and label set.

**Validate the round-trip, not just the query.** `/loki/api/v1/query`
accepting an expression does not mean the ruler will:

```bash
# what the ruler will actually store and re-parse
curl -sG "$LOKI/loki/api/v1/format_query" --data-urlencode "query=$EXPR"
# feed that result back through /loki/api/v1/query — it must parse and
# return the same series count as the original
```

After deploying, check health rather than assuming: `curl -s
"$LOKI/prometheus/api/v1/rules"` reports per-rule `health` and `lastError`.
A rule that fails to parse reports `health=err` and stops emitting — the
metric goes empty, it does not go wrong.

`claude_session_ready` and `claude_session_permission` compare two aggregations
with `>`. Each carries the label list **four times** — `max by (...)` and
`) by (...)`, on both sides of the comparison. Miss one and the join silently
returns no series instead of erroring. After editing, run each `expr` against
Loki directly and check the series count is non-zero.

`headless` is a self-made resource attribute: `bin/claude-wrapper.sh` (user's
bin repo) appends `headless=true` to `OTEL_RESOURCE_ATTRIBUTES` for
`-p`/`--print` runs. Claude Code emits no native headless marker (verified
through 2.1.233 — `query_source` is version-unstable: 2.1.226 interactive
main-thread requests emitted `repl_main_thread`, 2.1.233 emits `sdk`,
identical to headless). claude-dashboard skips remote rows whose state
metrics carry `headless="true"`; absent label = interactive.

### `location` label

Normalized display-friendly path derived from `sandbox_source` and `project` via `label_format` in each recording rule:
- Sandboxes (`sandbox_source` set): `~/sandboxes/<sandbox_source>`
- Local sessions: `/home/<user>/...` or `/Users/<user>/...` replaced with `~/...`
- Non-home paths (`/tmp`, `/opt`): pass through unchanged

The `hasPrefix "sandbox" .host_name` middle branch is dead for current data and
kept only for series predating the `host.name` change. Sandbox `project` is now
the sandbox's directory on the host (`~/sandboxes/<name>` after the home-prefix
rewrite), so both branches agree — it used to be `/sandbox/source/` for every
sandbox on every machine, which collapsed them into one `$project` entry.

Dashboards should use `location` instead of `project` for display. `project` is preserved as the raw value for filtering and debugging.

### Recording rule gotchas

- **Use `observed_timestamp` not `event_sequence`** for time comparisons — `event_sequence` resets across session resumes (sandbox reconnects, `/resume`).
- **Filter `event_name = "hook_execution_complete"`** for real hook events — `hook_registered` events also carry `hook_event` labels but are session-start metadata, not firings.
- **`count_over_time` needs `sum by` wrapper** — `count_over_time` does not support `by()` grouping directly.
- **Prometheus remote write receiver** must be enabled via `--web.enable-remote-write-receiver` flag on the Prometheus container.
- **Exclude housekeeping events from WORKING and READY rules** — both rules filter `query_source !~ "away_summary|compact|generate_session_title"` from activity events. These housekeeping events fire after Stop and would block READY detection by having a newer timestamp than the Stop event.
