# API Optimizer Agent

> **One command. Profile → Diagnose → Fix → PR.**
> An AI-powered agent that finds performance bottlenecks in any HTTP API, explains them in plain language, patches the source code, and opens a pull request — without a human ever reading a profiler trace.

---

## Why We Built This

At IndiaMart, backend teams manage hundreds of API endpoints across PHP and Go services. When an endpoint slows down, the usual workflow looks like this:

1. Someone notices latency spike in monitoring
2. Engineer reads application logs, traces DB queries manually
3. Finds the N+1 query or missing cache after hours of digging
4. Writes the fix, creates a PR, gets it reviewed
5. Deploy — hope it helps

**This entire cycle takes 4–8 hours per incident**, and the bottleneck pattern is almost always the same: N+1 queries, missing eager loading, synchronous I/O that could be parallelized, or missing cache headers.

The API Optimizer Agent automates steps 1–4 entirely. An engineer runs one command and gets back a ready-to-merge pull request with the fix, the before/after code diff, and projected latency improvement. What took half a day now takes 31 seconds.

---

## What It Does

The agent runs a three-stage pipeline:

```
┌─────────────────────────────────────────────────────────────────┐
│  INPUT: API endpoint URL  +  source repo path                   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │   STAGE 1: PROFILE    │
                    │  curl × 20 runs       │
                    │  p50 / p75 / p95 / p99│
                    │  DNS/TCP/TLS/App/Body │
                    │  Language detection   │
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │  STAGE 2: AI DIAGNOSE │
                    │  LLM reads source     │
                    │  Ranks bottlenecks    │
                    │  Produces code diffs  │
                    │  Estimates savings    │
                    └───────────┬───────────┘
                                │
                    ┌───────────▼───────────┐
                    │  STAGE 3: FIX + PR    │
                    │  Patches source files │
                    │  4-strategy patching  │
                    │  Generates PR desc    │
                    │  Pushes branch        │
                    └───────────┬───────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│  OUTPUT: Patched code on a branch  +  PR description markdown   │
└─────────────────────────────────────────────────────────────────┘
```

### Stage 1 — Profile + Detect
- Runs 20 curl requests and computes **p50, p75, p95, p99, mean** latency
- Breaks each request into **DNS / TCP / TLS / App logic / Body transfer** layers to pinpoint where time is actually spent
- **Auto-detects language** from headers and source tree (PHP, Go, or PHP→Go migration candidate)
- Flags when p95 crosses warning thresholds (200ms, 500ms)

### Stage 2 — AI Analysis
- Selects the most relevant source files (controllers, repositories, services) and sends them along with the profiling report to an LLM
- The LLM produces a **structured JSON diagnosis**: ranked bottlenecks, root cause description, category (N+1 query / missing cache / sync I/O / memory leak / dead code), severity, file + line range, and a before/after code diff
- Also surfaces **parallelism opportunities** — places where sequential async calls could be fanned out
- Includes a **PHP→Go migration plan** when applicable
- Estimates **latency saved in ms and % per fix**

### Stage 3 — Apply Fixes + PR
- Applies each AI-suggested patch using a **4-strategy engine** in order of confidence:
  1. Exact string match
  2. Normalised whitespace match
  3. Fuzzy match (difflib, 0.72 threshold sliding window)
  4. LLM-assisted placement (fallback for structurally shifted code)
- Creates a git branch named `api-optimizer/<lang>-<timestamp>`
- Generates a full **PR description** in Markdown: impact table, collapsible before/after diffs per fix, test checklist
- Optionally pushes the branch and opens the PR via `gh` or `glab` CLI

---

## Significance — What Problem Does It Solve

| Problem | Without the Tool | With the Tool |
|---------|-----------------|---------------|
| Spotting a slow endpoint | Monitoring alert → triage → assign | Immediate: tool profiles and flags p95 > threshold |
| Finding root cause | 2–6 hours of log reading and query tracing | 21 seconds — AI reads the controller and repository code |
| Writing the fix | Engineer writes patch, opens PR | Patch is written and committed automatically |
| Documentation | Usually skipped | PR description with impact table, diffs, test checklist generated |
| Consistency | Depends on who is on-call | Same systematic approach every time |
| Coverage | Only critical incidents get attention | Can be run on any endpoint, any time — in CI, pre-deploy |

### Specific Scenarios It Handles

**PHP APIs (Laravel / Bagisto-style)**
- N+1 Eloquent queries from missing `->with([...])` eager loading
- Missing `Cache::remember()` on expensive aggregation queries
- Synchronous third-party API calls that block the response
- Unindexed foreign keys in hot query paths

**Go APIs**
- Sequential `http.Get()` calls that could run as `goroutine` + `WaitGroup`
- Missing `sync.Pool` for high-allocation structs
- Context propagation gaps causing cascading timeouts
- Unoptimized JSON marshalling on large payloads

**PHP → Go Migration**
- Identifies endpoints where a Go rewrite would yield >50% latency reduction
- Produces a side-by-side migration plan with Go equivalents of Laravel patterns

---

## Measured Results (Live Test — Bagisto E-commerce API)

Tested against the real Bagisto v2.4.4 products API running locally on Docker:

| Metric | Value |
|--------|-------|
| Endpoint profiled | `GET /api/products` |
| Baseline p95 | **392 ms** |
| Bottleneck found | N+1 query in `ProductController.php:40–49` |
| Fix | Eager load `images`, `categories`, `prices`, `attributes` |
| Estimated p95 after fix | **~192 ms** |
| Estimated improvement | **−150 ms (−51%)** |
| Total time end-to-end | **31 seconds** |

---

## Repository Layout

```
api-optimizer/
├── run.py                      # Single entry point — wires all three stages
├── requirements.txt            # requests>=2.31.0 (only dependency)
├── scripts/
│   ├── check_api.py            # Stage 1: health check, language detect, profiling
│   ├── detect_language.py      # PHP vs Go detection from headers + source tree
│   ├── profile_endpoint.sh     # curl-based p50/p95/p99 with layer breakdown
│   ├── analyze_api.py          # Stage 2: LLM-powered diagnosis engine
│   ├── llm_client.py           # Thin LiteLLM proxy client (no OpenAI SDK needed)
│   └── generate_pr.py          # Stage 3: patch engine + git branch + PR description
├── assets/
│   └── slides.html             # 15-slide self-contained presentation (no dependencies)
├── SKILL.md                    # Technical deep-dive writeup (hackathon Axis 5)
└── DEMO_SCRIPT.md              # 7-minute demo video four-beat script
```

---

## Quick Start

```bash
# 1. Clone and set up
git clone https://github.com/maulik-dot/api-optimizer
cd api-optimizer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Set your LLM API key
export IMLLM_API_KEY=<your-key>
export IMLLM_MODEL=gemini-2.5-flash        # or any model from --list-models

# 3. Profile only (no LLM, no file changes)
.venv/bin/python run.py --url https://your-api.com/v1/products --profile-only

# 4. Full pipeline — profile + diagnose + patch (dry run, no file changes)
.venv/bin/python run.py \
  --url https://your-api.com/v1/products \
  --source /path/to/your/repo \
  --dry-run

# 5. Full pipeline with real patch + push PR
.venv/bin/python run.py \
  --url https://your-api.com/v1/products \
  --source /path/to/your/repo \
  --push
```

### All flags

| Flag | Description |
|------|-------------|
| `--url` | API endpoint to profile (required) |
| `--source` | Path to source repo for AI analysis |
| `--profile-only` | Stop after Stage 1 (no LLM call) |
| `--no-pr` | Run Stage 2 analysis but skip patching |
| `--dry-run` | Apply patch in memory only, don't write files |
| `--push` | After patching, push branch and open PR |
| `--model` | Override model (or set `IMLLM_MODEL`) |
| `--runs` | Number of profiling runs (default: 20) |
| `--list-models` | Print all models available on the gateway |

---

## How the LLM Integration Works

The tool connects to **imllm.intermesh.net**, IndiaMart's internal LiteLLM proxy. This is a single OpenAI-compatible endpoint that routes to multiple model providers (Gemini, Claude, GPT-4, Qwen, DeepSeek, etc.) using one API key format.

The integration uses **raw `requests`** — no OpenAI SDK, no LangChain. The entire LLM client is 105 lines (`scripts/llm_client.py`).

The prompt is constructed in three variants depending on detected language:
- `SYSTEM_PHP` — focused on Eloquent N+1, missing cache, Laravel-specific patterns
- `SYSTEM_GO` — focused on goroutine parallelism, sync.Pool, context propagation
- `SYSTEM_MIGRATION` — focused on identifying which PHP patterns map cleanly to Go

The LLM is instructed to return **strict JSON** (enforced via `response_format: {"type": "json_object"}`), which feeds directly into the patch engine without fragile regex parsing.

---

## The Patch Engine (4-Strategy Cascade)

When applying AI-suggested code fixes, the tool tries four strategies in order, stopping at the first success:

```
1. EXACT         — find the before snippet character-for-character
2. NORMALISED    — strip excess whitespace, try again
3. FUZZY         — sliding window search with difflib similarity ≥ 0.72
4. LLM-ASSISTED  — ask the LLM to locate the correct insertion point
```

This makes the patches robust even when the AI's "before" snippet doesn't exactly match the file (different indentation, minor version differences, surrounding code edits).

---

## Dependencies

| Dependency | Why |
|-----------|-----|
| `requests` | HTTP calls to LLM proxy and target API |
| `curl` | Latency profiling with per-layer timing (DNS/TCP/TLS) |
| `python3` | Embedded in profile script for p50/p95/p99 calculation |
| `git` | Branch creation and commit for PR workflow |
| `gh` / `glab` | PR creation (optional — falls back to branch-only if absent) |

No ML frameworks. No heavyweight SDKs. Runs anywhere Python 3.10+ and curl are available.

---

## Built For

This tool was built as a hackathon submission for IndiaMart's internal innovation challenge. The target is backend engineering teams who manage high-traffic PHP and Go APIs and need a way to systematically surface and fix latency regressions — without waiting for a performance engineer to become available.

The north star metric: **turn a 4-hour debugging session into a 31-second automated audit with a ready-to-merge PR.**

---

*GitHub: [github.com/maulik-dot/api-optimizer](https://github.com/maulik-dot/api-optimizer)*
