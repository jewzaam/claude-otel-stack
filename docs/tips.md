# OTEL + Loki + Grafana: Tips and Gotchas

Findings from standing up the Claude Code OTEL POC on rootless Podman (Fedora 43, Wayland).

## Podman / SELinux

- **`:z` on all bind mounts** — rootless podman on SELinux requires `:z` suffix on every volume mount that maps a host file into a container. Without it: `permission denied` on config files. Every. Single. Time.
- **`podman-compose`** (hyphenated) — `podman compose` (space) looks for docker-compose plugins and fails. Install with `python -m pip install podman-compose`.
- **`podman-compose up -d`** sometimes hangs on the last container — Ctrl+Z then `bg` works. It's a known podman-compose issue with detached mode.

## Tempo

- **Minimal config** — latest Tempo rejects `ingester` and `compactor` as top-level fields. They moved into sub-configs. The minimum viable `tempo.yaml`:
  ```yaml
  stream_over_http_enabled: true
  server:
    http_listen_port: 3200
  distributor:
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
  storage:
    trace:
      backend: local
      local:
        path: /var/tempo/traces
      wal:
        path: /var/tempo/wal
  ```
- Don't add fields from blog posts or older docs — the Tempo config schema changes between versions. When in doubt, start minimal and add.

## Loki: OTLP Ingestion

This is the biggest gotcha. When Loki receives data via OTLP (from the OTEL Collector), it stores attributes as **structured metadata**, not as indexed labels.

### What this means

- **Only `service_name` is a label** — confirmed via `/loki/api/v1/labels`. All other fields (event_name, tool_name, cost_usd, duration_ms, etc.) are structured metadata.
- **`/loki/api/v1/label/{name}/values`** returns empty for structured metadata fields — they're not in the label index.
- **Stream selector must use `service_name`** — you can't put `event_name` in the stream selector `{}` braces. It goes in the filter pipeline.

### Query patterns that WORK

```logql
# Filter by structured metadata field
{service_name="claude-code"} | event_name="api_request"

# Count events over time
count_over_time({service_name="claude-code"} | event_name="api_request" [1m])

# Count by a metadata field
sum by (tool_name) (count_over_time({service_name="claude-code"} | event_name="tool_result" [1m]))

# Count over dashboard time range
sum by (event_name) (count_over_time({service_name="claude-code"} | event_name=~"api_request|tool_result" [$__range]))

# Log stream with multiple filters
{service_name="claude-code"} | event_name="tool_result" | success="false"
```

### Query patterns that DON'T WORK

```logql
# ❌ Structured metadata fields in stream selector
{service_name="claude-code", event_name="api_request"}

# ❌ JSON parsing — data isn't in the log body, it's structured metadata
{service_name="claude-code"} | json | event_name="api_request"

# ❌ unwrap on structured metadata — parse error
{service_name="claude-code"} | event_name="api_request" | unwrap cost_usd

# ❌ unpack + unwrap — also fails
{service_name="claude-code"} | event_name="api_request" | unpack | unwrap cost_usd
```

### Implication for dashboards

- **Use Loki for**: counting events, filtering by type, rate calculations (`count_over_time`), log streams, breakdowns by category (`sum by`)
- **Use Prometheus for**: numeric aggregation (cost in USD, token counts, duration, active time) — these come in as proper OTEL metrics with full label support
- **You cannot scatter-plot numeric values from Loki events** (cost per call, latency per call) because `unwrap` doesn't work on structured metadata. Those visualizations require Prometheus metrics.

## OTEL Collector

- **Loki exporter** — use `otlphttp/loki` with endpoint `http://loki:3100/otlp`. The old `loki` exporter type is deprecated. The OTLP endpoint is what triggers structured metadata storage.
- **`resource_to_telemetry_conversion: enabled: true`** on the Prometheus exporter — promotes OTEL resource attributes (like `session_id`, `user_name`) to Prometheus labels. Without this, you can't filter metrics by session.

## Grafana Dashboard Import

- **Don't wrap in `{"dashboard": {...}}`** — Grafana import wants the dashboard object directly at the top level.
- **Use explicit datasource UIDs** — `"datasource": "Prometheus"` (string) doesn't reliably resolve. Use `"datasource": {"type": "prometheus", "uid": "PBFA97CFB590B2093"}`. Get UIDs from `curl -s http://localhost:3000/api/datasources`.
- **Every panel needs an `id` field** — Grafana silently fails import without them.
- **Include `schemaVersion`** — add `"schemaVersion": 39` at the top level.

## Claude Code OTEL Signals

### What you get from Prometheus (metrics)

| Metric | What it tells you |
|--------|------------------|
| `claude_code_cost_usage_USD_total` | Cumulative cost, split by model/session/query_source |
| `claude_code_token_usage_tokens_total` | Token counts by type: input, output, cacheRead, cacheCreation |
| `claude_code_active_time_seconds_total` | Time split: `cli` (processing) vs `user` (interaction) |
| `claude_code_session_count_total` | Session starts with `start_type` (fresh/resume/continue) |

### What you get from Loki (events)

| Event | Key fields in structured metadata |
|-------|----------------------------------|
| `api_request` | model, cost_usd, duration_ms, input_tokens, output_tokens, cache_read_tokens, speed, query_source |
| `tool_result` | tool_name, success, duration_ms, decision_type, decision_source |
| `user_prompt` | prompt_length, prompt (if `OTEL_LOG_USER_PROMPTS=1`), command_name |
| `hook_execution_start/complete` | (hook activity) |
| `mcp_server_connection` | (MCP server lifecycle) |

### What you get from Tempo (traces)

Span hierarchy: `interaction → llm_request / tool → tool.blocked_on_user / tool.execution`. Requires `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`.

## Data Persistence

- **Named volumes survive `podman-compose down`** — data persists across restarts.
- **`podman-compose down -v` deletes volumes** — this wipes all stored data.
- **Grafana dashboards** persist in the `grafana-data` volume — you don't need to reimport after restart.
- At ~10M tokens/day: expect ~100 MB/day of storage across all backends. ~3 GB/month.
