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
| `bin/claude-otel-stack.service` | systemd user unit for autostart |
| `config/otel-collector-config.yaml` | OTLP receiver → prometheus + loki + tempo exporters |
| `config/prometheus.yml` | Scrape config for collector's prometheus exporter on `:8889` |
| `config/tempo.yaml` | Tempo trace storage (local backend) |
| `config/grafana-datasources.yaml` | Auto-provisions Prometheus, Loki, Tempo datasources |
| `dashboards/*.json` | Grafana dashboard JSON for import |
| `docs/tips.md` | Gotchas: SELinux, Loki structured metadata, LogQL patterns |
| `docs/thoughts.md` | Design rationale, architecture options, signal inventory |

## Key constraints

- **Loki structured metadata** — OTLP attributes are stored as structured metadata, NOT labels. Only `service_name` is a label. Filter with `| field="value"` after the stream selector, not inside `{}`. `unwrap` does not work on structured metadata fields.
- **Prometheus for numbers** — cost, tokens, active time come as proper metrics. Use Prometheus for numeric aggregation, Loki for counting and filtering events.
- **`:z` on all bind mounts** — required for rootless podman on SELinux (Fedora).
- **`project` label** — set dynamically via `$(pwd)` in `claude-wrapper.sh` at launch time. Only available on sessions launched via the wrapper.

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
