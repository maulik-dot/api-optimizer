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
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

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


# ─── agentic file tracer ───────────────────────────────────────────────────────

SKIP_DIRS = frozenset({
    "vendor", "node_modules", ".git", "storage", "bootstrap/cache",
    "public", "resources/views", "tests", "test", "spec", "testing",
    "migrations", "seeders", "factories", "lang", "i18n",
})

MAX_FILE_READ = 15_000

SKIP_SUFFIXES = ("_test.go", "_mock.go", ".min.php", "autoload.php")


def _should_skip(path: str) -> bool:
    parts = Path(path).parts
    if any(p in SKIP_DIRS for p in parts):
        return True
    return any(Path(path).name.endswith(s) for s in SKIP_SUFFIXES)


def _url_path(url: str) -> str:
    path = urlparse(url).path.strip("/")
    for prefix in ("api/v1/", "api/v2/", "api/v3/", "api/", "v1/", "v2/", "v3/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


# ── agent tool definitions (OpenAI function-calling format) ────────────────────

TRACE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List files and subdirectories at a path relative to the source root. "
                "Automatically skips vendor, node_modules, .git, tests, migrations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from source root (e.g. '.' or 'app/Http/Controllers')",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a source file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from source root",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbol",
            "description": "Search for a class name, function name, or string pattern across all source files",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The symbol or string to search for",
                    },
                    "file_extension": {
                        "type": "string",
                        "description": "Filter by extension: 'php' or 'go' (optional)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_go_packages",
            "description": (
                "List all Go packages in the source tree by scanning for go.mod and "
                "directory names. Use this first in Go repos to understand the module layout "
                "before deciding which files to read."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_route",
            "description": (
                "Search router files (gin, echo, mux, chi, net/http HandleFunc) to locate "
                "the handler function registered for a given URL path. Returns file + line "
                "references so you know exactly which handler to read next."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url_path": {
                        "type": "string",
                        "description": "The URL path fragment to search for, e.g. 'products' or 'v1/search'",
                    }
                },
                "required": ["url_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_fix",
            "description": (
                "Report a performance bottleneck and its fix as you find it. "
                "Call this each time you identify an issue — you can call it multiple times before finishing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file":         {"type": "string", "description": "Relative file path"},
                    "line_range":   {"type": "string", "description": "e.g. '45-67'"},
                    "category":     {
                        "type": "string",
                        "enum": [
                            "n_plus_one_query", "serial_io", "missing_index",
                            "mutex_contention", "redundant_computation",
                            "missing_cache", "sync_to_async", "goroutine_leak", "memory_alloc",
                            "context_propagation", "http_client_reuse",
                            "connection_pool_sizing", "response_serialization",
                        ],
                    },
                    "severity":     {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "description":  {"type": "string", "description": "2 sentences max: what is slow and why"},
                    "before":       {"type": "string", "description": "Current slow code — max 8 lines"},
                    "after":        {"type": "string", "description": "Fixed code — max 8 lines"},
                    "estimated_ms": {"type": "integer", "description": "Estimated latency saved in ms"},
                    "anchor":       {
                        "type": "string",
                        "description": (
                            "The function signature + 3-5 surrounding lines that uniquely identify "
                            "where this fix sits in the file. Used by the patch engine."
                        ),
                    },
                },
                "required": ["file", "line_range", "category", "severity",
                             "description", "before", "after", "estimated_ms", "anchor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_analysis",
            "description": "Call this when you have finished exploring and reported all fixes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One sentence: primary bottleneck and total estimated gain",
                    },
                    "parallel_opportunities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description":                {"type": "string"},
                                "estimated_latency_saved_ms": {"type": "integer"},
                            },
                        },
                    },
                    "migration_applicable": {"type": "boolean"},
                    "migration_rationale":  {"type": "string"},
                    "total_estimated_improvement_ms":  {"type": "integer"},
                    "total_estimated_improvement_pct": {"type": "integer"},
                },
                "required": ["summary", "total_estimated_improvement_ms",
                             "total_estimated_improvement_pct"],
            },
        },
    },
]


# ── tool executors ─────────────────────────────────────────────────────────────

def _exec_list_directory(source_path: str, rel_path: str) -> str:
    abs_path = os.path.normpath(os.path.join(source_path, rel_path))
    if not abs_path.startswith(os.path.abspath(source_path)):
        return "Error: path outside source root"
    if not os.path.isdir(abs_path):
        return f"Not a directory: {rel_path}"
    entries = []
    try:
        for item in sorted(os.listdir(abs_path)):
            if item in SKIP_DIRS or item.startswith("."):
                continue
            item_abs = os.path.join(abs_path, item)
            entries.append(item + "/" if os.path.isdir(item_abs) else item)
    except Exception as e:
        return f"Error: {e}"
    return "\n".join(entries) if entries else "(empty)"


def _exec_read_file(source_path: str, rel_path: str) -> str:
    abs_path = os.path.normpath(os.path.join(source_path, rel_path))
    if not abs_path.startswith(os.path.abspath(source_path)):
        return "Error: path outside source root"
    if not os.path.isfile(abs_path):
        return f"File not found: {rel_path}"
    try:
        content = open(abs_path).read()
        if len(content) > MAX_FILE_READ:
            content = content[:MAX_FILE_READ] + f"\n... [truncated at {MAX_FILE_READ} chars]"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def _exec_list_go_packages(source_path: str) -> str:
    """Return all Go packages by scanning for directories containing .go files."""
    packages = []
    for root, dirs, files in os.walk(source_path):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIRS and not d.startswith(".")]
        if any(f.endswith(".go") for f in files):
            rel = os.path.relpath(root, source_path)
            go_files = [f for f in sorted(files) if f.endswith(".go")
                        and not f.endswith("_test.go")]
            packages.append(f"{rel}/  ({len(go_files)} .go files)")
    if not packages:
        return "No Go packages found."
    return "\n".join(packages[:60]) + ("\n... (truncated)" if len(packages) > 60 else "")


def _exec_find_route(source_path: str, url_path: str) -> str:
    """Search router registration sites for a URL path fragment."""
    fragment = url_path.strip("/").split("/")[-1]  # last path segment is most distinctive
    patterns = [
        fragment,
        f'"{url_path}"',
        f'"/{url_path}"',
    ]
    results = []
    for pat in patterns:
        cmd = [
            "grep", "-rn", "--max-count=5",
            "--include=*.go",
        ]
        for d in SKIP_DIRS:
            cmd += ["--exclude-dir", d]
        cmd += [pat, source_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.stdout.strip():
                prefix = source_path.rstrip("/") + "/"
                for line in r.stdout.strip().splitlines()[:15]:
                    results.append(line.replace(prefix, ""))
        except Exception:
            pass
    if not results:
        return f"No route registration found for: {url_path}"
    return "\n".join(dict.fromkeys(results))  # deduplicate while preserving order


def _exec_search_symbol(source_path: str, pattern: str, file_extension: str = "") -> str:
    ext_filter   = [f"*.{file_extension.lstrip('.')}"] if file_extension else ["*.php", "*.go"]
    include_args = [arg for ext in ext_filter for arg in ("--include", ext)]
    exclude_args = [arg for d in SKIP_DIRS for arg in ("--exclude-dir", d)]
    cmd = ["grep", "-rn", "--max-count=3"] + include_args + exclude_args + [pattern, source_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        output = result.stdout.strip()
        if not output:
            return f"No matches for: {pattern}"
        prefix = source_path.rstrip("/") + "/"
        lines  = [line.replace(prefix, "") for line in output.splitlines()[:20]]
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


# ── unified diagnosis agent ────────────────────────────────────────────────────

_SKILL_PATH = Path(__file__).parent.parent / "skills" / "optimize-api.md"


def _load_playbook() -> str:
    try:
        text = _SKILL_PATH.read_text()
        # Strip YAML frontmatter — only inject the markdown body
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:].lstrip()
        return text
    except Exception:
        return ""


def run_diagnosis_agent(url: str, source_path: str, report: dict,
                        max_iterations: int = 35,
                        playbook: str = "") -> dict | None:
    """
    Single agent that explores the codebase and reports bottlenecks in one pass.
    Eliminates the double-read of the old tracer + analysis call design.
    Returns an analysis dict matching OUTPUT_SCHEMA, or None on failure.
    """
    if not url or not os.path.isdir(source_path):
        return None

    if not playbook:
        playbook = _load_playbook()

    lang_det = report.get("language_detection", {})
    language = lang_det.get("language", "go")
    profile  = report.get("latency_profile", {})
    layers   = profile.get("layers", {})
    app_ms   = layers.get("app_ms", 0) or 0
    dns_ms   = layers.get("dns_ms", 0) or 0
    body_ms  = layers.get("body_ms", 0) or 0

    if app_ms >= dns_ms and app_ms >= body_ms:
        focus = f"App layer is {app_ms}ms — focus on handler, service, and DB calls."
    elif dns_ms > app_ms:
        focus = f"DNS is {dns_ms}ms — investigate external HTTP calls and their caching."
    else:
        focus = f"Body transfer is {body_ms}ms — look at response serialization and payload size."

    playbook_section = ""
    if playbook:
        playbook_section = f"\n\nOPTIMIZATION REASONING FRAMEWORK — apply these questions as you read each file:\n{playbook}"

    go_hints = ""
    if language == "go":
        go_hints = """
STRATEGY FOR GO REPOS (follow in order):
1. Call list_go_packages to see the full package layout.
2. Call find_route with the URL path segment to locate the handler file.
3. Read the handler file first — trace calls to service and repository layers.
4. Search for DB/Redis/HTTP call sites with search_symbol.
5. Report each fix as you find it with report_fix, then call finish_analysis."""

    system_prompt = f"""You are a {language} performance engineer diagnosing a slow API.

PROFILING DATA:
  Endpoint : {url}
  p50 / p95 / p99 : {profile.get('p50_ms','?')} / {profile.get('p95_ms','?')} / {profile.get('p99_ms','?')} ms
  App layer: {app_ms}ms   DNS: {dns_ms}ms   Body: {body_ms}ms

FOCUS: {focus}{go_hints}{playbook_section}

Use your tools to explore the codebase. As you read files and spot bottlenecks, call report_fix immediately — do not wait until the end. You may call report_fix as many times as needed.

When you are done, call finish_analysis with the overall summary.

Be strategic: the profiling data tells you where to look. You have a limited number of tool calls so prioritise files in the hot path."""

    initial_listing = _exec_list_directory(source_path, ".")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Diagnose: {url}\n\nSource root:\n{initial_listing}"},
    ]

    bottlenecks: list[dict] = []
    final: dict | None = None

    sec("DIAGNOSIS AGENT")
    try:
        for _iter in range(max_iterations):
            # Prune old tool results every 8 iterations to stay within context window
            if _iter > 0 and _iter % 8 == 0:
                messages = llm.prune_tool_results(messages, keep_last=8)
                info(f"  [context pruned at iteration {_iter}]")

            msg = llm.chat_tools(messages, TRACE_TOOLS, temperature=0.1, max_tokens=2048)
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                warn("Agent responded without a tool call — stopping")
                break

            done = False
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except Exception:
                    fn_args = {}
                tc_id = tc["id"]

                if fn_name == "finish_analysis":
                    final      = fn_args
                    result_str = "Analysis complete."
                    done       = True

                elif fn_name == "report_fix":
                    rank = len(bottlenecks) + 1
                    bottlenecks.append({
                        "rank":                       rank,
                        "file":                       fn_args.get("file", "unknown"),
                        "line_range":                 fn_args.get("line_range", "?"),
                        "category":                   fn_args.get("category", "serial_io"),
                        "severity":                   fn_args.get("severity", "medium"),
                        "description":                fn_args.get("description", ""),
                        "estimated_latency_saved_ms": fn_args.get("estimated_ms", 0),
                        "fix": {
                            "description": fn_args.get("description", ""),
                            "before":      fn_args.get("before", ""),
                            "after":       fn_args.get("after", ""),
                        },
                        "anchor": fn_args.get("anchor", ""),
                    })
                    ok(f"  fix #{rank}: {fn_args.get('category','?')} in "
                       f"{fn_args.get('file','?')}  (~{fn_args.get('estimated_ms',0)}ms)")
                    result_str = f"Fix #{rank} recorded."

                elif fn_name == "list_go_packages":
                    result_str = _exec_list_go_packages(source_path)
                    info(f"  list_go_packages  →  {len(result_str.splitlines())} packages")

                elif fn_name == "find_route":
                    url_path   = fn_args.get("url_path", "")
                    result_str = _exec_find_route(source_path, url_path)
                    info(f"  find_route({url_path!r})  →  {len(result_str.splitlines())} hits")

                elif fn_name == "list_directory":
                    rel        = fn_args.get("path", ".")
                    result_str = _exec_list_directory(source_path, rel)
                    info(f"  list_directory({rel!r})  →  {len(result_str.splitlines())} entries")

                elif fn_name == "read_file":
                    rel        = fn_args.get("path", "")
                    result_str = _exec_read_file(source_path, rel)
                    info(f"  read_file({rel!r})  →  {len(result_str):,} chars")

                elif fn_name == "search_symbol":
                    pat        = fn_args.get("pattern", "")
                    ext        = fn_args.get("file_extension", "")
                    result_str = _exec_search_symbol(source_path, pat, ext)
                    info(f"  search_symbol({pat!r})  →  {len(result_str.splitlines())} matches")

                else:
                    result_str = f"Unknown tool: {fn_name}"

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc_id,
                    "content":      result_str,
                })

            if done:
                break
        else:
            warn(f"Agent hit iteration limit ({max_iterations})")

    except Exception as e:
        warn(f"Diagnosis agent failed: {e}")
        return None

    if not bottlenecks and not final:
        return None

    total_ms  = (final or {}).get("total_estimated_improvement_ms",
                                  sum(b["estimated_latency_saved_ms"] for b in bottlenecks))
    total_pct = (final or {}).get("total_estimated_improvement_pct", 0)

    # Sort priority by severity
    _sev = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    priority = list(dict.fromkeys(
        b["category"] for b in sorted(bottlenecks, key=lambda b: _sev.get(b["severity"], 4))
    ))

    result = {
        "summary":   (final or {}).get("summary", f"Found {len(bottlenecks)} bottleneck(s)."),
        "language":  language,
        "bottlenecks": bottlenecks,
        "parallel_opportunities": (final or {}).get("parallel_opportunities", []),
        "migration_plan": {
            "applicable": (final or {}).get("migration_applicable", False),
            "rationale":  (final or {}).get("migration_rationale", ""),
        },
        "total_estimated_improvement_ms":  total_ms,
        "total_estimated_improvement_pct": total_pct,
        "priority_order": priority,
    }

    ok(f"Diagnosis complete: {len(bottlenecks)} fix(es), ~{total_ms}ms (~{total_pct}%) improvement")
    return result


# ── heuristic fallback (used when route tracing fails) ─────────────────────────

def _load_source_files_fallback(source_path: str, language: str) -> str:
    SCORE_HIGH = ("controller", "handler", "service", "repository", "model",
                  "middleware", "route", "api", "endpoint")
    SCORE_LOW  = ("config", "migration", "seed", "factory", "lang", "i18n",
                  "test", "spec", "fixture", "mock")

    def score(path: str) -> int:
        p = path.lower()
        return sum(3 for kw in SCORE_HIGH if kw in p) + sum(-5 for kw in SCORE_LOW if kw in p)

    ext = "*.php" if language == "php" else "*.go"
    if language == "php-to-go-migration":
        all_files = (glob.glob(f"{source_path}/**/*.php", recursive=True) +
                     glob.glob(f"{source_path}/**/*.go",  recursive=True))
    else:
        all_files = glob.glob(f"{source_path}/**/{ext}", recursive=True)

    candidates = sorted([f for f in all_files if not _should_skip(f)],
                        key=score, reverse=True)
    chunks, total = [], 0
    for fpath in candidates:
        if total >= 20_000:
            break
        try:
            content = open(fpath).read()
            if len(content) > 8_000:
                content = content[:8_000] + "\n... [truncated]"
            rel   = os.path.relpath(fpath, source_path)
            chunk = f"\n### FILE: {rel}\n```\n{content}\n```\n"
            chunks.append(chunk)
            total += len(chunk)
        except Exception:
            continue

    if not chunks:
        return ""
    skipped = len(candidates) - len(chunks)
    header  = f"[Heuristic selection: {len(chunks)} files"
    header += f", {skipped} lower-priority skipped]\n" if skipped else "]\n"
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

SYSTEM_GO = """You are a senior Go backend performance engineer with 10+ years of experience at high-scale companies.
You specialise in diagnosing API bottlenecks in Go services using Gin, Echo, Fiber, and net/http.

Your diagnostic checklist (apply while reading every file):

PARALLELISM:
- Serial DB/Redis/HTTP calls that can be parallelised with errgroup.WithContext()
- sync.WaitGroup loops that block when an early goroutine fails — should be errgroup
- Fan-out patterns where results are merged sequentially after concurrent fetches

BLOCKING / CONTENTION:
- sync.Mutex or sync.RWMutex held across I/O (DB query, HTTP call, file read)
- Global locks protecting hot-path data — candidate for sync.Map or sharded locks
- channel sends/receives that block the request goroutine

CONTEXT & TIMEOUTS:
- DB queries, HTTP calls, or Redis calls that do NOT receive context.Context
- context.Background() or context.TODO() in handlers instead of the request ctx
- Missing per-call timeouts (context.WithTimeout) on external dependencies

HTTP CLIENT REUSE:
- http.Client created inside handlers or per-request functions (GC pressure + no keep-alive pooling)
- Missing Transport configuration (MaxIdleConns, IdleConnTimeout)

CONNECTION POOLS:
- DB: sql.DB with default MaxOpenConns=0 (unlimited) or MaxIdleConns=2 (too low for high QPS)
- Redis: single-connection clients or pools sized for development not production
- gRPC: creating a new ClientConn per request instead of reusing a global one

ALLOCATIONS:
- []byte or string concatenation in a loop — use strings.Builder or bytes.Buffer
- JSON marshal/unmarshal of the same data twice (once for logging, once for response)
- Large structs copied by value in hot paths — pass pointers

SERIALISATION:
- json.Marshal on large structs with many fields — consider custom MarshalJSON or encoding/json options
- Returning entire DB rows when only a subset of fields is needed

Always respond with ONLY a valid JSON object — no prose, no markdown fences."""

SYSTEM_MIGRATION = """You are a senior backend engineer who specialises in PHP-to-Go API migrations.
You understand both languages deeply and know how to rewrite PHP business logic into idiomatic Go —
using errgroup for parallelism, errors.As for typed errors, context.Context threading, and clean
separation of concerns. Always respond with ONLY a valid JSON object — no prose, no markdown fences."""


OUTPUT_SCHEMA = """
Return ONLY this exact JSON object. Be concise — code snippets max 8 lines each, descriptions max 2 sentences:
{
  "summary": "<one sentence: primary bottleneck and estimated gain>",
  "language": "<php|go|php-to-go-migration>",
  "bottlenecks": [
    {
      "rank": 1,
      "file": "<relative file path or 'unknown'>",
      "line_range": "<e.g. 45-67>",
      "category": "<n_plus_one_query|serial_io|missing_index|mutex_contention|redundant_computation|missing_cache|sync_to_async>",
      "severity": "<critical|high|medium|low>",
      "description": "<2 sentences max: what is slow and why>",
      "estimated_latency_saved_ms": <integer>,
      "fix": {
        "description": "<one sentence: what to change>",
        "before": "<STRICT MAX 8 LINES of code showing the problem>",
        "after":  "<STRICT MAX 8 LINES of code showing the fix>"
      }
    }
  ],
  "parallel_opportunities": [
    {
      "description": "<one sentence: what can run in parallel>",
      "estimated_latency_saved_ms": <integer>
    }
  ],
  "migration_plan": {
    "applicable": <true|false>,
    "rationale": "<one sentence>"
  },
  "total_estimated_improvement_ms": <integer>,
  "total_estimated_improvement_pct": <integer>,
  "priority_order": ["<category1>", "<category2>"]
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


# ─── self-critique ─────────────────────────────────────────────────────────────

def run_critique(
    system_prompt: str,
    user_prompt: str,
    initial_analysis: dict,
    profile: dict,
) -> dict:
    """
    Second LLM call in a multi-turn conversation.
    Asks the model to review its own output against hard constraints from the
    profiling data and return a corrected (or confirmed) JSON.
    """
    app_ms  = profile.get("layers", {}).get("app_ms", 0) or 0
    p95_ms  = profile.get("p95_ms", 0) or 0
    total_savings = sum(
        b.get("estimated_latency_saved_ms", 0)
        for b in initial_analysis.get("bottlenecks", [])
    )

    critique_prompt = f"""Review the analysis you just produced against these hard constraints.

PROFILING GROUND TRUTH:
  Total p95 latency   : {p95_ms}ms
  App processing time : {app_ms}ms  ← MAXIMUM possible savings (DB + handler only)
  Your total savings  : {total_savings}ms across {len(initial_analysis.get('bottlenecks', []))} bottleneck(s)

CHECKLIST — correct any violations:
1. SAVINGS CAP: total estimated savings ({total_savings}ms) must be ≤ app_ms ({app_ms}ms).
   If exceeded, scale estimates down proportionally.
2. FILE PATHS: each `file` must be a plausible path visible in the source provided.
   If a path looks invented, set `file` to "unknown".
3. CODE ACCURACY: each `before` snippet must match code actually present in the source.
   If not verifiable, shorten or remove it.
4. MISSED PATTERNS: is there an obvious bottleneck you missed?
   (uncached config read inside a loop, missing eager load, synchronous external call)
5. ESTIMATE REALISM: are individual savings proportional to bottleneck severity and hit count?

Return the corrected JSON if changes are needed, or the original JSON if it passes all checks.
Return ONLY the JSON — no explanation, no markdown."""

    messages = [
        {"role": "system",    "content": system_prompt},
        {"role": "user",      "content": user_prompt},
        {"role": "assistant", "content": json.dumps(initial_analysis)},
        {"role": "user",      "content": critique_prompt},
    ]

    return llm.chat_json(
        messages=messages,
        temperature=0.1,
        max_tokens=8192,
        response_format={"type": "json_object"},
    )


# ─── display ───────────────────────────────────────────────────────────────────

SEVERITY_COLOR = {"critical": "91", "high": "93", "medium": "33", "low": "37"}
CATEGORY_EMOJI = {
    "n_plus_one_query":       "🔁",
    "serial_io":              "⛓",
    "missing_index":          "📋",
    "mutex_contention":       "🔒",
    "goroutine_leak":         "💧",
    "redundant_computation":  "♻️",
    "memory_alloc":           "🧠",
    "dead_code":              "💀",
    "missing_cache":          "⚡",
    "sync_to_async":          "🔀",
    "context_propagation":    "🧵",
    "http_client_reuse":      "🌐",
    "connection_pool_sizing": "🏊",
    "response_serialization": "📦",
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
    # ── build prompt & call LLM ───────────────────────────────────────────────
    head("STAGE 2: AI ANALYSIS")
    info(f"Language  : {language}")
    info(f"Model     : {llm.DEFAULT_MODEL or '(from IMLLM_MODEL env var)'}")
    info(f"Endpoint  : {report.get('endpoint', 'N/A')}")
    print()

    result = None

    if args.source:
        endpoint_url = report.get("endpoint") or args.url or ""
        result = run_diagnosis_agent(endpoint_url, args.source, report)
        if not result:
            warn("Diagnosis agent failed — falling back to pattern-based analysis")

    if not result:
        # Fallback: heuristic file selection + single analysis call
        source_code = _load_source_files_fallback(args.source, language) if args.source else ""
        info(f"Source: {'yes (' + str(len(source_code)) + ' chars)' if source_code else 'none'}")
        system_prompt, user_prompt = build_prompt(report, source_code, language)
        print("  Calling LLM...", end="", flush=True)
        try:
            result = llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=3000,
                response_format={"type": "json_object"},
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
