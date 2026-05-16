---
name: api-optimizer
description: >
  Profiles a live API endpoint end-to-end, diagnoses performance bottlenecks using an
  agentic LLM tracer, generates ready-to-merge code fixes, and optionally opens a pull
  request. Supports PHP and Go backends. Use when a user reports a slow API, when p95
  latency exceeds a threshold, or when you need to find N+1 queries, serial I/O,
  missing context propagation, http client reuse issues, connection pool sizing,
  mutex contention over I/O, missing cache, goroutine leaks, or response serialization
  overhead in source code. Also supports PHP-to-Go migration analysis.
compatibility: >
  Requires Python 3.10+, curl. Set IMLLM_API_KEY and IMLLM_MODEL env vars before use.
  Optional: gh CLI for pull request creation.
metadata:
  author: indiamartplatform
  version: "2.0"
allowed-tools: Bash Read
---

# API Optimizer

Three-stage pipeline: **profile → diagnose → patch**.

## Quickstart

```bash
export IMLLM_API_KEY=<key>
export IMLLM_MODEL=gemini-2.5-flash

# Profile only (no source needed)
python run.py --url https://your-api/v1/products --profile-only

# Full pipeline, dry run (no file writes)
python run.py --url https://your-api/v1/products --source /path/to/repo --dry-run

# Full pipeline + open PR
python run.py --url https://your-api/v1/products --source /path/to/repo --push
```

## Stage 1 — Profile (`scripts/check_api.py`)

Hits the endpoint with `curl` across N runs (default 20). Detects language (PHP / Go) from
response headers and source file counts. Produces:

- Latency percentiles: p50 / p75 / p95 / p99
- Per-layer breakdown: DNS / TCP / TLS / App / Body
- Saved to `./results/<timestamp>_<slug>/stage1_report.json`

## Stage 2 — Diagnose (`scripts/analyze_api.py`)

Runs an agentic LLM loop that explores the source repo autonomously using five tools:
`list_go_packages` (Go module layout), `find_route` (locate handler from URL path),
`list_directory`, `read_file`, `search_symbol`. As it reads files it calls `report_fix`
inline for each bottleneck found, then calls `finish_analysis` when done.

The agent's system prompt is injected with profiling data and the optimization playbook
from `skills/optimize-api.md`. Detected categories include:
`serial_io`, `context_propagation`, `http_client_reuse`, `connection_pool_sizing`,
`mutex_contention`, `n_plus_one_query`, `missing_cache`, `goroutine_leak`,
`response_serialization`, `memory_alloc`, `sync_to_async`.

Each `report_fix` call produces:
- `file` + `line_range` — exact location
- `category` — one of the above
- `severity` — critical / high / medium / low
- `before` / `after` — max 8 lines each
- `anchor` — function signature for Stage 3 patch targeting
- `estimated_ms` — latency saved estimate

Output saved to `stage2_analysis.json`.

## Stage 3 — Patch (`scripts/generate_pr.py`)

Applies each fix using a 4-strategy cascade:
1. Exact string match
2. Whitespace-normalised match
3. Fuzzy difflib (≥ 0.72 similarity)
4. LLM-assisted placement (uses `anchor` field for cheap targeted context)

After each patch: runs `php -l` (PHP) or `go build ./...` (Go) and auto-reverts on failure.
Commits to branch `api-optimizer/<lang>-<YYYYMMDD-HHMM>`, generates PR markdown.

## Standalone stage usage

```bash
python scripts/check_api.py --url <url> --output report.json
python scripts/analyze_api.py --report report.json --source /path/to/repo
python scripts/generate_pr.py --analysis report_analysis.json --source /path/to/repo --dry-run
```

## Key constraints

- Source files sent to LLM are capped at 15,000 chars per file.
- Code diffs in each fix are capped at 8 lines before/after.
- Branch naming: `api-optimizer/<language>-<YYYYMMDD-HHMM>`. Override with `--branch`.
- Results directory: `./results/<timestamp>_<url-slug>/` — each run gets its own folder.

## Optimization patterns

See [references/optimization-patterns.md](references/optimization-patterns.md) for the
full playbook of Go and PHP patterns the agent is trained to find and fix.
