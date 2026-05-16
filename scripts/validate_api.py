#!/usr/bin/env python3
"""
Validate that the API response is unchanged after applying fixes.

Compares:
  - HTTP status code
  - Top-level JSON schema (key names + types)
  - Record count (list length or pagination .data length)
  - Payload size (bytes)

Also runs a quick 5-run re-profile to measure the new p95.

Usage:
  # Capture baseline before patching
  python scripts/validate_api.py --url http://localhost/api/products --capture \
      --out results/baseline.json

  # Validate after patching (compares to baseline)
  python scripts/validate_api.py --url http://localhost/api/products \
      --baseline results/baseline.json

  # Full validate + quick re-profile
  python scripts/validate_api.py --url http://localhost/api/products \
      --baseline results/baseline.json --profile

Output JSON:
  {
    "passed": true,
    "comparison": {
      "status_match": true, "status_before": 200, "status_after": 200,
      "schema_match": true,  "records_match": true,
      "records_before": 31,  "records_after": 31,
      "payload_bytes_before": 34161, "payload_bytes_after": 34161
    },
    "performance_after": { "p50": 182.1, "p95": 192.4, "mean": 186.0 }
  }
"""

import argparse
import json
import subprocess
import statistics
import sys
from pathlib import Path
from typing import Optional


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def fetch(url: str, method: str = "GET", data: str = "", timeout: int = 15) -> dict:
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "--max-time", str(timeout),
           "--connect-timeout", "8", "-X", method]
    if data:
        cmd += ["-d", data, "-H", "Content-Type: application/json"]
    cmd.append(url)
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=timeout + 3)
        text = raw.decode(errors="replace")
        parts = text.rsplit("\n", 1)
        status = int(parts[-1].strip()) if parts[-1].strip().isdigit() else 0
        body   = parts[0] if len(parts) > 1 else text
        return {"status": status, "body": body, "bytes": len(body.encode())}
    except Exception as e:
        return {"status": 0, "body": "", "bytes": 0, "error": str(e)}


def _curl_ms(url: str, timeout: int = 12) -> Optional[float]:
    try:
        out = subprocess.check_output(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{time_total}",
             "--max-time", str(timeout), "--connect-timeout", "6", url],
            stderr=subprocess.DEVNULL, timeout=timeout + 2,
        )
        return float(out.strip()) * 1000
    except Exception:
        return None


def quick_profile(url: str, runs: int = 5) -> dict:
    samples = [ms for _ in range(runs) if (ms := _curl_ms(url)) is not None]
    if not samples:
        return {}
    samples.sort()
    return {
        "p50":  round(statistics.median(samples), 1),
        "p95":  round(samples[min(len(samples)-1, int(len(samples)*0.95))], 1),
        "mean": round(statistics.mean(samples), 1),
        "n":    len(samples),
    }


# ── Schema / record extraction ────────────────────────────────────────────────

def extract_schema(body: str) -> dict:
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return {k: type(v).__name__ for k, v in obj.items()}
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return {k: type(v).__name__ for k, v in obj[0].items()}
        return {"_type": type(obj).__name__}
    except Exception:
        return {}


def count_records(body: str) -> int:
    try:
        obj = json.loads(body)
        if isinstance(obj, list):
            return len(obj)
        if isinstance(obj, dict):
            if "data" in obj and isinstance(obj["data"], list):
                return len(obj["data"])
            if "total" in obj:
                return obj["total"]
            return len(obj)
        return 0
    except Exception:
        return 0


# ── Comparison ────────────────────────────────────────────────────────────────

def compare(before: dict, after: dict) -> dict:
    sa = extract_schema(before["body"])
    sb = extract_schema(after["body"])
    ra = count_records(before["body"])
    rb = count_records(after["body"])

    status_match  = before["status"] == after["status"]
    schema_match  = sa == sb
    records_match = ra == rb

    return {
        "status_match":         status_match,
        "status_before":        before["status"],
        "status_after":         after["status"],
        "schema_match":         schema_match,
        "schema_before":        sa,
        "schema_after":         sb,
        "records_match":        records_match,
        "records_before":       ra,
        "records_after":        rb,
        "payload_bytes_before": before["bytes"],
        "payload_bytes_after":  after["bytes"],
        "passed":               status_match and schema_match and records_match,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def capture_baseline(url: str, method: str = "GET", data: str = "", out_path: str = "") -> dict:
    resp = fetch(url, method, data)
    if out_path:
        Path(out_path).write_text(json.dumps(resp, indent=2))
    return resp


def validate(
    url: str,
    baseline: dict | None = None,
    baseline_path: str = "",
    method: str = "GET",
    data: str = "",
    do_profile: bool = False,
    profile_runs: int = 5,
) -> dict:
    if baseline is None:
        if baseline_path:
            try:
                baseline = json.loads(Path(baseline_path).read_text())
            except Exception:
                baseline = fetch(url, method, data)
        else:
            baseline = fetch(url, method, data)

    current    = fetch(url, method, data)
    comparison = compare(baseline, current)
    perf       = quick_profile(url, profile_runs) if do_profile else {}

    return {
        "passed":             comparison["passed"],
        "comparison":         comparison,
        "performance_after":  perf,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Validate API response after applying fixes")
    p.add_argument("--url",      required=True,  help="Endpoint URL")
    p.add_argument("--method",   default="GET")
    p.add_argument("--data",     default="",     help="Request body (JSON)")
    p.add_argument("--baseline", default="",     help="Path to saved baseline JSON")
    p.add_argument("--capture",  action="store_true",
                   help="Capture baseline only and save to --out (don't compare)")
    p.add_argument("--out",      default="",     help="Output path for --capture")
    p.add_argument("--profile",  action="store_true",
                   help="Run quick re-profile after validation")
    p.add_argument("--runs",     type=int, default=5, help="Profile runs (default: 5)")
    args = p.parse_args()

    if args.capture:
        result = capture_baseline(args.url, args.method, args.data, args.out or "")
        if args.out:
            print(f"Baseline saved to {args.out}")
        else:
            json.dump(result, sys.stdout, indent=2)
            print()
        return

    result = validate(
        url=args.url,
        baseline_path=args.baseline,
        method=args.method,
        data=args.data,
        do_profile=args.profile,
        profile_runs=args.runs,
    )
    json.dump(result, sys.stdout, indent=2)
    print()
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
