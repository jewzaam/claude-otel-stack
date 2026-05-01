# OTEL for Claude Code — Thoughts and Local POC

## What's Available

Claude Code has native OpenTelemetry support. Three signal types:

1. **Metrics** — session counts, token usage, cost (USD), lines of code modified, commits, PRs, active time, tool permission decisions
2. **Events** (via logs exporter) — user prompts, tool results, API requests, API errors. Each event carries a `prompt.id` for correlation
3. **Traces** (beta) — distributed tracing with span hierarchy: `interaction → llm_request / tool → tool.blocked_on_user / tool.execution`. Subagent spans nest under parent tool spans

All three are opt-in via environment variables. No code changes — just set env vars before launching `claude`.

## What We'd Get

| Signal | Value for us |
|--------|-------------|
| `claude_code.cost.usage` | Per-session, per-model cost tracking. Currently only visible in AgentPulse. OTEL gives us historical time-series |
| `claude_code.token.usage` | Token consumption by type (input/output/cacheRead/cacheCreation), by model, by query_source (main vs subagent) |
| `claude_code.active_time.total` | How much time is spent actively vs idle. Split by user interaction vs CLI processing |
| `tool_result` events | Every tool call with duration, success/failure, decision (accept/reject). Can answer: what's slow? What gets rejected? |
| `api_request` events | Per-request cost, latency, cache hit rate, model used. Can answer: is prompt caching working? |
| Traces | Full request waterfall — see exactly how long permission prompts block, how long tools run, how subagent chains flow |

The `OTEL_LOG_TOOL_DETAILS=1` flag adds bash commands, file paths, skill names, MCP server/tool names to events. Combined with `OTEL_LOG_USER_PROMPTS=1`, you get a full audit trail.

## Local POC Architecture

```
Claude Code → OTLP (gRPC :4317) → OpenTelemetry Collector → backends
                                        ↓           ↓           ↓
                                   Prometheus    Loki       Jaeger/Tempo
                                   (metrics)    (events)    (traces)
                                        ↓           ↓           ↓
                                        └─── Grafana ───────────┘
```

### Option A: All-in-one with Grafana stack (recommended for POC)

Use `docker compose` with:
- **OpenTelemetry Collector** — receives OTLP, fans out to backends
- **Prometheus** — metrics storage, query via PromQL
- **Loki** — log/event storage
- **Tempo** — trace storage
- **Grafana** — dashboards for all three

### Option B: Minimal — just Jaeger

Single container, receives OTLP directly, shows traces + basic metrics. No log storage. Good for just seeing if it works.

### Option C: Console exporter only

No infrastructure. Just `OTEL_METRICS_EXPORTER=console` and read stdout. Good for verifying data shapes before committing to a stack.

## POC Setup (Option A)

### 1. OTEL Collector config (`otel-collector-config.yaml`)

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

exporters:
  prometheus:
    endpoint: 0.0.0.0:8889
  loki:
    endpoint: http://loki:3100/loki/api/v1/push
  otlp/tempo:
    endpoint: http://tempo:4317
    tls:
      insecure: true

service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [prometheus]
    logs:
      receivers: [otlp]
      exporters: [loki]
    traces:
      receivers: [otlp]
      exporters: [otlp/tempo]
```

### 2. Docker Compose (`docker-compose.yml`)

```yaml
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    volumes:
      - ./otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml
    ports:
      - "4317:4317"   # OTLP gRPC
      - "8889:8889"   # Prometheus exporter

  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    ports:
      - "9090:9090"

  loki:
    image: grafana/loki:latest
    ports:
      - "3100:3100"

  tempo:
    image: grafana/tempo:latest
    command: ["-config.file=/etc/tempo.yaml"]
    volumes:
      - ./tempo.yaml:/etc/tempo.yaml
    ports:
      - "3200:3200"   # Tempo API

  grafana:
    image: grafana/grafana:latest
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
    ports:
      - "3000:3000"
    volumes:
      - ./grafana-datasources.yaml:/etc/grafana/provisioning/datasources/datasources.yaml
```

### 3. Claude Code env vars

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1

# Metrics
export OTEL_METRICS_EXPORTER=otlp
export OTEL_METRIC_EXPORT_INTERVAL=10000

# Events/Logs
export OTEL_LOGS_EXPORTER=otlp
export OTEL_LOGS_EXPORT_INTERVAL=5000

# Traces (beta)
export OTEL_TRACES_EXPORTER=otlp

# OTLP endpoint
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# Detailed data (for POC — would be more selective in prod)
export OTEL_LOG_TOOL_DETAILS=1
export OTEL_LOG_USER_PROMPTS=1

# Resource attributes for filtering
export OTEL_RESOURCE_ATTRIBUTES="user.name=nmalik,environment=dev"
```

## Questions to Answer with the POC

1. **Cost tracking** — can we get accurate per-session cost that matches what the API reports? How does it compare to AgentPulse's numbers?
2. **Cache hit rate** — what percentage of tokens are cache reads? Is prompt caching actually working across sessions?
3. **Tool latency** — which tools are slowest? How much time is spent waiting on permission prompts vs actual execution?
4. **Subagent cost** — when the PA dispatches agents, what's the cost breakdown per agent vs main thread?
5. **Error patterns** — are there recurring API errors (429s, 529s)? What's the retry distribution?

## Relevance to Broader Work

- **Hardening doc** — the quality gates include observability. If we can show OTEL-based monitoring of our own AI tools, that's a concrete example of the pattern we're recommending for Nexus features
- **AI SDLC** — this is the kind of instrumentation the WG should be recommending for all Claude Code deployments. A working POC could become a reference for the org
- **AgentPulse** — the PA dashboard already forwards session usage to AgentPulse. OTEL could replace or supplement that with standard tooling instead of custom forwarding
- **Vertex metrics** — the SonarCloud and Vertex usage dashboards stashed earlier today are similar in spirit. OTEL would give us the same for Claude Code locally

## Next Steps

1. Start with Option C (console exporter) to verify data shapes — 5 min
2. If shapes look right, stand up Option A with docker compose — 30 min
3. Build a basic Grafana dashboard: cost over time, tokens by model, tool latency histogram — 1 hr
4. Run a normal PA session with telemetry on, review the data — 30 min
5. Decide if this is worth maintaining or if it's a one-off exploration
