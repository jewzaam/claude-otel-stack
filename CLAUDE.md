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
| `config/tempo.yaml` | Tempo trace storage (local backend) |
| `config/grafana-datasources.yaml` | Auto-provisions Prometheus, Loki, Tempo datasources |
| `dashboards/*.json` | Grafana dashboard JSON for import |
| `docs/tips.md` | Gotchas: SELinux, Loki structured metadata, LogQL patterns |
| `docs/thoughts.md` | Design rationale, architecture options, signal inventory |

## Key constraints

- **Loki structured metadata** — OTLP attributes are stored as structured metadata, NOT labels. Only `service_name` is a label. Filter with `| field="value"` after the stream selector, not inside `{}`. `unwrap` does not work on structured metadata fields. Structured metadata fields support `=~` regex match operator, enabling Grafana variable substitution with "All" option (substitutes `.*`).
- **Prometheus for numbers** — cost, tokens, active time come as proper metrics. Use Prometheus for numeric aggregation, Loki for counting and filtering events.
- **`:z` on all bind mounts** — required for rootless podman on SELinux (Fedora).
- **`project` label** — set dynamically via `$(pwd)` in `claude-wrapper.sh` at launch time. Only available on sessions launched via the wrapper.
- **Dashboard sync is bidirectional** — edits in Grafana UI write back to JSON files on disk within 10s via the dashboard-sync sidecar. Dashboards must NOT be provisioned (provisioned = read-only in Grafana, breaks bidirectional sync). No manual import needed.
- **`host.name=$(hostname)`** — added to `OTEL_RESOURCE_ATTRIBUTES` in `claude.env` for future multi-host aggregation.

## Query language gotchas

- **TraceQL attribute prefixes** — resource attributes use `resource.` prefix (e.g., `resource.project`), span attributes use `span.` prefix (e.g., `span.session.id`). Do NOT backtick-quote dotted attribute names in Grafana — Tempo API accepts backticks but Grafana's TraceQL parser rejects them. Use `span.session.id` not `` span.`session.id` ``.
- **`label_replace` regex alternation** — `(group1)|(group2)` only fills `$1` when group1 matches; if group2 matches, `$1` is empty. Use single capture group with `$` anchor instead. Example for worktree path stripping: `(.*?)(/[^/]*worktree[^/]*/.*|$)`.
- **`count_over_time` on Loki** — returns one count per stream. Wrap with `sum()` for a single aggregate number in stat panels.
- **`increase(metric[$__range])`** — works on historical data in Prometheus; series persist in storage past the staleness window. Only instant queries on raw counters miss expired series. Use `increase(...[$__range])` for stat panels covering the full dashboard time range.

## Development notes

- **ShellCheck Fedora package** — `sudo dnf install ShellCheck` (capital S, capital C).

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
