#!/usr/bin/env python3
"""
check_api.py — Main entry point for the API Optimizer Agent's Stage 1.

Given an endpoint URL and optional source path, it:
  1. Tests if the API is reachable and healthy
  2. Detects the backend language (PHP / Go / migration)
  3. Profiles latency with p50/p95/p99 breakdown
  4. Emits a structured JSON report for Stage 2 (AI analysis)

Usage:
  python3 check_api.py --url https://api.example.com/v1/products
  python3 check_api.py --url https://api.example.com/v1/products --source /path/to/repo
  python3 check_api.py --url https://api.example.com/v1/products --method POST --data '{"id":1}' --headers 'Authorization: Bearer token'
  python3 check_api.py --url https://api.example.com/v1/products --runs 50 --output report.json
"""

import argparse
import json
import os
import subprocess
import sys
import time
import statistics
from datetime import datetime, timezone
from pathlib import Path

# Import our language detector (same directory)
sys.path.insert(0, str(Path(__file__).parent))
from detect_language import detect, DetectionResult


# ─── helpers ──────────────────────────────────────────────────────────────────

def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def ok(msg):    print(color(f"  ✓  {msg}", "92"))
def warn(msg):  print(color(f"  ⚠  {msg}", "93"))
def fail(msg):  print(color(f"  ✗  {msg}", "91"))
def info(msg):  print(f"     {msg}")


# ─── Stage 1a: health check ────────────────────────────────────────────────────

def health_check(url: str, method: str, data: str, headers: list[str]) -> dict:
    """Single curl call: is the API reachable, what does it return?"""
    print(f"\n  {'─'*48}")
    print(f"  HEALTH CHECK")
    print(f"  {'─'*48}")
    info(f"Endpoint : {url}")
    info(f"Method   : {method}")

    cmd = ["curl", "-s", "-i", "--max-time", "15", "-X", method]
    for h in headers:
        cmd += ["-H", h]
    if data:
        cmd += ["-d", data, "-H", "Content-Type: application/json"]
    cmd.append(url)

    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        fail("Request timed out (>15s)")
        return {"reachable": False, "error": "timeout"}
    except FileNotFoundError:
        fail("curl not found — install curl first")
        sys.exit(1)

    elapsed_ms = (time.time() - start) * 1000

    # Split headers from body
    if "\r\n\r\n" in result.stdout:
        header_block, body = result.stdout.split("\r\n\r\n", 1)
    elif "\n\n" in result.stdout:
        header_block, body = result.stdout.split("\n\n", 1)
    else:
        header_block, body = result.stdout, ""

    # Parse status line
    status_line = header_block.splitlines()[0] if header_block else ""
    http_code = 0
    if status_line.startswith("HTTP/"):
        try:
            http_code = int(status_line.split()[1])
        except (IndexError, ValueError):
            pass

    # Parse response headers into dict
    resp_headers = {}
    for line in header_block.splitlines()[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            resp_headers[k.strip().lower()] = v.strip()

    # Validate JSON body
    body_valid_json = False
    body_preview = ""
    try:
        parsed_body = json.loads(body.strip())
        body_valid_json = True
        body_preview = json.dumps(parsed_body, indent=2)[:400]
    except (json.JSONDecodeError, ValueError):
        body_preview = body.strip()[:400]

    # Report
    if http_code == 0:
        fail(f"No response (curl exit {result.returncode}): {result.stderr[:100]}")
        return {"reachable": False, "error": result.stderr}

    status_color = "92" if 200 <= http_code < 300 else ("93" if http_code < 500 else "91")
    print(f"  {color(f'HTTP {http_code}', status_color)}  —  {elapsed_ms:.0f} ms")

    if 200 <= http_code < 300:
        ok("API is reachable and returned success")
    elif http_code == 401:
        warn("HTTP 401 — API reachable but needs auth (add --headers 'Authorization: Bearer <token>')")
    elif http_code == 403:
        warn("HTTP 403 — reachable but forbidden (check permissions or auth header)")
    elif http_code == 404:
        warn("HTTP 404 — endpoint not found (check URL path)")
    elif http_code >= 500:
        fail(f"HTTP {http_code} — server error")

    content_type = resp_headers.get("content-type", "")
    if body_valid_json:
        ok(f"Response is valid JSON ({len(body.encode())} bytes)")
    elif body.strip():
        info(f"Response body ({len(body.encode())} bytes, content-type: {content_type})")

    if body_preview:
        info("Preview:")
        for line in body_preview.splitlines()[:10]:
            info(f"  {line}")
        if body_preview.count('\n') > 10:
            info("  ... (truncated)")

    return {
        "reachable": True,
        "http_code": http_code,
        "latency_ms": round(elapsed_ms, 1),
        "response_headers": resp_headers,
        "body_valid_json": body_valid_json,
        "body_bytes": len(body.encode()),
        "content_type": content_type,
    }


# ─── Stage 1b: language detection ─────────────────────────────────────────────

def run_language_detection(url: str, source_path: str | None, resp_headers: dict) -> dict:
    print(f"\n  {'─'*48}")
    print(f"  LANGUAGE DETECTION")
    print(f"  {'─'*48}")

    result = detect(url=url, source_path=source_path)

    lang_icons = {
        "php": "🐘 PHP",
        "go": "🐹 Go",
        "php-to-go-migration": "🔄 PHP → Go migration",
        "unknown": "❓ Unknown"
    }
    conf_colors = {"high": "92", "medium": "93", "low": "91"}

    lang_display = lang_icons.get(result.language, result.language)
    conf_display = color(result.confidence.upper(), conf_colors.get(result.confidence, "0"))

    print(f"  Language   : {lang_display}")
    print(f"  Confidence : {conf_display}")
    if result.php_files or result.go_files:
        info(f"Source files : {result.php_files} PHP  |  {result.go_files} Go")
    info("Evidence:")
    for s in result.signals:
        info(f"  • {s}")

    if result.language == "php-to-go-migration":
        warn("Migration in progress — profiler will compare PHP and Go handler performance")
    elif result.language == "unknown":
        warn("Could not determine language — provide --source for more accurate detection")

    return {
        "language": result.language,
        "confidence": result.confidence,
        "signals": result.signals,
        "php_files": result.php_files,
        "go_files": result.go_files,
    }


# ─── Stage 1c: latency profiling ──────────────────────────────────────────────

def profile_latency(url: str, method: str, data: str, headers: list[str], runs: int) -> dict:
    print(f"\n  {'─'*48}")
    print(f"  LATENCY PROFILING  ({runs} runs)")
    print(f"  {'─'*48}")

    curl_format = "%{time_namelookup}|%{time_connect}|%{time_appconnect}|%{time_pretransfer}|%{time_starttransfer}|%{time_total}|%{http_code}|%{size_download}"

    cmd_base = ["curl", "-o", "/dev/null", "-s", "--max-time", "30",
                "-w", curl_format, "-X", method]
    for h in headers:
        cmd_base += ["-H", h]
    if data:
        cmd_base += ["-d", data, "-H", "Content-Type: application/json"]
    cmd_base.append(url)

    # warm-up (discarded)
    subprocess.run(cmd_base, capture_output=True, timeout=35)

    measurements = []
    errors = 0

    for i in range(runs):
        try:
            r = subprocess.run(cmd_base, capture_output=True, text=True, timeout=35)
            raw = r.stdout.strip()
            parts = raw.split("|")
            if len(parts) < 8:
                errors += 1
                continue
            code = int(parts[6])
            entry = {
                "dns":   float(parts[0]) * 1000,
                "tcp":   (float(parts[1]) - float(parts[0])) * 1000,
                "tls":   (float(parts[2]) - float(parts[1])) * 1000,
                "app":   (float(parts[4]) - float(parts[3])) * 1000,
                "ttfb":  float(parts[4]) * 1000,
                "total": float(parts[5]) * 1000,
                "code":  code,
                "bytes": int(float(parts[7])),
            }
            measurements.append(entry)
            sys.stdout.write(f"\r  progress: {i+1}/{runs} runs")
            sys.stdout.flush()
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            errors += 1

    print()  # newline after progress

    if not measurements:
        fail("All profiling runs failed")
        return {"error": "all_failed"}

    totals = sorted(d["total"] for d in measurements)
    n = len(totals)

    def pct(arr, p):
        return arr[max(0, min(int(len(arr) * p / 100), len(arr) - 1))]

    avg = lambda key: statistics.mean(d[key] for d in measurements)

    result = {
        "runs": n,
        "errors": errors,
        "p50_ms":  round(pct(totals, 50), 1),
        "p75_ms":  round(pct(totals, 75), 1),
        "p95_ms":  round(pct(totals, 95), 1),
        "p99_ms":  round(pct(totals, 99), 1),
        "mean_ms": round(statistics.mean(totals), 1),
        "min_ms":  round(totals[0], 1),
        "max_ms":  round(totals[-1], 1),
        "layers": {
            "dns_ms":  round(avg("dns"),  1),
            "tcp_ms":  round(avg("tcp"),  1),
            "tls_ms":  round(avg("tls"),  1),
            "app_ms":  round(avg("app"),  1),
            "body_ms": round(avg("total") - avg("ttfb"), 1),
        }
    }

    # Display
    print(f"  {'Metric':<10}  {'ms':>8}")
    print(f"  {'─'*22}")
    print(f"  {'p50':<10}  {result['p50_ms']:>8.1f}")
    print(f"  {'p75':<10}  {result['p75_ms']:>8.1f}")
    print(f"  {'p95':<10}  {result['p95_ms']:>8.1f}")
    print(f"  {'p99':<10}  {result['p99_ms']:>8.1f}")
    print(f"  {'mean':<10}  {result['mean_ms']:>8.1f}")
    print()
    print(f"  LAYER BREAKDOWN (mean)")
    print(f"  {'─'*22}")
    layers = result["layers"]
    total_mean = result["mean_ms"]
    for layer, key in [("DNS", "dns_ms"), ("TCP", "tcp_ms"), ("TLS", "tls_ms"),
                        ("App logic", "app_ms"), ("Body xfer", "body_ms")]:
        ms = layers[key]
        pct_share = (ms / total_mean * 100) if total_mean > 0 else 0
        bar = "█" * int(pct_share / 5)
        marker = " ← BOTTLENECK" if key == "app_ms" and pct_share > 60 else ""
        print(f"  {layer:<10}  {ms:>6.1f} ms  {bar:<20} {pct_share:4.0f}%{marker}")

    # Threshold warnings
    print()
    if result["p95_ms"] > 500:
        warn(f"p95 {result['p95_ms']}ms is above 500ms — will impact user experience")
    elif result["p95_ms"] > 200:
        warn(f"p95 {result['p95_ms']}ms is above 200ms — acceptable but watch for growth")
    else:
        ok(f"p95 {result['p95_ms']}ms is within healthy range (<200ms)")

    if errors > 0:
        warn(f"{errors}/{runs + errors} requests errored out")

    return result


# ─── Stage 1 report ────────────────────────────────────────────────────────────

def build_report(url, method, health, lang, latency) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": url,
        "method": method,
        "health": health,
        "language_detection": lang,
        "latency_profile": latency,
        "next_step": (
            "php_optimization" if lang.get("language") == "php" else
            "go_optimization"  if lang.get("language") == "go"  else
            "migration_analysis" if lang.get("language") == "php-to-go-migration" else
            "manual_review"
        ),
    }


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="API Optimizer — Stage 1: Health check, language detection, latency profiling"
    )
    parser.add_argument("--url",     "-u", required=True,  help="API endpoint URL")
    parser.add_argument("--source",  "-s",                  help="Path to source code directory")
    parser.add_argument("--method",  "-m", default="GET",   help="HTTP method (default: GET)")
    parser.add_argument("--data",    "-d", default="",      help="Request body (JSON string)")
    parser.add_argument("--headers", "-H", default="",
                        help="Extra headers, pipe-separated: 'Authorization: Bearer x|X-Trace: y'")
    parser.add_argument("--runs",    "-r", type=int, default=20,
                        help="Number of profiling runs (default: 20)")
    parser.add_argument("--output",  "-o", default="",
                        help="Write JSON report to file (e.g. report.json)")
    args = parser.parse_args()

    extra_headers = [h.strip() for h in args.headers.split("|") if h.strip()] if args.headers else []

    print()
    print(color("  ╔══════════════════════════════════════════════╗", "96"))
    print(color("  ║     API OPTIMIZER — STAGE 1: ANALYSIS       ║", "96"))
    print(color("  ╚══════════════════════════════════════════════╝", "96"))

    health  = health_check(args.url, args.method, args.data, extra_headers)
    lang    = run_language_detection(args.url, args.source, health.get("response_headers", {}))

    latency = {}
    if health.get("reachable") and health.get("http_code", 0) < 500:
        latency = profile_latency(args.url, args.method, args.data, extra_headers, args.runs)
    else:
        warn("Skipping latency profiling — API returned error or is unreachable")

    report = build_report(args.url, args.method, health, lang, latency)

    print(f"\n  {'─'*48}")
    print(f"  NEXT STEP: {report['next_step'].upper().replace('_', ' ')}")
    print(f"  {'─'*48}\n")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        ok(f"Report saved to: {args.output}")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
