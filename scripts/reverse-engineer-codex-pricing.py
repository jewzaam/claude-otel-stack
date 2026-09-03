#!/usr/bin/env python3
"""Estimate Codex token prices from daily spend and Prometheus telemetry.

The input CSV must contain exactly these columns (additional columns are ignored):

    date,usd
    2026-09-01,1.23

The script compares reported daily spend with Codex's four token categories and
prints two estimates:

* a single base-input rate using the upstream cached-input/cache-write/output
  multipliers; and
* independent rates for all four token categories when the observations have
  enough variation to identify them.

The estimates are API-equivalent estimates. They are not useful if the supplied
spend includes a subscription, credits, taxes, other models, or other devices.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TOKEN_TYPES = ("input", "cached_input", "cache_write_input", "output")
UPSTREAM_MULTIPLIERS = (1.0, 0.1, 1.25, 5.0)
METRIC = "codex_turn_token_usage_sum"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="CSV file containing date,usd columns")
    parser.add_argument(
        "--prometheus-url",
        default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
        help="Prometheus base URL (default: PROMETHEUS_URL or http://localhost:9090)",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.6-luna",
        help="Codex model label to query (default: gpt-5.6-luna)",
    )
    parser.add_argument(
        "--timezone",
        default="UTC",
        help="IANA timezone used for daily windows (default: UTC)",
    )
    parser.add_argument(
        "--day-start-hour",
        type=int,
        default=0,
        help="Hour at which a reported day starts, 0-23 (default: 0)",
    )
    return parser.parse_args()


def read_costs(path: Path) -> list[tuple[date, float]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not {"date", "usd"}.issubset(reader.fieldnames):
            raise ValueError("CSV must contain date and usd columns")

        rows = []
        for line_number, row in enumerate(reader, start=2):
            try:
                day = date.fromisoformat((row["date"] or "").strip())
                usd = float((row["usd"] or "").strip())
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid date or usd on CSV line {line_number}") from exc
            if not math.isfinite(usd) or usd < 0:
                raise ValueError(f"usd must be a non-negative number on CSV line {line_number}")
            rows.append((day, usd))

    if not rows:
        raise ValueError("CSV contains no data rows")
    if len({day for day, _ in rows}) != len(rows):
        raise ValueError("CSV contains duplicate dates")
    return sorted(rows)


def query_daily_tokens(
    prometheus_url: str,
    model: str,
    start: datetime,
    end: datetime,
) -> dict[str, float]:
    selector = (
        f'{METRIC}{{model="{model}",token_type=~"'
        + "|".join(TOKEN_TYPES)
        + '"}'
    )
    duration_seconds = int(end.timestamp() - start.timestamp())
    end_timestamp = int(end.timestamp())
    expression = f"sum by (token_type) (increase({selector}[{duration_seconds}s] @ {end_timestamp}))"
    query_url = prometheus_url.rstrip("/") + "/api/v1/query?" + urlencode({"query": expression})

    try:
        with urlopen(query_url, timeout=15) as response:
            payload = json.load(response)
    except OSError as exc:
        raise RuntimeError(f"could not query Prometheus: {exc}") from exc

    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")

    values = {token_type: 0.0 for token_type in TOKEN_TYPES}
    for result in payload.get("data", {}).get("result", []):
        token_type = result.get("metric", {}).get("token_type")
        if token_type in values:
            values[token_type] = float(result["value"][1])
    return values


def solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small square system with Gaussian elimination and pivoting."""
    size = len(vector)
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            raise ValueError("token categories do not vary enough for four independent rates")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                current - factor * pivot_value
                for current, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def fit_independent_rates(samples: list[tuple[list[float], float]]) -> list[float]:
    matrix = [[sum(row[i] * row[j] for row, _ in samples) for j in range(4)] for i in range(4)]
    vector = [sum(row[i] * usd for row, usd in samples) for i in range(4)]
    return solve_linear_system(matrix, vector)


def fit_upstream_rate(samples: list[tuple[list[float], float]]) -> float:
    weighted = [sum(token * multiplier for token, multiplier in zip(row, UPSTREAM_MULTIPLIERS)) for row, _ in samples]
    return sum(total * usd for total, (_, usd) in zip(weighted, samples)) / sum(total * total for total in weighted)


def format_rates(rates: Iterable[float]) -> str:
    return ", ".join(f"{token_type}=${rate:.6g}/MTok" for token_type, rate in zip(TOKEN_TYPES, rates))


def main() -> int:
    args = parse_args()
    if not 0 <= args.day_start_hour <= 23:
        raise ValueError("--day-start-hour must be between 0 and 23")
    try:
        timezone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {args.timezone}") from exc

    costs = read_costs(args.csv)
    samples = []
    print(f"Model: {args.model}")
    print(f"Day boundary: {args.timezone} at {args.day_start_hour:02d}:00")
    print()
    print("date         reported_usd  input       cached_input  cache_write  output")

    for day, usd in costs:
        start = datetime.combine(day, time(args.day_start_hour), timezone)
        end = start + timedelta(days=1)
        tokens = query_daily_tokens(args.prometheus_url, args.model, start, end)
        row = [tokens[token_type] / 1_000_000 for token_type in TOKEN_TYPES]
        samples.append((row, usd))
        print(
            f"{day.isoformat()}  {usd:12.6f}  "
            + "  ".join(f"{tokens[token_type]:10.0f}" for token_type in TOKEN_TYPES)
        )

    weighted_totals = [sum(row[i] * UPSTREAM_MULTIPLIERS[i] for i in range(4)) for row, _ in samples]
    if not any(weighted_totals):
        raise RuntimeError("no matching Codex token telemetry found for the supplied dates")

    base_rate = fit_upstream_rate(samples)
    print()
    print("Upstream-ratio estimate (cached input=.1x, cache write=1.25x, output=5x):")
    print(f"  base input rate: ${base_rate:.6g}/MTok")
    print(f"  implied rates:   {format_rates([base_rate * multiplier for multiplier in UPSTREAM_MULTIPLIERS])}")

    if len(samples) < 4:
        print("\nIndependent four-rate estimate: unavailable; need at least four days.")
    else:
        try:
            rates = fit_independent_rates(samples)
        except ValueError as exc:
            print(f"\nIndependent four-rate estimate: unavailable; {exc}.")
        else:
            print("\nIndependent four-rate estimate:")
            print(f"  {format_rates(rates)}")
            print("  Treat this as unstable unless the daily token mixes differ substantially.")

    observed_scales = [usd / total for total, (_, usd) in zip(weighted_totals, samples) if total > 0]
    print(f"\nUpstream-ratio daily base-rate median: ${statistics.median(observed_scales):.6g}/MTok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
