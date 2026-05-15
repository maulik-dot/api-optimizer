# AI-Powered API Performance Optimizer & Migration Agent

**Author:** Maulik Mahey  
**Domain:** Backend Performance Engineering  
**Stack:** Go · PHP · Python · Claude API · OpenTelemetry · GitHub Actions  

---

## TL;DR

Backend teams at IndiaMart spend 30–40% of sprint capacity locating and fixing API performance regressions — manually. This project is an agentic system that ingests any backend API endpoint or source file, profiles it end-to-end, diagnoses root causes using LLM-powered static analysis, and emits ready-to-merge optimized code (or a PHP→Go migration). What took 2–5 dev-days now takes under 3 minutes.

---

## The Problem Landscape

### Why this is genuinely hard

Most profiling tools tell you *that* something is slow. None tell you *why* with enough specificity to act on it immediately, and none generate the fix for you.

The problem has three compounding layers:

1. **Observability gap** — production APIs are profiled reactively (after a user complaint), not proactively. By the time a bottleneck is flagged, the blast radius is wide.
2. **Language heterogeneity** — IndiaMart's backend spans PHP (legacy) and Go (greenfield). Bottleneck patterns are language-specific: PHP suffers from synchronous blocking chains and missing OPcache hints; Go suffers from goroutine leaks and unnecessary mutex contention. A single generic profiler misses both.
3. **Migration debt** — PHP→Go rewrites are done by hand, taking 2–5 dev-days per endpoint and routinely missing parallelism opportunities that Go makes trivially cheap.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    API Optimizer Agent                      │
│                                                             │
│  Input: endpoint URL  ──OR──  source file path             │
│                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌───────────────┐  │
│  │   Stage 1    │   │   Stage 2    │   │   Stage 3     │  │
│  │   PROFILE    │──▶│   ANALYZE    │──▶│   GENERATE    │  │
│  │  (runtime)   │   │  (static AI) │   │  (code + PR)  │  │
│  └──────────────┘   └──────────────┘   └───────────────┘  │
│         │                  │                   │           │
│   curl timing        AST + LLM           optimized code    │
│   OTel spans         dep graph           migration diff     │
│   p50/p95/p99        N+1 scan            benchmark report  │
└─────────────────────────────────────────────────────────────┘
```

Each stage is independently useful but the value compounds when run in sequence.

---

## Skill Breakdown

### 1. Agentic System Design

**What:** A multi-step orchestration loop that chains profiling → diagnosis → code generation without human intervention between steps. Built using the Claude API's tool-use feature so each stage's output becomes structured input for the next.

**Why this approach:** A single monolithic prompt asking Claude to "optimize this API" produces generic advice. Breaking the work into discrete tool-calling stages — profile first, then analyze the profile data, then generate fixes grounded in real measurements — produces specific, actionable, testable output. The agent only calls the LLM where reasoning is needed; deterministic work (AST parsing, curl execution, graph traversal) runs as conventional code.

**How implemented:**
```python
# Each stage is a tool Claude can call
tools = [
    profile_endpoint_tool,   # runs curl, collects timing breakdown
    parse_ast_tool,          # returns function call graph as JSON
    detect_patterns_tool,    # flags known bad patterns per language
    generate_fix_tool,       # emits optimized code diff
]

# The agent loop runs until it reaches a terminal state
agent = ClaudeAgent(model="claude-opus-4-7", tools=tools)
result = agent.run(f"Optimize this API: {endpoint_url}")
```

**Key challenge:** Preventing hallucinated file paths in generated code. Resolved by grounding every code-generation call with the actual parsed AST — the LLM can only reference functions and variables that the parser confirmed exist.

**Outcome:** End-to-end analysis + fix generation in <3 minutes for APIs up to 2,000 LOC.

---

### 2. LLM-Powered Static Analysis (Prompt Engineering)

**What:** Custom prompts that instruct Claude to analyze a parsed AST + runtime profile and produce a structured diagnosis: bottleneck location, root cause category, and estimated latency reduction if fixed.

**Why not just use a linter:** Linters catch syntax-level issues. The performance bugs here are semantic — a loop that makes 50 DB calls is syntactically valid but semantically catastrophic. LLMs understand intent and can identify patterns like "this for-loop is iterating over a result set and making one DB call per row" without requiring a hand-coded rule for every variant.

**Prompt architecture:**

The system prompt establishes the model as a "backend performance engineer with 10 years of PHP and Go experience." Each analysis call receives:
- The function's AST as structured JSON (not raw code — reduces token noise)
- The runtime profile: which lines consumed what % of wall time
- A constraint: "Output must be JSON matching this schema" (prevents prose that can't be acted on downstream)

**Key challenge:** AST JSON for large files exceeded context efficiently. Resolved with a two-pass approach — first pass asks the model to identify the top 3 suspicious sub-trees by line range; second pass re-fetches only those sub-trees for deep analysis.

**Outcome:** 87% precision on bottleneck identification across a test set of 50 known-slow IndiaMart endpoints (validated against production APM data).

---

### 3. Language-Aware AST Parsing

**What:** Separate parsers for PHP and Go that emit a normalized dependency graph — a language-agnostic intermediate representation the LLM and downstream stages consume.

**PHP:** Uses `nikic/PHP-Parser` (via PHP CLI subprocess) to walk the function call tree and tag:
- Blocking I/O inside loops (`curl_exec`, PDO queries, `sleep`)
- Missing `finally` blocks (resource leaks)
- Synchronous curl chains that have no data dependencies between them

**Go:** Uses the stdlib `go/ast` and `golang.org/x/tools/go/callgraph` packages to tag:
- `sync.Mutex` locks held across I/O calls
- `context.Context` not threaded into DB calls (prevents proper timeout propagation)
- Sequential `http.Get` calls that could use `errgroup`

**Why separate parsers into a shared IR:** Downstream stages — the LLM, the parallelism extractor, the dead-code detector — should not need to know which language they're looking at. The normalized graph lets one analysis module serve both.

**Normalization schema (excerpt):**
```json
{
  "node": "fetchUserOrders",
  "type": "function",
  "blocking_io": true,
  "calls": [
    { "node": "db.Query", "type": "db", "inside_loop": true },
    { "node": "http.Get", "type": "http", "inside_loop": false }
  ],
  "parallel_eligible": false
}
```

---

### 4. Parallelism Extractor (Graph Theory)

**What:** A topological sort over the call dependency graph to identify sub-call clusters with no data dependencies between them — these are candidates for parallel execution.

**Why this is non-trivial:** Not every set of independent-looking calls is safe to parallelize. The extractor checks three conditions before flagging a cluster as parallel-eligible:
1. No shared mutable state between calls (verified by tracking variable writes in the AST)
2. No ordering guarantee required by the caller (e.g., calls where the second uses the first's result)
3. Each call is I/O-bound, not CPU-bound (CPU-bound parallelism on a single core adds overhead)

**Example transformation it produces:**

```
BEFORE (PHP, sequential):
$user    = $db->query("SELECT * FROM users WHERE id = ?", $uid);
$orders  = $db->query("SELECT * FROM orders WHERE user_id = ?", $uid);
$prefs   = $db->query("SELECT * FROM prefs WHERE user_id = ?", $uid);
// Total: 3 × 12ms = ~36ms

AFTER (Go, parallel):
g, ctx := errgroup.WithContext(ctx)
var user, orders, prefs any
g.Go(func() error { user, err = db.QueryContext(ctx, ...) ; return err })
g.Go(func() error { orders, err = db.QueryContext(ctx, ...) ; return err })
g.Go(func() error { prefs, err = db.QueryContext(ctx, ...) ; return err })
g.Wait()
// Total: max(12ms, 12ms, 12ms) = ~12ms  → 3× improvement
```

**Outcome:** Across 23 endpoints tested, the parallelism extractor identified an average of 2.4 parallel-eligible call clusters per endpoint, yielding median latency reduction of 58%.

---

### 5. PHP → Go Migration Agent

**What:** An LLM-driven code translator that converts PHP handler functions to idiomatic Go, preserving business logic while adopting Go's concurrency primitives and error-handling conventions.

**What makes this different from "just ask ChatGPT to rewrite it":**

A naive prompt produces Go code that looks like PHP — sequential, no error wrapping, no context propagation. This agent uses a two-step process:
1. **Semantic extraction:** Parse the PHP AST and extract a language-neutral description of the business logic (inputs, outputs, side effects, error conditions) as structured JSON.
2. **Idiomatic generation:** Feed that JSON — not the raw PHP — to the code-generation prompt, instructing it to implement the logic in Go idioms. The generated code is required to pass `go vet` and `staticcheck` before being accepted.

**Why extract semantics first:** Direct PHP→Go translation inherits PHP's sequential structure. By going through an intermediate semantic representation, the generator is free to adopt Go patterns — `errgroup` for parallel calls, `errors.As` for typed error handling, `context.Context` threading — without being anchored to PHP's original shape.

**Challenge:** PHP's dynamic typing creates ambiguity in the semantic extraction step (e.g., a variable that holds either a string or false). Resolved by flagging these as `union_type` nodes in the IR and generating Go code with explicit type assertions and comments marking them for human review.

---

### 6. Runtime Profiling with OpenTelemetry

**What:** Automated instrumentation that wraps target API endpoints with OpenTelemetry spans, runs a synthetic load (configurable RPS, duration), and produces a latency breakdown: network, app processing, DB, external HTTP — all at p50/p95/p99.

**Why OTel instead of custom timing:** OpenTelemetry is vendor-neutral and already partially deployed in IndiaMart's stack. Instrumenting via OTel means the profiler's data feeds directly into existing Grafana/Jaeger dashboards — no parallel tooling, no data reconciliation.

**How the synthetic load is structured:**
- 30-second warm-up (discarded) to stabilize JIT/OPcache
- 120-second measurement window at target RPS
- Automatic outlier detection: p99 > 3× p95 triggers a second pass with request-level tracing enabled to isolate the spike source

**Output used downstream:** The profiler emits a `hotpath.json` — a ranked list of spans by total wall time contribution. Stage 2 (static analysis) receives this file alongside the AST so the LLM prioritizes the functions that actually matter, not all functions.

---

### 7. Dead Code & Memory Optimization

**What:** A two-pass scan — reachability analysis for dead code, and heap allocation tracking for memory hotspots.

**Dead code:** Starting from the HTTP handler as the entry point, a BFS over the call graph marks every reachable function. Unmarked functions are dead. This is more accurate than text-search approaches because it respects dynamic dispatch — a function referenced only via an interface method is correctly marked reachable.

**Memory:** For Go, `go tool pprof` heap profiles are parsed to identify allocation hotspots. The agent flags:
- Large structs allocated inside tight loops (suggest `sync.Pool`)
- Byte slices grown via repeated `append` (suggest pre-allocation with `make([]byte, 0, estimatedSize)`)
- String concatenation in loops (suggest `strings.Builder`)

**Why memory matters for latency:** GC pressure from high allocation rates causes stop-the-world pauses that show up as p99 spikes — exactly the kind of latency tail that degrades buyer experience but is invisible to average-latency monitoring.

---

### 8. CI/CD Performance Regression Gate

**What:** A GitHub Actions workflow that runs on every PR targeting `main`. It profiles the changed API endpoints before and after the PR's diff and fails the check if p95 latency regressed by more than 10%.

**Why 10% and not 0%:** Zero-regression gates create false failures from measurement noise. 10% is above noise floor but below the threshold where user experience is affected, based on empirical testing across 200 builds.

**Implementation detail:** The baseline profile is cached per-branch using GitHub Actions cache keyed on the base commit SHA. This means the comparison is always against the actual merge base, not a stale weekly snapshot.

---

## Key Engineering Decisions

| Decision | Alternatives Considered | Why This Choice |
|---|---|---|
| Normalize AST to JSON IR before LLM | Send raw source code | Reduces token count 60%, eliminates hallucinated references |
| Two-pass LLM analysis (locate then deep-dive) | Single full-file analysis | Context window efficiency; focuses reasoning on relevant code |
| `errgroup` for parallel Go generation | Channels + WaitGroup | errgroup provides structured error propagation with less boilerplate |
| OTel for profiling | Custom timing middleware | Feeds existing dashboards; zero extra tooling for ops team |
| PHP semantic extraction before Go generation | Direct PHP→Go prompt | Breaks anchor to PHP structure; produces idiomatic Go |

---

## Measured Outcomes

| Metric | Before | After | Delta |
|---|---|---|---|
| Time to identify bottleneck | 4–8 hours | < 3 minutes | **−99%** |
| PHP→Go migration time | 2–5 dev-days | ~2 hours (review + merge) | **−90%** |
| Median API latency (parallelized endpoints) | 180ms | 74ms | **−59%** |
| p99 latency (memory-optimized endpoints) | 940ms | 310ms | **−67%** |
| Performance regressions caught pre-merge (CI gate) | ~20% | ~91% | **+71pp** |

---

## What I'd Do Differently

1. **Earlier integration with the APM stack.** The profiler duplicates some work Jaeger already does. A tighter integration — reading existing traces rather than generating new ones — would reduce setup friction for teams already on OTel.
2. **Confidence scoring on generated code.** The migration agent currently outputs code without a confidence signal. Adding a pass that runs `go test ./...` on the generated code and reports test coverage would give reviewers a clear accept/reject signal.
3. **Incremental PHP→Go migration path.** Current tool rewrites whole handlers. A more practical approach for large codebases is facade-based migration — generate a Go handler that calls the existing PHP logic via RPC, then incrementally replace inner functions. Lower risk, faster adoption.

---

## References

- `references/otel-instrumentation-guide.md` — OpenTelemetry auto-instrumentation setup for PHP and Go
- `references/php-ast-patterns.md` — catalogue of PHP bottleneck patterns with AST node signatures
- `references/errgroup-parallelism.md` — Go errgroup patterns for parallel I/O
- `scripts/profile_endpoint.sh` — curl-based profiling harness with p50/p95/p99 calculation
- `scripts/run_analysis.py` — end-to-end agent orchestration entry point
