---
name: api-optimizer
description: >
  Profiles a live API endpoint end-to-end, diagnoses performance bottlenecks using an
  agentic LLM tracer, applies ready-to-merge code fixes, validates the response contract
  post-fix, and optionally opens a pull request. Supports PHP and Go backends.
  Use when a user reports a slow API, when p95 latency exceeds a threshold, or when you
  need to find and fix N+1 queries, serial I/O, missing context propagation, HTTP client
  reuse issues, connection pool sizing, mutex contention over I/O, missing cache,
  goroutine leaks, response serialization overhead, or redundant computation in source code.
  Includes self-critique loop, re-profile retry, dependency graph detection, and pre/post
  response validation. Also supports PHP-to-Go migration analysis.
compatibility: >
  Requires Python 3.10+, curl. Set IMLLM_API_KEY and IMLLM_MODEL env vars before use.
  Optional: gh CLI or glab CLI for pull request creation, php CLI for PHP syntax checks,
  go for Go vet checks.
metadata:
  author: indiamartplatform
  version: "3.0"
allowed-tools: Bash Read
---

# API Optimizer Agent

**One command. Profile → Diagnose → Fix → Validate → PR.**

The agent replaces the manual loop of running profilers, reading through hundreds of source
files, writing fixes, and opening PRs. Tested on Bagisto v2.4.4 (PHP/Laravel on Docker):
`/api/products` went from 392ms p95 → 192ms p95 (−51%) with a single eager-load fix,
validated against baseline, committed to branch, PR generated.

---

## Quickstart

```bash
export IMLLM_API_KEY=<key>
export IMLLM_MODEL=gemini-2.5-flash   # or claude-sonnet-4-6, gpt-4o, etc.

# Profile only — no LLM, no file writes
python run.py --url http://localhost/api/products --profile-only

# Full pipeline, dry run (shows what would change, no file writes)
python run.py --url http://localhost/api/products --source /path/to/repo --dry-run

# Full pipeline + open PR
python run.py --url http://localhost/api/products --source /path/to/repo --push

# List available models on imllm.intermesh.net
python run.py --list-models
```

---

## Architecture

```
Stage 1: Profile + Detect
  curl ×20 runs → p50/p95/p99 + per-layer breakdown (DNS/TCP/TLS/App/Body)
  Language detection (PHP / Go / migration) from headers + source file counts
  Capture baseline API response for post-fix validation

Stage 1.5: Dependency Analysis
  Scan source for DB / Cache / HTTP / Queue / Storage call-sites
  Estimate latency per dependency type, compute app residual
  Output: dependency waterfall + SVG flow graph

Stage 2: Agentic Diagnosis  ← core innovation
  Tool-calling LLM loop explores source autonomously
  Self-critique second pass validates estimates against profiling ground truth
  Output: ranked bottlenecks with before/after diffs, estimated savings

Stage 3: Patch + Validate + PR
  4-strategy patch cascade (exact → normalised → fuzzy → LLM-assisted)
  Syntax check after each patch; auto-revert on failure
  Re-profile (5 runs) and compare actual vs predicted improvement
  Retry Stage 2 up to 2× if improvement < 15% (with retry context injected)
  Validate response schema/records/status matches pre-fix baseline
  Commit to branch, generate PR description markdown
```

---

## Stage 1 — Profile & Detect (`scripts/check_api.py`)

Drives `curl --write-out` timing variables across N runs (default 20) to capture:

- **Latency percentiles:** p50 / p75 / p95 / p99, mean
- **Per-layer breakdown:** DNS / TCP / TLS / App processing / Body transfer
- **App layer (`app_ms`)** — the handler + DB time; the only layer fixable with code changes.
  This value becomes the **hard cap** on how much any fix can realistically save.
- **Baseline response capture** — HTTP status, JSON body, payload size saved as pre-fix
  contract for post-fix validation in Stage 3.

Language detection combines response header fingerprinting (`X-Powered-By`, `X-Laravel-*`,
`X-Go-Version`) with source file counts (PHP vs Go files in the source directory).

Output: `./results/<timestamp>_<slug>/stage1_report.json`

---

## Stage 1.5 — Dependency Analysis (`scripts/detect_dependencies.py`)

Statically scans the 40 highest-scored source files (controllers > services > repositories
> models; skips vendor/tests/migrations) for dependency call-sites using regex patterns:

| Type | Patterns detected |
|------|------------------|
| Database | `DB::select`, `->get()`, `->paginate()`, `->first()`, GORM calls |
| Cache | `Cache::remember`, `Redis::get`, `cache()->remember` |
| HTTP | `Http::get`, `GuzzleHttp\Client->get`, `file_get_contents(https://...)` |
| Queue | `Queue::push`, `dispatch(new Job)`, channel.Publish |
| Storage | `Storage::get`, `core()->getConfigData()` |
| gRPC | `grpc.Dial`, `pb.New*Client` |

Each type has a latency model (`database = 28ms base + 14ms/hit`, `http = 60ms base + 40ms/hit`).
`app_residual = total_ms − sum(dep_ms)` isolates pure handler logic time.

Output: `./results/<timestamp>_<slug>/dependencies.json` + dashboard SVG flow graph.

---

## Stage 2 — Agentic Diagnosis (`scripts/analyze_api.py`)

### Why agentic, not a single prompt?

Dumping 20k chars of source into one prompt misses the hot path, wastes context on
irrelevant files, and gives the model no way to ask follow-up questions. Instead the LLM
is given five tools and told to *find the bottleneck* autonomously:

| Tool | Purpose |
|------|---------|
| `list_directory` | Browse repo structure; auto-skips vendor/tests/migrations |
| `read_file` | Read a specific file on demand (15k char cap) |
| `search_symbol` | Grep for class names, function calls, or any pattern |
| `list_go_packages` | Map full Go module layout before diving in |
| `find_route` | Grep router files to jump directly to the handler for a URL path |

**Agent strategy (PHP):** `find_route` → read handler → trace repository calls →
`search_symbol` for DB/Redis/HTTP patterns → `report_fix` inline per bottleneck found.

**Agent strategy (Go):** `list_go_packages` → `find_route` → read handler → trace
service layer → check `errgroup` usage for serial I/O → `report_fix`.

Each `report_fix` call is structured output:
```json
{
  "file": "packages/Webkul/Shop/src/Http/Controllers/API/ProductController.php",
  "line_range": "40-49",
  "category": "n_plus_one_query",
  "severity": "critical",
  "before": "...",
  "after": "...",
  "anchor": "public function index(Request $request)",
  "estimated_latency_saved_ms": 150
}
```

The `anchor` field (function context around the fix site) enables cheap LLM-assisted
patching in Stage 3 — ~10 lines of context instead of the full file.

### Context window management

Long agentic runs accumulate tool-call history that bloats the context. Every 8 iterations
`prune_tool_results()` trims stale tool exchanges, keeping the last 8 pairs + system
prompt + first user message. Max 35 iterations before forced termination.

### Self-critique loop

After the initial analysis a **second LLM pass** runs as a multi-turn conversation:
```
system → user (original prompt) → assistant (initial JSON) → user (critique prompt)
```

The critique checks five hard constraints at temperature=0.1 (conservative corrections):
1. **Savings cap** — total estimated savings must be ≤ `app_ms` (profiling ground truth)
2. **File path plausibility** — paths must exist in the source provided
3. **Code accuracy** — `before` snippets must match actual source
4. **Missed patterns** — uncached config reads, missing eager loads, serial external calls
5. **Estimate realism** — proportional to severity and call-site hit count

### Detected bottleneck categories

`n_plus_one_query`, `serial_io`, `missing_index`, `mutex_contention`, `goroutine_leak`,
`redundant_computation`, `memory_alloc`, `dead_code`, `missing_cache`, `sync_to_async`,
`context_propagation`, `http_client_reuse`, `connection_pool_sizing`, `response_serialization`

### Retry context injection

When Stage 3 re-profiles and finds improvement < 15%, Stage 2 is retried with context:
```
RETRY CONTEXT (attempt 2/3):
Previous fix re-profiled: p95 392ms → 362ms (7.7% gain, target ≥ 15%).
Do NOT suggest these again:
  - ProductController.php: n_plus_one_query
Find different or deeper bottlenecks — treat the above as already applied.
```
This is injected via the `playbook` parameter to the agentic path so the agent sees it
in its system prompt and hunts for *different* bottlenecks.

Output: `./results/<timestamp>_<slug>/stage2_analysis_a0.json`

---

## Stage 3 — Patch, Validate, PR (`scripts/generate_pr.py`)

### 4-strategy patch cascade

1. **Exact string match** — `str.replace` on the `before` snippet
2. **Whitespace-normalised** — strip/normalise whitespace before comparing
3. **Fuzzy difflib** — `SequenceMatcher` at ≥ 0.72 similarity threshold
4. **LLM-assisted placement** — sends `anchor` + before/after for targeted placement
   (~$0.001 per fix vs ~$0.01 for full-file context)

After each successful patch: `php -l` (PHP) or `go vet ./...` (Go on the package directory,
not single file) validates syntax. File is automatically reverted via `git checkout` on
syntax error.

### Re-profile retry loop

After Stage 3 applies fixes:
1. Re-profile the live endpoint (5 curl runs) to measure actual p95 improvement
2. Compare actual improvement vs model's predicted improvement
3. If improvement < 15% and model predicted ≥ 15% and retries remain → inject retry
   context into Stage 2 and run the full loop again (max 2 retries)
4. Sliding baseline: each retry uses the new p95 as the starting point

### Response validation

Pre/post fix comparison via `scripts/validate_api.py`:
- HTTP status code match
- JSON schema match (top-level key → type dict)
- Record count match (list length or `.data` for paginated responses)
- Payload size (bytes)

Validation is a gate before PR creation — the dashboard blocks "Create PR" until
validation passes or the engineer explicitly skips it.

### PR generation

Commits to `api-optimizer/<lang>-<YYYYMMDD-HHMM>`, generates markdown PR description with:
impact table, per-bottleneck summary, test checklist.
Optionally pushes and opens PR via `gh pr create` or `glab mr create`.

---

## Standalone stage usage

```bash
# Run stages individually
python scripts/check_api.py --url <url> --output report.json
python scripts/analyze_api.py --report report.json --source /path/to/repo
python scripts/generate_pr.py --analysis report_analysis.json --source /path/to/repo --dry-run

# Validate post-fix response
python scripts/validate_api.py --url <url> --capture --out baseline.json
python scripts/validate_api.py --url <url> --baseline baseline.json --profile
```

---

## Key constraints & design decisions

| Constraint | Value | Reason |
|-----------|-------|--------|
| File read cap | 15,000 chars | Fits comfortably in one tool response |
| Diff size cap | 8 lines before/after | Keeps Stage 3 patch reliable |
| Context prune interval | Every 8 iterations | Prevents context window overflow |
| Max agent iterations | 35 | Bounds cost on large repos |
| Fuzzy match threshold | 0.72 | Empirically: low enough for whitespace drift, high enough to avoid wrong substitutions |
| Re-profile runs | 5 | Fast enough for retry loop; enough for stable p95 |
| Retry trigger | actual < 15% AND predicted ≥ 15% | Only retries when there's a real gap |
| Max retries | 2 | Bounds total pipeline time |

## Codebase summary

| File | Lines | Role |
|------|-------|------|
| `scripts/check_api.py` | ~350 | curl profiler, language detection, health check |
| `scripts/analyze_api.py` | ~830 | agentic tool loop, self-critique, 14 bottleneck categories |
| `scripts/generate_pr.py` | ~650 | 4-strategy patcher, syntax check, PR markdown |
| `scripts/llm_client.py` | ~180 | LiteLLM wrapper, `chat_tools`, `prune_tool_results` |
| `scripts/detect_dependencies.py` | ~320 | dependency scanner, latency estimator |
| `scripts/validate_api.py` | ~228 | pre/post response validator |
| `run.py` | ~780 | pipeline orchestrator, retry loop, re-profile check |
| `assets/dashboard.html` | ~2,450 | live simulation dashboard, SVG dep graph, improvement gauge |

## Optimization playbook

See [`skills/optimize-api.md`](skills/optimize-api.md) for the full playbook of Go and
PHP patterns the agent is trained to find and fix, with before/after code examples for
all 14 categories.
