#!/usr/bin/env python3
"""
Detect and profile service dependencies from PHP/Go source files.

Scans controllers, services, and repositories for:
  - Database calls (Eloquent, raw DB, GORM)
  - Cache operations (Redis, Cache::remember)
  - Outbound HTTP calls (Guzzle, Http::, http.Get)
  - Queue / event dispatches
  - Storage / filesystem reads
  - gRPC client calls (Go)

For each dependency type found, estimates its latency contribution
based on call-site hit count and patterns. If --profile-http is set,
reachable URLs in source are profiled with curl.

Outputs JSON:
  {
    "dependencies": [
      { "type": "database", "display": "Database", "hits": 12,
        "estimated_ms": 280, "files": [...], "urls": [],
        "profiled": false, "profiled_stats": null }
    ],
    "sum_estimated_ms": 310,
    "target_total_ms": 392,
    "app_residual_ms": 82,
    "critical_path": ["database", "app", "cache"]
  }

Usage:
  python scripts/detect_dependencies.py \\
      --source /path/to/repo --language php --total-ms 392
"""

import argparse
import json
import os
import re
import subprocess
import statistics
import sys
from pathlib import Path
from typing import Optional

# ── Pattern library ───────────────────────────────────────────────────────────

PHP_PATTERNS = {
    "database": [
        r'DB\s*::\s*(?:select|table|raw|statement|insert|update|delete)',
        r'->\s*(?:whereIn|whereHas|whereRaw|whereNull|orWhere|whereBetween)\s*\(',
        r'->\s*(?:get|first|find|paginate|all|count|sum|max|min|avg)\s*\(',
        r'->(?:create|update|delete|save|attach|detach|sync)\s*\(',
    ],
    "cache": [
        r'Cache\s*::\s*(?:remember|get|put|has|forget|tags|flush)',
        r'Redis\s*::\s*(?:get|set|hget|hset|del|exists|expire|lpush|rpush)',
        r'cache\s*\(\s*\)\s*->(?:remember|get|put|store)',
    ],
    "http": [
        r'Http\s*::\s*(?:get|post|put|delete|patch)\s*\([\'"]([^\'"]+)[\'"]',
        r'(?:new\s+)?(?:\\\\?GuzzleHttp\\\\)?Client[^;]{0,60}->(?:get|post|request)\s*\([\'"]([^\'"]+)[\'"]',
        r'file_get_contents\s*\([\'"](?:https?://[^\'"]+)[\'"]',
        r'curl_setopt[^;]{0,80}?CURLOPT_URL[^;]{0,80}?[\'"]([^\'"]+)[\'"]',
    ],
    "queue": [
        r'Queue\s*::\s*(?:push|later|bulk|connection)',
        r'(?:dispatch|Bus\s*::\s*dispatch)\s*\(new\s+\w+',
        r'->dispatch\s*\(',
        r'->onQueue\s*\(',
    ],
    "event": [
        r'Event\s*::\s*(?:dispatch|fire)',
        r'event\s*\(new\s+\w+',
        r'->fireEvent\s*\(',
    ],
    "mail": [
        r'Mail\s*::\s*(?:send|queue|to)',
        r'Notification\s*::\s*send',
    ],
    "storage": [
        r'Storage\s*::\s*(?:get|put|disk|exists|delete|url|path)',
        r'config\s*\([\'"][^\'"]+[\'"]',
        r'core\s*\(\s*\)\s*->getConfigData\s*\(',
    ],
}

GO_PATTERNS = {
    "database": [
        r'\.Query(?:Context)?\s*\(',
        r'\.Exec(?:Context)?\s*\(',
        r'\.QueryRow(?:Context)?\s*\(',
        r'db\.(?:Find|First|Where|Create|Save|Delete)\s*\(',
        r'\.Begin\s*\(',
    ],
    "cache": [
        r'rdb\.(?:Get|Set|HGet|HSet|Del|Expire|TTL)\s*\(',
        r'mc\.(?:Get|Set|Delete|Add)\s*\(',
        r'cache\.(?:Get|Set|Delete|Exists)\s*\(',
    ],
    "http": [
        r'http\.Get\s*\([\'"]([^\'"]+)[\'"]',
        r'http\.Post\s*\([\'"]([^\'"]+)[\'"]',
        r'http\.NewRequest\s*\([^,]+,\s*[\'"]([^\'"]+)[\'"]',
        r'client\.(?:Get|Post|Do)\s*\(',
    ],
    "grpc": [
        r'grpc\.Dial\s*\(',
        r'pb\.New[A-Z]\w+Client\s*\(',
        r'\.NewClient\s*\(',
    ],
    "queue": [
        r'(?:amqp|kafka|nats|stan)\.',
        r'(?:Publish|Subscribe|Produce|Consume)\s*\(',
        r'channel\.Publish\s*\(',
    ],
}

LATENCY_ESTIMATES = {
    "database": {"base": 28, "per_hit": 14},
    "cache":    {"base": 3,  "per_hit": 1},
    "http":     {"base": 60, "per_hit": 40},
    "queue":    {"base": 5,  "per_hit": 2},
    "event":    {"base": 3,  "per_hit": 1},
    "mail":     {"base": 15, "per_hit": 8},
    "storage":  {"base": 8,  "per_hit": 3},
    "grpc":     {"base": 12, "per_hit": 6},
}

TYPE_DISPLAY = {
    "database": "Database",
    "cache":    "Cache",
    "http":     "HTTP",
    "queue":    "Queue",
    "event":    "Events",
    "mail":     "Mail",
    "storage":  "Storage",
    "grpc":     "gRPC",
    "app":      "App Logic",
}

SKIP_DIRS = {"vendor", "node_modules", ".git", "tests", "test",
             "migrations", "lang", "storage", "bootstrap", "public", "dist"}


# ── File scoring ──────────────────────────────────────────────────────────────

def _score_file(path: Path) -> int:
    parts_lower = [p.lower() for p in path.parts]
    if any(p in SKIP_DIRS for p in parts_lower):
        return -1
    name = path.name.lower()
    if any(x in name for x in ["controller", "handler"]):    return 100
    if any(x in name for x in ["service", "repository"]):   return  80
    if any(x in name for x in ["resource", "route"]):       return  60
    if any(x in name for x in ["model"]):                   return  50
    return 10


# ── Source scanning ───────────────────────────────────────────────────────────

def scan_dependencies(source_path: str, language: str) -> list[dict]:
    root   = Path(source_path)
    ext    = ".php" if language in ("php", "php-to-go-migration") else ".go"
    pats   = PHP_PATTERNS if language != "go" else GO_PATTERNS

    files  = [(p, _score_file(p)) for p in root.rglob(f"*{ext}")]
    files  = [(p, s) for p, s in files if s >= 0]
    files.sort(key=lambda x: -x[1])

    by_type: dict[str, dict] = {}

    for path, _ in files[:40]:
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        lines = text.splitlines()

        for dep_type, pat_list in pats.items():
            for pattern in pat_list:
                for i, line in enumerate(lines, 1):
                    m = re.search(pattern, line)
                    if not m:
                        continue
                    if dep_type not in by_type:
                        by_type[dep_type] = {
                            "type":    dep_type,
                            "display": TYPE_DISPLAY.get(dep_type, dep_type.title()),
                            "hits":    0,
                            "urls":    [],
                            "files":   [],
                        }
                    by_type[dep_type]["hits"] += 1
                    ref = f"{path.relative_to(root)}:{i}"
                    if ref not in by_type[dep_type]["files"]:
                        by_type[dep_type]["files"].append(ref)
                    if m.lastindex and m.group(1):
                        url = m.group(1)
                        if url.startswith(("http://", "https://")) \
                                and url not in by_type[dep_type]["urls"]:
                            by_type[dep_type]["urls"].append(url)

    return list(by_type.values())


# ── HTTP profiling ────────────────────────────────────────────────────────────

def _curl_once(url: str, timeout: int = 8) -> Optional[float]:
    try:
        out = subprocess.check_output(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{time_total}",
             "--max-time", str(timeout), "--connect-timeout", "4", url],
            stderr=subprocess.DEVNULL, timeout=timeout + 2,
        )
        return float(out.strip()) * 1000
    except Exception:
        return None


def profile_http_url(url: str, runs: int = 5) -> Optional[dict]:
    samples = [ms for _ in range(runs) if (ms := _curl_once(url)) is not None]
    if not samples:
        return None
    samples.sort()
    return {
        "url":  url,
        "p50":  round(statistics.median(samples), 1),
        "p95":  round(samples[min(len(samples)-1, int(len(samples)*0.95))], 1),
        "mean": round(statistics.mean(samples), 1),
    }


# ── Latency estimation ────────────────────────────────────────────────────────

def estimate_latency(dep: dict) -> int:
    est  = LATENCY_ESTIMATES.get(dep["type"], {"base": 5, "per_hit": 2})
    hits = max(1, dep.get("hits", 1))
    return est["base"] + (hits - 1) * est["per_hit"]


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_dependencies(
    source_path: str,
    language: str,
    total_ms: float = 0,
    profile_http: bool = False,
) -> dict:
    """
    Scan source, estimate/profile each dependency, compute residual.
    Returns dict suitable for dashboard dep-graph or LLM context.
    """
    deps_raw = scan_dependencies(source_path, language)

    deps: list[dict] = []
    sum_est = 0

    for dep in deps_raw:
        ms = estimate_latency(dep)
        profiled_stats = None

        if profile_http and dep.get("urls"):
            for url in dep["urls"][:2]:
                stats = profile_http_url(url)
                if stats:
                    ms = int(stats["p50"])
                    profiled_stats = stats
                    break

        sum_est += ms
        deps.append({
            "type":           dep["type"],
            "display":        dep["display"],
            "hits":           dep["hits"],
            "files":          dep["files"][:3],
            "urls":           dep["urls"][:3],
            "estimated_ms":   ms,
            "profiled":       profiled_stats is not None,
            "profiled_stats": profiled_stats,
        })

    deps.sort(key=lambda x: -x["estimated_ms"])

    app_residual = max(0, int(total_ms) - sum_est) if total_ms > 0 else None

    return {
        "dependencies":     deps,
        "sum_estimated_ms": sum_est,
        "target_total_ms":  total_ms,
        "app_residual_ms":  app_residual,
        "critical_path":    [d["type"] for d in deps[:3]],
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Detect and profile API service dependencies from source")
    p.add_argument("--source",       required=True, help="Path to source repo")
    p.add_argument("--language",     default="php",
                   choices=["php", "go", "php-to-go-migration"])
    p.add_argument("--total-ms",     type=float, default=0,
                   help="Endpoint p95 in ms (used to compute app residual)")
    p.add_argument("--profile-http", action="store_true",
                   help="Profile reachable HTTP URLs found in source via curl")
    args = p.parse_args()

    result = analyze_dependencies(
        source_path=args.source,
        language=args.language,
        total_ms=args.total_ms,
        profile_http=args.profile_http,
    )
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
