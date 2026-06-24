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
| `bin/claude.env` | OTEL env vars including dynamic `project=$(pwd)` |
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

## Key constraints

- **Loki structured metadata** — OTLP attributes are stored as structured metadata, NOT labels. Only `service_name` is a label. Filter with `| field="value"` after the stream selector, not inside `{}`. `unwrap` works on numeric structured metadata fields (e.g., `sum(sum_over_time({service_name="claude-code"} | event_name="api_request" | unwrap cost_usd [1h]))` verified against live Loki — older note in `docs/tips.md` claiming otherwise is wrong). Structured metadata fields support `=~` regex match operator, enabling Grafana variable substitution with "All" option (substitutes `.*`).
- **Loki `label_values()` only returns true labels** — `service_name` plus any k8s-injected labels. Structured metadata (`session_id`, `project`, `command_name`, `prompt_id`) is NOT enumerable via Grafana variable query type 1. Use Prometheus `label_values(claude_code_cost_usage_USD_total, project)` for template variables, or hardcode allowed values. The Loki `/loki/api/v1/detected_field/{name}/values` endpoint returns structured-metadata values but Grafana's dashboard JSON variable format does not expose it cleanly.
- **Prefer Loki for cost and token panels** — `increase(claude_code_cost_usage_USD_total[$__range])` produces oscillating values that mismatch Claude Code's statusline (sparse counter increments + extrapolation at series boundaries + ephemeral `session_id` label dimension). `sum_over_time({service_name="claude-code"} | event_name="api_request" | unwrap cost_usd [...])` is exact per-event sum with no extrapolation. Same applies to all 4 token types (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`).
- **Prometheus for numbers** — cost, tokens, active time come as proper metrics. Use Prometheus for numeric aggregation, Loki for counting and filtering events.
- **`:z` on all bind mounts** — required for rootless podman on SELinux (Fedora).
- **`project` label** — set dynamically via `$(pwd)` in `claude-wrapper.sh` at launch time. Only available on sessions launched via the wrapper.
- **Dashboard sync is bidirectional** — edits in Grafana UI write back to JSON files on disk within 10s via the dashboard-sync sidecar. Dashboards must NOT be provisioned (provisioned = read-only in Grafana, breaks bidirectional sync). No manual import needed. Adding a new UID→filename mapping in `dashboard-sync.py` requires restarting the container (`podman-compose restart dashboard-sync`); existing dashboard edits sync without restart.
- **`host.name=$(hostname)`** — added to `OTEL_RESOURCE_ATTRIBUTES` in `claude.env` for future multi-host aggregation.

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

Loki recording rules in `config/loki-rules/fake/rules.yaml` derive session state from native Claude Code OTEL events and remote-write metrics to Prometheus. Used by `claude-dashboard` for state display.

| Metric | Meaning | LogQL signal |
|--------|---------|-------------|
| `claude_session_working` | Activity count in last 60s | `api_request`, `tool_decision`, `tool_result`, `skill_activated` |
| `claude_session_ready` | Stop is most recent event | `hook_execution_complete` with `hook_event=Stop` timestamp > activity timestamp |
| `claude_session_permission` | PermissionRequest timestamp > last tool_result/user_prompt timestamp | `hook_execution_complete` with `hook_event=PermissionRequest` timestamp > activity timestamp |

Labels on all: `session_id`, `host_name`, `project`, `location`.

### `location` label

Normalized display-friendly path derived from `host_name` and `project` via `label_format` in each recording rule:
- Sandboxes (`host_name` starts with `sandbox`): `~/sandboxes/<host_name>`
- Local sessions: `/home/<user>/...` or `/Users/<user>/...` replaced with `~/...`
- Non-home paths (`/tmp`, `/opt`): pass through unchanged

Dashboards should use `location` instead of `project` for display. `project` is preserved as the raw value for filtering and debugging.

### Recording rule gotchas

- **Use `observed_timestamp` not `event_sequence`** for time comparisons — `event_sequence` resets across session resumes (sandbox reconnects, `/resume`).
- **Filter `event_name = "hook_execution_complete"`** for real hook events — `hook_registered` events also carry `hook_event` labels but are session-start metadata, not firings.
- **`count_over_time` needs `sum by` wrapper** — `count_over_time` does not support `by()` grouping directly.
- **Prometheus remote write receiver** must be enabled via `--web.enable-remote-write-receiver` flag on the Prometheus container.
- **Exclude housekeeping events from WORKING and READY rules** — both rules filter `query_source !~ "away_summary|compact|generate_session_title"` from activity events. These housekeeping events fire after Stop and would block READY detection by having a newer timestamp than the Stop event.
