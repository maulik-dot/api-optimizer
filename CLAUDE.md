# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the tool

```bash
# Set up
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Required env vars for Stage 2+
export IMLLM_API_KEY=<your-key>
export IMLLM_MODEL=gemini-2.5-flash        # or use --list-models to see options

# Profile only (no LLM, no file changes)
python run.py --url https://your-api/v1/products --profile-only

# Full pipeline, dry run (no file writes)
python run.py --url https://your-api/v1/products --source /path/to/repo --dry-run

# Full pipeline with PR
python run.py --url https://your-api/v1/products --source /path/to/repo --push

# List available LLM models
python run.py --list-models

# Run individual stages standalone
python scripts/check_api.py --url <url> --output report.json
python scripts/analyze_api.py --report report.json --source /path/to/repo
python scripts/generate_pr.py --analysis report_analysis.json --source /path/to/repo --dry-run
```

There are no tests or linter configurations in this project.

## Architecture

Three-stage pipeline wired together by `run.py`:

**Stage 1 — `scripts/check_api.py`**  
Health-checks the endpoint, detects language (PHP / Go / php-to-go-migration) via `scripts/detect_language.py`, and profiles latency across N curl runs (default 20). Produces a structured JSON report with p50/p75/p95/p99, mean, and per-layer timing (DNS/TCP/TLS/App/Body). Output saved to `./results/<timestamp>_<slug>/stage1_report.json`.

**Stage 2 — `scripts/analyze_api.py`**  
Loads the Stage 1 report and the most relevant source files (scored by path keywords: controller, handler, service, repository, etc.; capped at 20K chars). Sends a language-specific system prompt (`SYSTEM_PHP`, `SYSTEM_GO`, or `SYSTEM_MIGRATION`) plus the profiling data to the LLM. The LLM must return strict JSON matching `OUTPUT_SCHEMA`: ranked bottlenecks with before/after code diffs, parallelism opportunities, optional PHP→Go migration plan, and estimated latency savings. Output saved to `stage2_analysis.json`.

**Stage 3 — `scripts/generate_pr.py`**  
Applies AI-suggested patches using a 4-strategy cascade (exact match → whitespace-normalised → fuzzy difflib ≥ 0.72 → LLM-assisted placement), runs a syntax check after each patch and reverts on failure, commits to a new branch `api-optimizer/<lang>-<timestamp>`, generates a PR description markdown, and optionally pushes + opens a PR via `gh` or `glab` CLI.

**LLM client — `scripts/llm_client.py`**  
Thin wrapper around the IndiaMart LiteLLM proxy at `imllm.intermesh.net` (OpenAI-compatible). Uses raw `requests` — no OpenAI SDK. `chat_json()` strips markdown fences and retries JSON parsing once on failure. Env vars: `IMLLM_API_KEY` (required), `IMLLM_MODEL` (required for chat), `IMLLM_BASE_URL` (optional override).

## Key design constraints

- `MAX_SOURCE_CHARS = 20_000` in `analyze_api.py` — source files sent to the LLM are capped here. Files scoring low (test, config, migration, seed paths) are deprioritised; high-value paths (controller, handler, service, repository) get priority.
- The LLM is prompted to return code diffs with `STRICT MAX 8 LINES` per before/after block to keep responses within `max_tokens=4096`.
- Stage 3 runs a syntax check (`php -l` for PHP, `go build ./...` for Go) after every patch and auto-reverts the file if it fails.
- Branch naming: `api-optimizer/<language>-<YYYYMMDD-HHMM>`. Override with `--branch`.
- Results directory: `./results/<timestamp>_<url-slug>/` — each run gets its own subdirectory.
