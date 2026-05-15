#!/usr/bin/env python3
"""
analyze_api.py — Stage 2: AI-powered API analysis via IndiaMart LLM gateway

Reads a Stage 1 report (check_api.py output) + optional source files,
sends them to imllm.intermesh.net, and returns a structured diagnosis:
  - Bottleneck location and root cause
  - Ranked optimization recommendations with code diffs
  - PHP→Go migration plan (when applicable)
  - Estimated latency improvement per fix

Usage:
  export IMLLM_API_KEY=<your key>
  export IMLLM_MODEL=<model name>          # or pass --model

  python3 analyze_api.py --list-models
  python3 analyze_api.py --report report.json
  python3 analyze_api.py --report report.json --source /path/to/repo
  python3 analyze_api.py --report report.json --file controllers/CatalogController.php
  python3 analyze_api.py --url https://api.example.com/v1/products --source /path/to/repo
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from textwrap import indent

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
import llm_client as llm


# ─── colour helpers ────────────────────────────────────────────────────────────

def c(text, code): return f"\033[{code}m{text}\033[0m"
def head(msg):  print(c(f"\n  ╔{'═'*48}╗", "96")); print(c(f"  ║  {msg:<46}  ║", "96")); print(c(f"  ╚{'═'*48}╝", "96"))
def sec(msg):   print(f"\n  {'─'*50}\n  {c(msg, '96')}\n  {'─'*50}")
def ok(msg):    print(c(f"  ✓  {msg}", "92"))
def warn(msg):  print(c(f"  ⚠  {msg}", "93"))
def fail(msg):  print(c(f"  ✗  {msg}", "91"))
def info(msg):  print(f"     {msg}")


# ─── source loader ─────────────────────────────────────────────────────────────

# Directories and files to always skip — vendor, generated, tests
SKIP_DIRS  = {"vendor", "node_modules", ".git", "storage", "bootstrap/cache",
              "public", "resources/js", "resources/css"}
SKIP_FILES = {"_test.go", "_mock.go", ".min.php", "autoload.php"}

MAX_SOURCE_CHARS = 40_000   # keep prompt size manageable


def _should_skip(path: str) -> bool:
    parts = Path(path).parts
    for skip in SKIP_DIRS:
        if any(p == skip or p.startswith(skip) for p in parts):
            return True
    name = Path(path).name
    return any(name.endswith(s) for s in SKIP_FILES)


def _score_file(path: str, language: str) -> int:
    """Higher score = more likely to contain handler / bottleneck logic."""
    score = 0
    p = path.lower()
    # High-value paths
    for keyword in ("controller", "handler", "service", "repository", "model",
                    "middleware", "route", "api", "endpoint"):
        if keyword in p:
            score += 3
    # Low-value paths
    for keyword in ("config", "migration", "seed", "factory", "lang", "i18n",
                    "test", "spec", "fixture", "mock"):
        if keyword in p:
            score -= 5
    return score


def load_source_files(source_path: str, language: str) -> str:
    """
    Walk source_path, pick the most relevant files, and return them as a
    single annotated string for the LLM prompt.
    """
    if not os.path.isdir(source_path):
        return ""

    ext = "*.php" if language == "php" else "*.go"
    if language == "php-to-go-migration":
        php_files = glob.glob(f"{source_path}/**/*.php", recursive=True)
        go_files  = glob.glob(f"{source_path}/**/*.go",  recursive=True)
        all_files = php_files + go_files
    else:
        all_files = glob.glob(f"{source_path}/**/{ext}", recursive=True)

    # Filter, score, sort
    candidates = [f for f in all_files if not _should_skip(f)]
    candidates.sort(key=lambda f: _score_file(f, language), reverse=True)

    chunks = []
    total_chars = 0

    for fpath in candidates:
        if total_chars >= MAX_SOURCE_CHARS:
            break
        try:
            content = open(fpath).read()
            if len(content) > 8000:
                content = content[:8000] + "\n... [truncated]"
            rel = os.path.relpath(fpath, source_path)
            chunk = f"\n### FILE: {rel}\n```\n{content}\n```\n"
            chunks.append(chunk)
            total_chars += len(chunk)
        except Exception:
            continue

    if not chunks:
        return ""

    skipped = len(candidates) - len(chunks)
    header = f"[Showing {len(chunks)} most relevant files"
    header += f", {skipped} lower-priority files skipped]\n" if skipped else "]\n"
    return header + "\n".join(chunks)


def load_single_file(fpath: str) -> str:
    try:
        content = open(fpath).read()
        if len(content) > 12000:
            content = content[:12000] + "\n... [truncated]"
        return f"### FILE: {fpath}\n```\n{content}\n```\n"
    except FileNotFoundError:
        fail(f"File not found: {fpath}")
        return ""


# ─── prompt builders ───────────────────────────────────────────────────────────

SYSTEM_PHP = """You are a senior PHP backend performance engineer with 10+ years of experience.
You specialise in diagnosing API bottlenecks in Laravel, Symfony, and raw PHP applications.
You are an expert in: N+1 query patterns, synchronous blocking I/O, missing DB indexes,
OPcache misconfiguration, PHP-FPM tuning, and identifying serial call chains that can be parallelised.
Always respond with ONLY a valid JSON object — no prose, no markdown fences."""

SYSTEM_GO = """You are a senior Go backend performance engineer with 10+ years of experience.
You specialise in diagnosing API bottlenecks in Go services using Gin, Echo, Fiber, and net/http.
You are an expert in: goroutine leaks, sync.Mutex contention across I/O, missing context propagation,
sequential HTTP/DB calls that could use errgroup, heap allocation hotspots, and string builder patterns.
Always respond with ONLY a valid JSON object — no prose, no markdown fences."""

SYSTEM_MIGRATION = """You are a senior backend engineer who specialises in PHP-to-Go API migrations.
You understand both languages deeply and know how to rewrite PHP business logic into idiomatic Go —
using errgroup for parallelism, errors.As for typed errors, context.Context threading, and clean
separation of concerns. Always respond with ONLY a valid JSON object — no prose, no markdown fences."""


OUTPUT_SCHEMA = """
Return this exact JSON schema (fill every field, do not add extra top-level keys):
{
  "summary": "<one sentence: what is the primary bottleneck and estimated gain>",
  "language": "<php|go|php-to-go-migration>",
  "bottlenecks": [
    {
      "rank": 1,
      "file": "<relative file path or 'unknown'>",
      "line_range": "<e.g. 45-67 or 'unknown'>",
      "category": "<one of: n_plus_one_query | serial_io | missing_index | mutex_contention | goroutine_leak | redundant_computation | memory_alloc | dead_code | missing_cache | sync_to_async>",
      "severity": "<critical|high|medium|low>",
      "description": "<2-3 sentences: what is happening and why it is slow>",
      "estimated_latency_saved_ms": <integer>,
      "fix": {
        "description": "<one sentence: what to change>",
        "before": "<code snippet showing the problem — keep under 20 lines>",
        "after":  "<code snippet showing the fix — keep under 20 lines>"
      }
    }
  ],
  "parallel_opportunities": [
    {
      "description": "<what calls can run in parallel>",
      "estimated_latency_saved_ms": <integer>,
      "code": "<parallel implementation snippet>"
    }
  ],
  "migration_plan": {
    "applicable": <true|false>,
    "rationale": "<why migration helps or why it doesn't apply>",
    "go_equivalent": "<Go handler code for the critical path, if applicable>",
    "estimated_latency_saved_ms": <integer>
  },
  "total_estimated_improvement_ms": <integer>,
  "total_estimated_improvement_pct": <integer>,
  "priority_order": ["<bottleneck category 1>", "<bottleneck category 2>"]
}
"""


def build_prompt(report: dict, source_code: str, language: str) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the given language."""
    system = {
        "php": SYSTEM_PHP,
        "go":  SYSTEM_GO,
        "php-to-go-migration": SYSTEM_MIGRATION,
    }.get(language, SYSTEM_GO)

    profile = report.get("latency_profile", {})
    layers  = profile.get("layers", {})
    health  = report.get("health", {})

    profile_summary = f"""
API PROFILING DATA:
  Endpoint  : {report.get('endpoint', 'unknown')}
  Method    : {report.get('method', 'GET')}
  HTTP code : {health.get('http_code', 'unknown')}
  Body size : {health.get('body_bytes', 0) / 1024:.1f} KB

  Latency percentiles (ms):
    p50  = {profile.get('p50_ms', 'N/A')}
    p95  = {profile.get('p95_ms', 'N/A')}
    p99  = {profile.get('p99_ms', 'N/A')}
    mean = {profile.get('mean_ms', 'N/A')}

  Per-layer breakdown (mean ms):
    DNS resolution : {layers.get('dns_ms', 0)}
    TCP handshake  : {layers.get('tcp_ms', 0)}
    TLS handshake  : {layers.get('tls_ms', 0)}
    App processing : {layers.get('app_ms', 0)}   ← handler + DB time
    Body transfer  : {layers.get('body_ms', 0)}
"""

    lang_det = report.get("language_detection", {})
    lang_summary = f"""
LANGUAGE DETECTION:
  Detected language : {lang_det.get('language', language)}
  Confidence        : {lang_det.get('confidence', 'unknown')}
  PHP source files  : {lang_det.get('php_files', 0)}
  Go source files   : {lang_det.get('go_files', 0)}
"""

    source_block = f"\nSOURCE CODE:\n{source_code}" if source_code else \
                   "\nSOURCE CODE: Not provided — base analysis on profiling data and common patterns."

    user = f"""Analyse this API for performance bottlenecks and generate a complete optimization report.

{profile_summary}
{lang_summary}
{source_block}

{OUTPUT_SCHEMA}
"""
    return system, user


# ─── display ───────────────────────────────────────────────────────────────────

SEVERITY_COLOR = {"critical": "91", "high": "93", "medium": "33", "low": "37"}
CATEGORY_EMOJI = {
    "n_plus_one_query":     "🔁",
    "serial_io":            "⛓",
    "missing_index":        "📋",
    "mutex_contention":     "🔒",
    "goroutine_leak":       "💧",
    "redundant_computation":"♻️",
    "memory_alloc":         "🧠",
    "dead_code":            "💀",
    "missing_cache":        "⚡",
    "sync_to_async":        "🔀",
}


def display_analysis(result: dict):
    sec("ANALYSIS SUMMARY")
    info(result.get("summary", ""))

    total_ms  = result.get("total_estimated_improvement_ms", 0)
    total_pct = result.get("total_estimated_improvement_pct", 0)
    print()
    print(c(f"  Total estimated improvement : {total_ms} ms  ({total_pct}%)", "92"))

    bottlenecks = result.get("bottlenecks", [])
    if bottlenecks:
        sec(f"BOTTLENECKS  ({len(bottlenecks)} found)")
        for b in bottlenecks:
            sev    = b.get("severity", "medium")
            sev_c  = SEVERITY_COLOR.get(sev, "37")
            cat    = b.get("category", "")
            emoji  = CATEGORY_EMOJI.get(cat, "🔍")
            saving = b.get("estimated_latency_saved_ms", 0)

            rank = b.get("rank", "?")
            print()
            print(f"  {c('#' + str(rank), '1')}  {emoji}  {c(cat.replace('_', ' ').upper(), sev_c)}"
                  f"  [{c(sev, sev_c)}]  save ~{saving}ms")
            print(f"     📁 {b.get('file', 'unknown')}:{b.get('line_range', '?')}")
            info(b.get("description", ""))

            fix = b.get("fix", {})
            if fix.get("before") and fix.get("after"):
                print(f"\n     {c('FIX:', '93')} {fix.get('description', '')}")
                print(f"\n     {c('Before:', '91')}")
                for line in fix["before"].splitlines():
                    print(f"       {line}")
                print(f"\n     {c('After:', '92')}")
                for line in fix["after"].splitlines():
                    print(f"       {line}")

    parallel = result.get("parallel_opportunities", [])
    if parallel:
        sec(f"PARALLELISM OPPORTUNITIES  ({len(parallel)} found)")
        for i, p in enumerate(parallel, 1):
            print(f"\n  #{i}  🔀  save ~{p.get('estimated_latency_saved_ms', 0)}ms")
            info(p.get("description", ""))
            if p.get("code"):
                print()
                for line in p["code"].splitlines():
                    print(f"     {line}")

    migration = result.get("migration_plan", {})
    if migration.get("applicable"):
        sec("PHP → GO MIGRATION PLAN")
        info(migration.get("rationale", ""))
        saving = migration.get("estimated_latency_saved_ms", 0)
        print(c(f"\n  Estimated gain from migration : ~{saving}ms", "92"))
        go_code = migration.get("go_equivalent", "")
        if go_code:
            print(f"\n  {c('Generated Go handler:', '96')}")
            for line in go_code.splitlines():
                print(f"     {line}")

    priority = result.get("priority_order", [])
    if priority:
        sec("FIX PRIORITY ORDER")
        for i, p in enumerate(priority, 1):
            info(f"{i}. {p.replace('_', ' ')}")


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="API Optimizer — Stage 2: AI analysis via imllm.intermesh.net"
    )
    parser.add_argument("--report",      "-r",  help="Stage 1 JSON report (from check_api.py)")
    parser.add_argument("--url",         "-u",  help="API URL (if no report; runs quick profile first)")
    parser.add_argument("--source",      "-s",  help="Source code directory")
    parser.add_argument("--file",        "-f",  help="Single source file to analyse")
    parser.add_argument("--model",       "-m",  help="LLM model name (overrides IMLLM_MODEL env var)")
    parser.add_argument("--api-key",     "-k",  help="API key (overrides IMLLM_API_KEY env var)")
    parser.add_argument("--output",      "-o",  help="Save analysis JSON to file")
    parser.add_argument("--list-models", action="store_true", help="List available models and exit")
    args = parser.parse_args()

    # Override env vars if CLI flags provided
    if args.api_key:
        os.environ["IMLLM_API_KEY"] = args.api_key
        llm.API_KEY = args.api_key
    if args.model:
        os.environ["IMLLM_MODEL"] = args.model
        llm.DEFAULT_MODEL = args.model

    # ── list models ──────────────────────────────────────────────────────────
    if args.list_models:
        head("AVAILABLE MODELS")
        try:
            models = llm.list_models()
            if not models:
                warn("No models returned. Check your API key.")
            for m in models:
                mid = m.get("id") or m.get("model") or str(m)
                owner = m.get("owned_by", "")
                print(f"  • {c(mid, '92')}  {c(owner, '37')}")
            print()
            info(f"Set your preferred model:  export IMLLM_MODEL=<model-id>")
        except Exception as e:
            fail(str(e))
            sys.exit(1)
        return

    # ── load or build report ─────────────────────────────────────────────────
    report = {}
    if args.report:
        try:
            with open(args.report) as f:
                report = json.load(f)
            ok(f"Loaded Stage 1 report: {args.report}")
        except FileNotFoundError:
            fail(f"Report file not found: {args.report}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            fail(f"Invalid JSON in report: {e}")
            sys.exit(1)
    elif args.url:
        # Minimal stub report — no profile data, just URL and language detection
        warn("No Stage 1 report provided. Running language detection from URL only.")
        warn("For full analysis, first run: python3 check_api.py --url <url> --output report.json")
        sys.path.insert(0, str(Path(__file__).parent))
        from detect_language import detect
        lang_result = detect(url=args.url, source_path=args.source)
        report = {
            "endpoint": args.url,
            "method": "GET",
            "health": {"http_code": 200},
            "language_detection": {
                "language": lang_result.language,
                "confidence": lang_result.confidence,
                "signals": lang_result.signals,
                "php_files": lang_result.php_files,
                "go_files": lang_result.go_files,
            },
            "latency_profile": {},
            "next_step": lang_result.language,
        }
    else:
        parser.error("Provide either --report <file> or --url <url>")

    # ── determine language ────────────────────────────────────────────────────
    lang_det  = report.get("language_detection", {})
    language  = lang_det.get("language", "unknown")
    next_step = report.get("next_step", "")

    if language == "unknown":
        warn("Language could not be determined. Defaulting to Go analysis.")
        language = "go"

    # ── load source code ──────────────────────────────────────────────────────
    source_code = ""
    if args.file:
        source_code = load_single_file(args.file)
        ok(f"Loaded file: {args.file}")
    elif args.source:
        source_code = load_source_files(args.source, language)
        if source_code:
            ok(f"Loaded source from: {args.source}")
        else:
            warn(f"No source files found in: {args.source}")

    # ── build prompt & call LLM ───────────────────────────────────────────────
    head("STAGE 2: AI ANALYSIS")
    info(f"Language  : {language}")
    info(f"Model     : {llm.DEFAULT_MODEL or '(from IMLLM_MODEL env var)'}")
    info(f"Endpoint  : {report.get('endpoint', 'N/A')}")
    info(f"Source    : {'yes (' + str(len(source_code)) + ' chars)' if source_code else 'none — pattern-based analysis'}")
    print()

    system_prompt, user_prompt = build_prompt(report, source_code, language)

    print("  Calling LLM...", end="", flush=True)
    try:
        result = llm.chat_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4096,
        )
        print(c("  done", "92"))
    except PermissionError as e:
        print()
        fail(str(e))
        sys.exit(1)
    except ValueError as e:
        print()
        fail(str(e))
        sys.exit(1)
    except Exception as e:
        print()
        fail(f"LLM call failed: {e}")
        sys.exit(1)

    # ── display ───────────────────────────────────────────────────────────────
    display_analysis(result)

    # ── save output ───────────────────────────────────────────────────────────
    output_path = args.output
    if not output_path and args.report:
        # Auto-save alongside the input report
        output_path = args.report.replace(".json", "_analysis.json")

    if output_path:
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)
        print()
        ok(f"Analysis saved to: {output_path}")

    print()


if __name__ == "__main__":
    main()
