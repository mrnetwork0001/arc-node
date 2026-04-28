#!/usr/bin/env python3
"""Analyze arc-engine-bench CSV output and emit JSON to stdout.

Usage:

    engine-bench-report.py <results_dir> [--markdown <path>]

Reads `summary.csv` and `combined_latency.csv` from `results_dir` (either
may be absent), resolves a report status (`normal`/`aggregate_only`/
`partial`/`no_data`/`error`), computes derived stats, and prints a JSON
analysis document to stdout containing the report status, summary,
analysis, flags, and any parse errors. Callers (e.g., CI) parse the
JSON directly.

With `--markdown <path>`, also renders a markdown report at that path.

CSV schemas and percentile implementation mirror
`crates/engine-bench/src/bench/output.rs`.

Exit codes:
    0  JSON printed (and markdown written if requested)
    1  results_dir does not exist
    2  bad usage / argparse
    3  markdown write failed after JSON was emitted
"""

import argparse
import csv
import json
import math
import os
import sys
import traceback

WINDOW_SIZE_HIGH = 1500  # ≥ WINDOW_SIZE_HIGH → 10 windows
WINDOW_SIZE_LOW = 600    # ≥ WINDOW_SIZE_LOW  →  5 windows; below → no windowed trend
OUTLIER_MULTIPLIER = 5   # per-block outlier: total_ms > OUTLIER_MULTIPLIER × median(total_ms)
TOP_OUTLIERS_LIMIT = 10
TAIL_LATENCY_RATIO = 3   # p99/p50 threshold for a Flag
PER_BLOCK_OUTLIER_LIST_LIMIT = 3

REPORT_STATUS_NORMAL = "normal"
REPORT_STATUS_AGGREGATE_ONLY = "aggregate_only"
REPORT_STATUS_PARTIAL = "partial"
REPORT_STATUS_NO_DATA = "no_data"
REPORT_STATUS_ERROR = "error"

SUMMARY_STATUS_OK = "ok"
SUMMARY_STATUS_MISSING = "missing"
SUMMARY_STATUS_ZERO_BYTES = "zero_bytes"
SUMMARY_STATUS_HEADER_ONLY = "header_only"
SUMMARY_STATUS_PARSE_FAILED = "parse_failed"

# CombinedLatencyRow — crates/engine-bench/src/bench/output.rs:29-42.
EXPECTED_COMBINED_COLUMNS = [
    "block_number", "block_hash", "tx_count", "gas_used",
    "new_payload_ms", "fcu_ms", "total_ms", "elapsed_ms",
    "mgas_per_s", "tx_per_s",
    "cumulative_mgas_per_s", "cumulative_tx_per_s",
]

# SummaryRow — crates/engine-bench/src/bench/output.rs:45-66.
SUMMARY_REQUIRED_INT_COLUMNS = ["samples", "total_gas", "total_txs"]
SUMMARY_REQUIRED_FLOAT_COLUMNS = [
    "wall_clock_ms", "execution_ms", "avg_total_ms",
    "avg_mgas_per_s", "avg_tx_per_s",
    "p50_total_ms", "p95_total_ms", "p99_total_ms",
]
SUMMARY_OPTIONAL_COLUMNS = [
    "avg_new_payload_ms", "avg_fcu_ms",
    "p50_new_payload_ms", "p95_new_payload_ms", "p99_new_payload_ms",
    "p50_fcu_ms", "p95_fcu_ms", "p99_fcu_ms",
]


# ----- percentile + stats (pinned to Rust parity — do not modify) -----

def percentile(sorted_values, q):
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    q = max(0.0, min(1.0, q))
    rank = q * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (rank - lo)


def stats(values):
    if not values:
        return None
    sv = sorted(values)
    return {
        "n": len(values),
        "avg": sum(values) / len(values),
        "p50": percentile(sv, 0.5),
        "p95": percentile(sv, 0.95),
        "p99": percentile(sv, 0.99),
        "max": sv[-1],
    }


# ----- combined_latency.csv parsing -----

def _to_finite_float(s):
    v = float(s)
    if not math.isfinite(v):
        raise ValueError(f"non-finite float: {s!r}")
    return v


def _parse_combined_row(r):
    # csv::Writer in output.rs buffers writes (flushed on Drop); a crashed
    # bench can leave the final row byte-truncated. Missing cells, and
    # non-finite floats from corrupt cells, signal a torn/malformed row —
    # caller drops it so aggregates stay clean.
    for col in EXPECTED_COMBINED_COLUMNS:
        if r.get(col) in (None, ""):
            return None
    try:
        return {
            "block_number": int(r["block_number"]),
            "tx_count": int(r["tx_count"]),
            "gas_used": int(r["gas_used"]),
            "new_payload_ms": _to_finite_float(r["new_payload_ms"]),
            "fcu_ms": _to_finite_float(r["fcu_ms"]),
            "total_ms": _to_finite_float(r["total_ms"]),
            "elapsed_ms": _to_finite_float(r["elapsed_ms"]),
            "mgas_per_s": _to_finite_float(r["mgas_per_s"]),
            "tx_per_s": _to_finite_float(r["tx_per_s"]),
            "cumulative_mgas_per_s": _to_finite_float(r["cumulative_mgas_per_s"]),
            "cumulative_tx_per_s": _to_finite_float(r["cumulative_tx_per_s"]),
        }
    except ValueError:
        return None


def _load_combined_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        try:
            for row in reader:
                rows.append(row)
        except csv.Error as e:
            raise csv.Error(f"line {reader.line_num}: {e}") from e
        return rows


def _compute_windows(n, block_num, new_payload_ms):
    if n >= WINDOW_SIZE_HIGH:
        n_windows = 10
    elif n >= WINDOW_SIZE_LOW:
        n_windows = 5
    else:
        return 0, []

    window_size = n // n_windows
    windows = []
    for i in range(n_windows):
        lo = i * window_size
        hi = (i + 1) * window_size if i < n_windows - 1 else n
        slice_np = new_payload_ms[lo:hi]
        sv = sorted(slice_np)
        windows.append({
            "first_block": block_num[lo],
            "last_block": block_num[hi - 1],
            "avg": sum(slice_np) / len(slice_np),
            "p50": percentile(sv, 0.5),
            "p95": percentile(sv, 0.95),
        })
    return n_windows, windows


def _compute_per_block_outliers(block_num, total_ms, median_total):
    if median_total <= 0:
        return []
    threshold = OUTLIER_MULTIPLIER * median_total
    return [
        {"block": blk, "total_ms": v, "median": median_total}
        for blk, v in zip(block_num, total_ms)
        if v > threshold
    ]


def _compute_throughput(cum_mgas_last, cum_tx_last, total_gas, total_txs, last_elapsed_ms):
    # cumulative_* in output.rs are denominator-normalised over run elapsed,
    # so the final row's cumulative is the run-wide average. Fall back to
    # totals when the bench crashed before that denominator became non-zero.
    last_elapsed_s = last_elapsed_ms / 1000.0 if last_elapsed_ms > 0 else 0.0
    if cum_mgas_last <= 0.0 and last_elapsed_s > 0.0:
        return (
            total_gas / last_elapsed_s / 1_000_000.0,
            total_txs / last_elapsed_s,
            "recomputed",
        )
    return cum_mgas_last, cum_tx_last, "cumulative"


def analyze(raw_rows):
    parsed = [_parse_combined_row(r) for r in raw_rows]
    n_raw = len(parsed)
    # Only the trailing row may be torn (csv::Writer was mid-flush when the
    # bench crashed). Earlier Nones signal schema drift or corruption and
    # must be surfaced distinctly so a systematic mismatch is not masked.
    dropped_torn_rows = 1 if n_raw > 0 and parsed[-1] is None else 0
    dropped_malformed_rows = sum(1 for p in parsed[:-1] if p is None)
    rows = [r for r in parsed if r is not None]
    n = len(rows)

    if n == 0:
        return {
            "samples": 0,
            "n_raw": n_raw,
            "dropped_torn_rows": dropped_torn_rows,
            "dropped_malformed_rows": dropped_malformed_rows,
            "empty": True,
        }

    cols = {k: [r[k] for r in rows] for k in rows[0]}
    block_num = cols["block_number"]
    tx_count = cols["tx_count"]
    gas_used = cols["gas_used"]
    new_payload_ms = cols["new_payload_ms"]
    fcu_ms = cols["fcu_ms"]
    total_ms = cols["total_ms"]
    elapsed_ms = cols["elapsed_ms"]
    mgas_per_s = cols["mgas_per_s"]
    tx_per_s = cols["tx_per_s"]

    n_tx_gt_0 = sum(1 for c in tx_count if c > 0)
    n_tx_eq_0 = n - n_tx_gt_0

    np_tx_gt_0 = [v for v, c in zip(new_payload_ms, tx_count) if c > 0]
    np_tx_eq_0 = [v for v, c in zip(new_payload_ms, tx_count) if c == 0]

    indexed = sorted(range(n), key=lambda i: (-new_payload_ms[i], block_num[i]))
    top_outliers = [
        {
            "block": block_num[i],
            "new_payload_ms": new_payload_ms[i],
            "tx": tx_count[i],
            "gas": gas_used[i],
        }
        for i in indexed[:TOP_OUTLIERS_LIMIT]
    ]

    n_windows, windows = _compute_windows(n, block_num, new_payload_ms)

    sv_total = sorted(total_ms)
    median_total = percentile(sv_total, 0.5)
    per_block_outliers = _compute_per_block_outliers(block_num, total_ms, median_total)

    total_gas = sum(gas_used)
    total_txs = sum(tx_count)
    last_elapsed_ms = elapsed_ms[-1]
    avg_mgas_per_s, avg_tx_per_s, throughput_source = _compute_throughput(
        cols["cumulative_mgas_per_s"][-1], cols["cumulative_tx_per_s"][-1],
        total_gas, total_txs, last_elapsed_ms,
    )

    sv_np = sorted(new_payload_ms)
    sv_fcu = sorted(fcu_ms)
    partial_headline = {
        "samples": n,
        "total_gas": total_gas,
        "total_txs": total_txs,
        "last_sampled_elapsed_ms": last_elapsed_ms,
        "avg_new_payload_ms": sum(new_payload_ms) / n,
        "avg_fcu_ms": sum(fcu_ms) / n,
        "avg_total_ms": sum(total_ms) / n,
        "avg_mgas_per_s": avg_mgas_per_s,
        "avg_tx_per_s": avg_tx_per_s,
        "throughput_source": throughput_source,
        "p50_new_payload_ms": percentile(sv_np, 0.5),
        "p95_new_payload_ms": percentile(sv_np, 0.95),
        "p99_new_payload_ms": percentile(sv_np, 0.99),
        "p50_fcu_ms": percentile(sv_fcu, 0.5),
        "p95_fcu_ms": percentile(sv_fcu, 0.95),
        "p99_fcu_ms": percentile(sv_fcu, 0.99),
        "p50_total_ms": percentile(sv_total, 0.5),
        "p95_total_ms": percentile(sv_total, 0.95),
        "p99_total_ms": percentile(sv_total, 0.99),
    }

    throughput_tx_bearing = None
    if n_tx_gt_0 > 0:
        throughput_tx_bearing = {
            "mgas_per_s": stats([v for v, c in zip(mgas_per_s, tx_count) if c > 0]),
            "tx_per_s": stats([v for v, c in zip(tx_per_s, tx_count) if c > 0]),
        }

    return {
        "samples": n,
        "n_raw": n_raw,
        "dropped_torn_rows": dropped_torn_rows,
        "dropped_malformed_rows": dropped_malformed_rows,
        "n_tx_gt_0": n_tx_gt_0,
        "n_tx_eq_0": n_tx_eq_0,
        "per_class": {
            "all": stats(new_payload_ms),
            "tx_gt_0": stats(np_tx_gt_0),
            "tx_eq_0": stats(np_tx_eq_0),
        },
        "top_outliers": top_outliers,
        "top_outliers_count": len(top_outliers),
        "n_windows": n_windows,
        "windows": windows,
        "throughput_tx_bearing": throughput_tx_bearing,
        "per_block_outliers": per_block_outliers,
        "partial_headline": partial_headline,
    }


# ----- summary.csv parsing -----

def _coerce(raw, cast):
    """Convert `raw` via `cast`; return (value, reason).

    `reason` is None on success, "empty" for empty/None input, or
    "non_numeric" when the cast raised ValueError.
    """
    if raw is None or raw == "":
        return None, "empty"
    try:
        return cast(raw), None
    except ValueError:
        return None, "non_numeric"


def parse_summary_csv(path):
    """Parse summary.csv. Returns (dict | None, malformed_columns, status).

    `malformed_columns` is a list of {column, reason, raw_value} dicts.
    status is one of SUMMARY_STATUS_OK, SUMMARY_STATUS_ZERO_BYTES, SUMMARY_STATUS_HEADER_ONLY.
    Raises OSError / UnicodeDecodeError / csv.Error on read failure.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames
    if not rows:
        return None, [], SUMMARY_STATUS_HEADER_ONLY if fieldnames else SUMMARY_STATUS_ZERO_BYTES

    r = rows[0]
    malformed = []

    def record_malformed(col, reason, raw):
        malformed.append({"column": col, "reason": reason, "raw_value": raw})

    out = {"mode": r.get("mode") or None}
    if not out["mode"]:
        record_malformed("mode", "empty", r.get("mode"))

    for col in SUMMARY_REQUIRED_INT_COLUMNS:
        raw = r.get(col)
        out[col], reason = _coerce(raw, int)
        if reason is not None:
            record_malformed(col, reason, raw)
    for col in SUMMARY_REQUIRED_FLOAT_COLUMNS:
        raw = r.get(col)
        out[col], reason = _coerce(raw, float)
        if reason is not None:
            record_malformed(col, reason, raw)
    for col in SUMMARY_OPTIONAL_COLUMNS:
        raw = r.get(col)
        if raw is None or raw == "":
            out[col] = None
        else:
            out[col], reason = _coerce(raw, float)
            if reason == "non_numeric":
                record_malformed(col, reason, raw)

    return out, malformed, SUMMARY_STATUS_OK


# ----- report status resolution -----

def resolve_report_status(summary, latency):
    """Resolve report status from parsed inputs.

    `summary` is None when summary.csv is absent or parsed to None.
    `latency` is None when combined_latency.csv is absent, or the dict
    returned by analyze() otherwise (which may have samples == 0).
    """
    has_summary = summary is not None
    has_latency = latency is not None and latency.get("samples", 0) > 0
    if has_summary and has_latency:
        return REPORT_STATUS_NORMAL
    if has_summary and not has_latency:
        return REPORT_STATUS_AGGREGATE_ONLY
    if not has_summary and has_latency:
        return REPORT_STATUS_PARTIAL
    return REPORT_STATUS_NO_DATA


# ----- flag computation -----

def compute_flags(
    report_status,
    analysis,
    summary,
    summary_malformed,
    analyze_error,
    summary_status=SUMMARY_STATUS_MISSING,
    summary_error=None,
):
    flags = []

    if summary_status == SUMMARY_STATUS_ZERO_BYTES:
        flags.append("⚠ summary.csv present but zero bytes")
    elif summary_status == SUMMARY_STATUS_HEADER_ONLY:
        flags.append(
            "⚠ summary.csv has header but no data row — bench may have "
            "failed before writing summary"
        )

    if summary_error is not None:
        etype, emsg = summary_error
        flags.append(f"⚠ summary.csv parse failed: {etype}: {emsg}")

    if report_status == REPORT_STATUS_PARTIAL:
        flags.append(
            f"⚠ partial run: summary.csv missing, N={analysis['samples']} blocks in combined_latency.csv"
        )

    if analysis is not None:
        n_torn = analysis.get("dropped_torn_rows", 0)
        if n_torn > 0:
            s = "" if n_torn == 1 else "s"
            flags.append(f"⚠ dropped {n_torn} torn trailing row{s} from combined_latency.csv")

        n_malformed = analysis.get("dropped_malformed_rows", 0)
        if n_malformed > 0:
            s = "" if n_malformed == 1 else "s"
            flags.append(
                f"⚠ dropped {n_malformed} malformed mid-file row{s} from combined_latency.csv "
                "— possible schema drift"
            )

        if not analyze_error:
            n_raw = analysis.get("n_raw", 0)
            samples = analysis.get("samples", 0)
            if samples == 0 and n_raw > 0:
                flags.append(
                    f"⚠ all {n_raw} row{'s' if n_raw != 1 else ''} in "
                    "combined_latency.csv were malformed — schema drift likely"
                )
            elif samples == 0 and n_raw == 0:
                flags.append("⚠ combined_latency.csv had no data rows (header-only)")

    if analyze_error is not None:
        etype, emsg = analyze_error
        flags.append(f"⚠ combined_latency.csv parse failed: {etype}: {emsg}")

    partial_headline = (analysis or {}).get("partial_headline") or {}
    throughput_recomputed = partial_headline.get("throughput_source") == "recomputed"
    if report_status == REPORT_STATUS_PARTIAL and throughput_recomputed:
        flags.append("⚠ cumulative throughput column was zero; recomputed from totals")
    elif throughput_recomputed and report_status in (REPORT_STATUS_NORMAL, REPORT_STATUS_AGGREGATE_ONLY):
        flags.append(
            "⚠ combined_latency.csv cumulative throughput column was zero "
            "— headline uses summary.csv, but latency data may be corrupt"
        )

    if analysis is not None:
        samples = analysis.get("samples", 0)
        if 0 < samples < 10:
            flags.append(
                f"⚠ only {samples} block{'s' if samples != 1 else ''} sampled "
                "— percentile statistics are degenerate"
            )

    tail_source = partial_headline if report_status == REPORT_STATUS_PARTIAL else (summary or {})
    p50_total = tail_source.get("p50_total_ms")
    p99_total = tail_source.get("p99_total_ms")
    if p50_total is not None and p99_total is not None and p50_total > 0:
        ratio = p99_total / p50_total
        if ratio > TAIL_LATENCY_RATIO:
            flags.append(f"⚠ tail-latency divergence: p99/p50={ratio:.2f}")

    if analysis is not None:
        outliers = analysis.get("per_block_outliers", [])
        shown = outliers[:PER_BLOCK_OUTLIER_LIST_LIMIT]
        for o in shown:
            flags.append(
                f"⚠ per-block outlier: block {o['block']}, "
                f"total_ms={o['total_ms']:.1f}, median={o['median']:.1f}"
            )
        rest = len(outliers) - len(shown)
        if rest > 0:
            flags.append(f"…and {rest} more.")

    if report_status in (REPORT_STATUS_NORMAL, REPORT_STATUS_AGGREGATE_ONLY):
        for item in summary_malformed:
            col, reason, raw = item["column"], item["reason"], item["raw_value"]
            if reason == "empty":
                flags.append(f"⚠ malformed summary: {col} empty")
            else:
                flags.append(
                    f"⚠ malformed summary: {col} non-numeric ({raw!r})"
                )

    return flags


# ----- formatters -----

def _fmt_or_dash(fn, v):
    if v is None:
        return "—"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return "—"
    return fn(v)


def _fmt_int(n):
    return f"{int(n)}"


def _fmt_int_grouped(n):
    n = int(n)
    return f"{n:,}" if n >= 10_000 else f"{n}"


def _fmt_gas_used(n):
    return f"{int(n):,}"


def _fmt_total_gas(n):
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.2f} Ggas"
    if n >= 1e6:
        return f"{n / 1e6:.1f} Mgas"
    return f"{int(n):,} gas"


def _fmt_ms(v):
    return f"{float(v):.1f} ms"


def _fmt_1dp(v):
    return f"{float(v):.1f}"


def _fmt_mgas_per_s(v):
    return f"{float(v):.1f} Mgas/s"


def _fmt_wall_clock_ms(ms):
    seconds = float(ms) / 1000.0
    if seconds >= 10_000:
        return f"{seconds:,.1f} s"
    return f"{seconds:.1f} s"


# ----- report rendering -----

def _render_minimal_report(flags):
    lines = [
        "# arc-engine-bench: no data",
        "",
        "_No benchmark output was available — EaaS may not have produced "
        "results, the run aborted before the first block, or the CSV was "
        "truncated._",
        "",
    ]
    lines.extend(_render_flags(flags))
    return "\n".join(lines) + "\n"


def _render_partial_banner(analysis):
    return [
        "> ⚠ **Partial results** — `summary.csv` not found. The bench run likely errored",
        "> or was aborted. Headline numbers below are derived from `combined_latency.csv`",
        f"> ({analysis['samples']} blocks); percentiles recomputed from per-block samples.",
        "",
    ]


def _render_title_and_workload(report_status, analysis, summary):
    if report_status == REPORT_STATUS_PARTIAL:
        ph = analysis["partial_headline"]
        title_mode = "unknown (partial run)"
        samples = ph["samples"]
        total_gas = ph["total_gas"]
        total_txs = ph["total_txs"]
        elapsed_label = "last-sampled elapsed"
        elapsed_value = _fmt_or_dash(_fmt_wall_clock_ms, ph["last_sampled_elapsed_ms"])
    else:
        title_mode = summary.get("mode") or "unknown"
        samples = summary.get("samples")
        total_gas = summary.get("total_gas")
        total_txs = summary.get("total_txs")
        elapsed_label = "wall clock"
        elapsed_value = _fmt_or_dash(_fmt_wall_clock_ms, summary.get("wall_clock_ms"))

    if analysis is not None:
        classes_line = (
            f"tx-bearing: {_fmt_int_grouped(analysis['n_tx_gt_0'])} · "
            f"empty: {_fmt_int_grouped(analysis['n_tx_eq_0'])} · "
        )
    else:
        classes_line = ""

    return [
        f"# arc-engine-bench: `{title_mode}`",
        "",
        f"Samples: {_fmt_or_dash(_fmt_int_grouped, samples)} blocks · {classes_line}"
        f"total gas: {_fmt_or_dash(_fmt_total_gas, total_gas)} · "
        f"total tx: {_fmt_or_dash(_fmt_int_grouped, total_txs)} · "
        f"{elapsed_label}: {elapsed_value}.",
        "",
    ]


def _render_headline(report_status, analysis, summary):
    if report_status == REPORT_STATUS_PARTIAL:
        src = analysis["partial_headline"]
        elapsed_ms = src["last_sampled_elapsed_ms"]
        elapsed_label = "Last-sampled elapsed"
        recomputed = src["throughput_source"] == "recomputed"
        # Partial throughput is bench-elapsed averaged, not wall-clock;
        # footnote keeps the semantic gap with normal mode explicit.
        throughput_label = "Throughput (avg)†"
        if recomputed:
            footnote = (
                "† throughput recomputed from totals (cumulative column was zero); "
                "basis is last-sampled elapsed, not wall clock"
            )
        else:
            footnote = "† throughput basis is last-sampled elapsed, not wall clock"
    else:
        src = summary
        elapsed_ms = summary.get("wall_clock_ms")
        elapsed_label = "Wall clock"
        throughput_label = "Throughput (avg)"
        footnote = None

    rows = [
        (elapsed_label, _fmt_wall_clock_ms, elapsed_ms),
        (throughput_label, _fmt_mgas_per_s, src.get("avg_mgas_per_s")),
        ("Tx/s (avg)", _fmt_1dp, src.get("avg_tx_per_s")),
        ("`new_payload` avg", _fmt_ms, src.get("avg_new_payload_ms")),
        ("`new_payload` p50", _fmt_ms, src.get("p50_new_payload_ms")),
        ("`new_payload` p95", _fmt_ms, src.get("p95_new_payload_ms")),
        ("`new_payload` p99", _fmt_ms, src.get("p99_new_payload_ms")),
        ("`fcu` avg", _fmt_ms, src.get("avg_fcu_ms")),
        ("`fcu` p99", _fmt_ms, src.get("p99_fcu_ms")),
    ]

    lines = ["## Headline", "", "| Metric | Value |", "|---|---:|"]
    for label, fmt, value in rows:
        lines.append(f"| {label} | {_fmt_or_dash(fmt, value)} |")
    if footnote is not None:
        lines.append("")
        lines.append(footnote)
    lines.append("")
    return lines


def _skip_section(heading, what):
    return [
        heading,
        "",
        f"_combined_latency.csv not available — skipping {what}._",
        "",
    ]


def _render_per_window(analysis):
    heading = "## Per-window trend (`new_payload_ms`)"
    if analysis is None:
        return _skip_section(heading, "per-window trend")
    lines = [heading, ""]
    if analysis.get("n_windows", 0) == 0:
        lines.append("_Run too short for windowed trend._")
        lines.append("")
        return lines

    lines.append("| Blocks | avg | p50 | p95 |")
    lines.append("|---|---:|---:|---:|")
    for w in analysis["windows"]:
        lines.append(
            f"| {_fmt_int(w['first_block'])}–{_fmt_int(w['last_block'])} "
            f"| {_fmt_1dp(w['avg'])} "
            f"| {_fmt_1dp(w['p50'])} "
            f"| {_fmt_1dp(w['p95'])} |"
        )
    lines.append("")
    return lines


def _render_top_outliers(analysis):
    if analysis is None:
        return _skip_section("## Top `new_payload` outliers", "per-block outliers")
    count = analysis.get("top_outliers_count", 0)
    lines = [f"## Top `new_payload` outliers (N={count})", ""]
    if count == 0:
        lines.append("_No samples to rank._")
        lines.append("")
        return lines
    lines.append("| Block | new_payload_ms | tx | gas |")
    lines.append("|---:|---:|---:|---:|")
    for o in analysis["top_outliers"]:
        lines.append(
            f"| {_fmt_int(o['block'])} "
            f"| {_fmt_1dp(o['new_payload_ms'])} "
            f"| {_fmt_int_grouped(o['tx'])} "
            f"| {_fmt_gas_used(o['gas'])} |"
        )
    lines.append("")
    return lines


def _render_per_class_entry(label, cls):
    if cls is None:
        return f"| {label} | — | — | — | — | — | — |"
    return (
        f"| {label} | {_fmt_int_grouped(cls['n'])} "
        f"| {_fmt_1dp(cls['avg'])} "
        f"| {_fmt_1dp(cls['p50'])} "
        f"| {_fmt_1dp(cls['p95'])} "
        f"| {_fmt_1dp(cls['p99'])} "
        f"| {_fmt_1dp(cls['max'])} |"
    )


def _render_per_class(analysis):
    if analysis is None:
        return _skip_section("## Per-class breakdown (`new_payload_ms`)", "per-class breakdown")
    pc = analysis["per_class"]
    lines = [
        "## Per-class breakdown (`new_payload_ms`)",
        "",
        "| Class | n | avg | p50 | p95 | p99 | max |",
        "|---|---:|---:|---:|---:|---:|---:|",
        _render_per_class_entry("all", pc["all"]),
        _render_per_class_entry("tx > 0", pc["tx_gt_0"]),
        _render_per_class_entry("tx = 0", pc["tx_eq_0"]),
        "",
    ]

    tb = analysis.get("throughput_tx_bearing")
    lines.append("**Throughput on tx-bearing blocks**")
    lines.append("")
    if tb is None:
        lines.append("_No tx-bearing blocks in run._")
        lines.append("")
    else:
        lines.append("| Metric | avg | p50 | p95 |")
        lines.append("|---|---:|---:|---:|")
        for metric_label, key in (("`mgas_per_s`", "mgas_per_s"), ("`tx_per_s`", "tx_per_s")):
            s = tb[key]
            lines.append(
                f"| {metric_label} | {_fmt_1dp(s['avg'])} "
                f"| {_fmt_1dp(s['p50'])} "
                f"| {_fmt_1dp(s['p95'])} |"
            )
        lines.append("")

    lines.append(
        "> The per-class `mgas_per_s` / `tx_per_s` are "
        "**per-block instantaneous** (gas_used ÷ per-block latency), "
        "while the Headline `Throughput (avg)` is "
        "**wall-clock averaged** (total gas ÷ wall clock). "
        "The two series are not directly comparable."
    )
    lines.append("")
    return lines


def _render_flags(flags):
    lines = ["## Flags", ""]
    if not flags:
        lines.append("_No flags._")
    else:
        for f in flags:
            lines.append(f"- {f}")
    lines.append("")
    return lines


def render_report(report_status, analysis, summary, flags):
    if report_status == REPORT_STATUS_NO_DATA:
        return _render_minimal_report(flags)

    lines = []
    if report_status == REPORT_STATUS_PARTIAL:
        lines.extend(_render_partial_banner(analysis))
    lines.extend(_render_title_and_workload(report_status, analysis, summary))
    lines.extend(_render_headline(report_status, analysis, summary))
    lines.extend(_render_per_window(analysis))
    lines.extend(_render_top_outliers(analysis))
    lines.extend(_render_per_class(analysis))
    lines.extend(_render_flags(flags))
    return "\n".join(lines) + "\n"


# ----- orchestration -----

# Read-side errors worth surfacing as "parse failed" flags rather than
# crashing the whole run — callers keep whatever data did parse.
_CSV_READ_ERRORS = (OSError, UnicodeDecodeError, csv.Error)


def _atomic_write(path, text):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _render_error_report(exc_info):
    etype, emsg = exc_info
    lines = [
        "# arc-engine-bench: renderer error",
        "",
        "_The renderer hit an unexpected error while processing the benchmark "
        "output. The CI logs contain the full traceback._",
        "",
        "## Error",
        "",
        f"`{etype}: {emsg}`",
        "",
        "## Flags",
        "",
        "- ⚠ renderer exited with an unexpected error; see CI logs for details.",
        "",
    ]
    return "\n".join(lines) + "\n"


def _run_analysis(results_dir):
    """Parse inputs and return the full analysis document as a dict.

    Raises FileNotFoundError if results_dir does not exist. All other
    parse errors are captured into `summary_error` / `analyze_error`
    fields on the returned dict so the caller can still emit JSON.
    """
    if not os.path.isdir(results_dir):
        raise FileNotFoundError(f"results_dir not found: {results_dir}")

    summary_path = os.path.join(results_dir, "summary.csv")
    combined_path = os.path.join(results_dir, "combined_latency.csv")

    summary = None
    summary_malformed = []
    summary_status = SUMMARY_STATUS_MISSING
    summary_error = None
    if os.path.exists(summary_path):
        try:
            summary, summary_malformed, summary_status = parse_summary_csv(summary_path)
        except _CSV_READ_ERRORS as e:
            summary_error = [type(e).__name__, str(e)]
            summary_status = SUMMARY_STATUS_PARSE_FAILED

    analysis = None
    analyze_error = None
    if os.path.exists(combined_path):
        try:
            analysis = analyze(_load_combined_rows(combined_path))
        except (ValueError, KeyError) + _CSV_READ_ERRORS as e:
            analyze_error = [type(e).__name__, str(e)]

    report_status = resolve_report_status(summary, analysis)
    flags = compute_flags(
        report_status, analysis, summary, summary_malformed, analyze_error,
        summary_status=summary_status,
        summary_error=summary_error,
    )

    return {
        "report_status": report_status,
        "summary": summary,
        "summary_status": summary_status,
        "summary_malformed": summary_malformed,
        "summary_error": summary_error,
        "analysis": analysis,
        "analyze_error": analyze_error,
        "flags": flags,
    }


def _render_markdown(data):
    if data["report_status"] == REPORT_STATUS_ERROR:
        return _render_error_report(data["error"])
    return render_report(
        data["report_status"], data["analysis"], data["summary"], data["flags"],
    )


def _error_document(exc):
    etype, emsg = type(exc).__name__, str(exc)
    return {
        "report_status": REPORT_STATUS_ERROR,
        "error": [etype, emsg],
        "summary": None,
        "summary_status": SUMMARY_STATUS_PARSE_FAILED,
        "summary_malformed": [],
        "summary_error": None,
        "analysis": None,
        "analyze_error": None,
        "flags": [f"⚠ renderer error: {etype}: {emsg}"],
    }


def main(argv):
    parser = argparse.ArgumentParser(
        description="Analyze arc-engine-bench CSV output; print JSON to stdout.",
    )
    parser.add_argument(
        "results_dir",
        help="Directory containing summary.csv and/or combined_latency.csv.",
    )
    parser.add_argument(
        "--markdown",
        metavar="PATH",
        help="Also render a markdown report to PATH.",
    )
    args = parser.parse_args(argv[1:])

    try:
        data = _run_analysis(args.results_dir)
    except FileNotFoundError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    except Exception as e:
        # Emit an error-status document so callers parsing stdout as JSON
        # still get a well-formed payload.
        traceback.print_exc(file=sys.stderr)
        data = _error_document(e)

    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")

    if args.markdown:
        try:
            _atomic_write(args.markdown, _render_markdown(data))
        except OSError as e:
            print(
                f"::error::could not write markdown to {args.markdown}: {e}",
                file=sys.stderr,
            )
            return 3

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
