#!/usr/bin/env python3
"""
run.py — API Optimizer: one command, three stages, full pipeline

  Stage 1  →  Profile endpoint, detect language (PHP / Go / migration)
  Stage 2  →  AI analysis via imllm.intermesh.net  (bottlenecks + code fixes)
  Stage 3  →  Apply fixes, commit on new branch, generate PR description

Usage:
  python run.py --url https://your-api/v1/products --source /path/to/repo

  # Quick analysis only (skip PR generation)
  python run.py --url https://your-api/v1/products --source /path/to/repo --no-pr

  # Profile-only (no LLM)
  python run.py --url https://your-api/v1/products --profile-only

  # POST endpoint with auth
  python run.py --url https://your-api/v1/search \\
                --method POST --data '{"q":"bearings"}' \\
                --headers 'Authorization: Bearer <token>' \\
                --source /path/to/repo --push

  # See available LLM models
  python run.py --list-models

Environment:
  IMLLM_API_KEY   your API key for imllm.intermesh.net  (required for Stage 2+)
  IMLLM_MODEL     model name                             (or use --model)
  IMLLM_BASE_URL  override base URL                      (default: https://imllm.intermesh.net)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── add scripts/ to path ───────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "scripts"))

import llm_client as llm

# Lazy imports after env is wired (so API key overrides are respected)
def _import_stages():
    import check_api   as s1
    import analyze_api as s2
    import generate_pr as s3
    return s1, s2, s3


# ─── colour helpers ────────────────────────────────────────────────────────────

def c(text, code): return f"\033[{code}m{text}\033[0m"

def banner():
    print(c("""
  ╔══════════════════════════════════════════════════════╗
  ║                                                      ║
  ║        API OPTIMIZER AGENT  ⚡  IndiaMart           ║
  ║   Profile → Diagnose → Fix → PR  in one command     ║
  ║                                                      ║
  ╚══════════════════════════════════════════════════════╝""", "96"))

def stage_header(n: int, title: str):
    labels = {1: "PROFILE + DETECT", 2: "AI ANALYSIS", 3: "APPLY FIXES + PR"}
    label = labels.get(n, title)
    print(c(f"\n  ┌{'─'*54}┐", "94"))
    print(c(f"  │  STAGE {n}  —  {label:<43}│", "94"))
    print(c(f"  └{'─'*54}┘", "94"))

def stage_done(n: int, elapsed: float, msg: str = ""):
    tag = c(f"  STAGE {n} DONE", "92")
    t   = c(f"{elapsed:.1f}s", "37")
    print(f"\n  {tag}  {t}  {msg}")

def stage_skip(n: int, reason: str):
    print(c(f"\n  STAGE {n} SKIPPED  —  {reason}", "37"))

def stage_fail(n: int, err: str):
    print(c(f"\n  STAGE {n} FAILED  —  {err}", "91"))

def ok(msg):   print(c(f"  ✓  {msg}", "92"))
def warn(msg): print(c(f"  ⚠  {msg}", "93"))
def fail(msg): print(c(f"  ✗  {msg}", "91"))
def info(msg): print(f"     {msg}")


# ─── output directory helpers ──────────────────────────────────────────────────

def make_output_dir(base: str, url: str) -> str:
    slug = url.split("//")[-1].replace("/", "_").replace(":", "_")[:40]
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(base, f"{ts}_{slug}")
    os.makedirs(path, exist_ok=True)
    return path


# ─── stage 1 ───────────────────────────────────────────────────────────────────

def run_stage1(args, s1) -> dict | None:
    stage_header(1, "PROFILE + DETECT")
    t0 = time.time()

    extra_headers = [h.strip() for h in args.headers.split("|") if h.strip()] \
                    if args.headers else []

    # Health check
    health = s1.health_check(args.url, args.method, args.data or "", extra_headers)
    if not health.get("reachable"):
        stage_fail(1, "API unreachable — cannot continue")
        return None

    # Language detection
    lang = s1.run_language_detection(args.url, args.source, health.get("response_headers", {}))

    # Latency profiling
    latency = {}
    if health.get("http_code", 0) < 500:
        latency = s1.profile_latency(
            args.url, args.method, args.data or "", extra_headers, args.runs
        )
    else:
        warn("Skipping profiling — API returned 5xx")

    report = s1.build_report(args.url, args.method, health, lang, latency)
    stage_done(1, time.time() - t0,
               f"  {lang['language'].upper()}  |  "
               f"p95={latency.get('p95_ms','?')}ms  |  "
               f"app={latency.get('layers', {}).get('app_ms','?')}ms")
    return report


# ─── stage 2 ───────────────────────────────────────────────────────────────────

def run_stage2(args, s2, report: dict) -> dict | None:
    stage_header(2, "AI ANALYSIS")
    t0 = time.time()

    language = report.get("language_detection", {}).get("language", "unknown")
    if language == "unknown":
        warn("Language unknown — defaulting to Go analysis")
        language = "go"

    # Load source code
    source_code = ""
    if args.source:
        source_code = s2.load_source_files(args.source, language)
        if source_code:
            ok(f"Source loaded ({len(source_code):,} chars)")
        else:
            warn("No source files found — pattern-based analysis only")

    system_prompt, user_prompt = s2.build_prompt(report, source_code, language)

    print("  Calling LLM", end="", flush=True)
    dot_t = time.time()
    try:
        result = llm.chat_json(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4096,
        )
    except (PermissionError, ValueError, Exception) as e:
        print()
        stage_fail(2, str(e))
        return None

    elapsed_llm = time.time() - dot_t
    print(c(f"  {elapsed_llm:.1f}s", "37"))

    s2.display_analysis(result)

    n_bottlenecks = len(result.get("bottlenecks", []))
    total_ms  = result.get("total_estimated_improvement_ms", 0)
    total_pct = result.get("total_estimated_improvement_pct", 0)
    stage_done(2, time.time() - t0,
               f"  {n_bottlenecks} bottlenecks  |  "
               f"−{total_ms}ms  (−{total_pct}%)")
    return result


# ─── stage 3 ───────────────────────────────────────────────────────────────────

def run_stage3(args, s3, analysis: dict, out_dir: str, report_path: str) -> dict:
    stage_header(3, "APPLY FIXES + PR")
    t0 = time.time()

    if not args.source:
        stage_skip(3, "no --source provided — cannot patch files")
        return {"skipped": True}

    from generate_pr import (
        find_git_root, current_branch, has_remote,
        apply_fix_to_file, build_pr_description,
        git, syntax_check,
    )
    import re, shutil, subprocess
    from datetime import datetime, timezone

    source = os.path.abspath(args.source)
    repo_root = find_git_root(source)
    if not repo_root:
        stage_skip(3, f"not a git repo: {source}")
        return {"skipped": True}

    ok(f"Git repo: {repo_root}")
    base_branch = current_branch(repo_root)

    language    = analysis.get("language", "unknown")
    bottlenecks = analysis.get("bottlenecks", [])
    parallel    = analysis.get("parallel_opportunities", [])
    migration   = analysis.get("migration_plan", {})
    total_ms    = analysis.get("total_estimated_improvement_ms", 0)
    total_pct   = analysis.get("total_estimated_improvement_pct", 0)

    # Create branch
    slug   = re.sub(r"[^a-z0-9]+", "-", language.lower())
    ts     = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    branch = args.branch or f"api-optimizer/{slug}-{ts}"

    if not args.dry_run:
        r = git(["checkout", "-b", branch], cwd=repo_root, check=False)
        if r.returncode != 0:
            stage_fail(3, f"Could not create branch: {r.stderr.strip()}")
            return {"error": r.stderr.strip()}
        ok(f"Branch: {branch}")
    else:
        info(f"Would create branch: {branch}")

    # Apply patches
    patches_applied = []
    modified_files  = set()
    model = llm.DEFAULT_MODEL or None

    for b in bottlenecks:
        rank = b.get("rank", "?")
        fix  = b.get("fix", {})
        frel = b.get("file", "")
        cat  = b.get("category", "unknown").replace("_", " ")

        if not fix.get("before") or not fix.get("after") or not frel or frel == "unknown":
            warn(f"#{rank} — {cat}: skipped (no file/fix in analysis)")
            patches_applied.append({"rank": rank, "file": frel, "success": False,
                                     "method": "skipped", "error": "no file or fix data"})
            continue

        fabs = os.path.join(repo_root, frel)
        if not os.path.isfile(fabs):
            fabs = os.path.join(source, frel)
        if not os.path.isfile(fabs):
            warn(f"#{rank} — {cat}: file not found ({frel})")
            patches_applied.append({"rank": rank, "file": frel, "success": False,
                                     "method": "file_not_found", "error": f"not found: {frel}"})
            continue

        print(f"\n  Patching #{rank} — {c(cat, '93')}  [{frel}]")
        result = apply_fix_to_file(
            fabs, fix["before"], fix["after"],
            fix.get("description", ""), model=model, dry_run=args.dry_run,
        )

        if result.success:
            ok(f"Applied via {c(result.method, '96')}  (±{result.lines_changed} lines)")
            modified_files.add(fabs)
            if not args.dry_run:
                ok_syn, msg = syntax_check(fabs)
                if not ok_syn:
                    warn(f"Syntax error after patch — reverting: {msg}")
                    git(["checkout", "--", fabs], cwd=repo_root, check=False)
                    result.success = False
                    result.error   = f"syntax error: {msg}"
        else:
            warn(f"Could not patch: {result.error}")

        patches_applied.append({
            "rank": rank, "file": frel,
            "success": result.success, "method": result.method,
            "error": getattr(result, "error", ""),
        })

    # Migration: write Go file
    if migration.get("applicable") and migration.get("go_equivalent"):
        go_code = migration["go_equivalent"].strip()
        php_handler = next(
            (b.get("file", "") for b in bottlenecks if b.get("file", "").endswith(".php")),
            "app/handler.php"
        )
        go_path = os.path.join(repo_root, re.sub(r"\.php$", "_optimized.go", php_handler))
        if not args.dry_run:
            os.makedirs(os.path.dirname(go_path), exist_ok=True)
            open(go_path, "w").write(go_code + "\n")
            modified_files.add(go_path)
            ok(f"Wrote Go handler: {os.path.relpath(go_path, repo_root)}")

    # Commit
    successful = [p for p in patches_applied if p["success"]]
    if not args.dry_run and successful:
        for fabs in modified_files:
            git(["add", fabs], cwd=repo_root)
        staged = git(["diff", "--cached", "--name-only"], cwd=repo_root).stdout.strip()
        if staged:
            commit_msg = (
                f"perf: optimize {language} API — {total_pct}% latency reduction\n\n"
                f"Applied {len(successful)} fix(es) via API Optimizer Agent:\n"
            )
            for p in successful:
                b   = next((x for x in bottlenecks if x.get("rank") == p["rank"]), {})
                cat = b.get("category", "").replace("_", " ")
                commit_msg += f"- {p['file']}: fix {cat} (~{b.get('estimated_latency_saved_ms',0)}ms saved)\n"
            commit_msg += f"\nTotal improvement: -{total_ms}ms (-{total_pct}%)\n"
            commit_msg += "\nCo-Authored-By: API Optimizer Agent <optimizer@indiamart.com>"
            git(["commit", "-m", commit_msg], cwd=repo_root)
            ok(f"Committed {len(staged.splitlines())} file(s)")

    # PR description
    pr_md   = build_pr_description(analysis, patches_applied, branch)
    pr_path = os.path.join(out_dir, "pr_description.md")
    open(pr_path, "w").write(pr_md)
    ok(f"PR description: {pr_path}")

    # Push
    if args.push and not args.dry_run and successful:
        if has_remote(repo_root):
            r = git(["push", "-u", "origin", branch], cwd=repo_root, check=False)
            if r.returncode == 0:
                ok(f"Pushed: {branch}")
                gh   = shutil.which("gh")
                glab = shutil.which("glab")
                pr_title = f"perf: optimize {language} API — {total_pct}% latency reduction"
                if gh:
                    r2 = subprocess.run(
                        [gh, "pr", "create", "--title", pr_title,
                         "--body", pr_md, "--base", base_branch, "--head", branch],
                        capture_output=True, text=True
                    )
                    if r2.returncode == 0:
                        ok(f"PR created: {r2.stdout.strip()}")
                    else:
                        warn(f"gh pr create: {r2.stderr.strip()}")
                elif glab:
                    r2 = subprocess.run(
                        [glab, "mr", "create", "--title", pr_title,
                         "--description", pr_md,
                         "--target-branch", base_branch, "--source-branch", branch],
                        capture_output=True, text=True
                    )
                    if r2.returncode == 0:
                        ok(f"MR created: {r2.stdout.strip()}")
                    else:
                        warn(f"glab mr create: {r2.stderr.strip()}")
                else:
                    warn("Install gh or glab CLI to auto-open the PR.")
                    info(f"Push done. Open PR manually from branch: {branch}")
            else:
                warn(f"Push failed: {r.stderr.strip()}")
        else:
            warn("No git remote — skipping push.")

    n_ok = len(successful)
    stage_done(3, time.time() - t0,
               f"  {n_ok}/{len(patches_applied)} fixes applied  |  branch: {branch}")

    return {
        "branch": branch,
        "patches_applied": patches_applied,
        "pr_path": pr_path,
        "n_ok": n_ok,
    }


# ─── final summary ─────────────────────────────────────────────────────────────

def print_summary(report: dict | None, analysis: dict | None, pr: dict | None,
                  out_dir: str, total_elapsed: float):
    print(c(f"""
  ╔══════════════════════════════════════════════════════╗
  ║                    PIPELINE COMPLETE                 ║
  ╚══════════════════════════════════════════════════════╝""", "92"))

    if report:
        lp   = report.get("latency_profile", {})
        lang = report.get("language_detection", {}).get("language", "?")
        print(f"""
  STAGE 1 — PROFILE
    Language : {c(lang.upper(), '96')}
    p50      : {lp.get('p50_ms', '—')} ms
    p95      : {lp.get('p95_ms', '—')} ms
    App layer: {lp.get('layers', {}).get('app_ms', '—')} ms""")

    if analysis:
        total_ms  = analysis.get("total_estimated_improvement_ms", 0)
        total_pct = analysis.get("total_estimated_improvement_pct", 0)
        n_b = len(analysis.get("bottlenecks", []))
        print(f"""
  STAGE 2 — AI ANALYSIS
    Bottlenecks  : {c(str(n_b), '93')}
    Expected gain: {c(f'-{total_ms}ms  (-{total_pct}%)', '92')}
    Summary      : {analysis.get('summary', '')}""")

    if pr and not pr.get("skipped") and not pr.get("error"):
        n_ok = pr.get("n_ok", 0)
        n_total = len(pr.get("patches_applied", []))
        print(f"""
  STAGE 3 — PR
    Fixes applied : {c(f'{n_ok}/{n_total}', '92')}
    Branch        : {c(pr.get('branch', '—'), '96')}
    PR description: {pr.get('pr_path', '—')}""")

    print(f"""
  OUTPUT DIR  : {c(out_dir, '96')}
  TOTAL TIME  : {c(f'{total_elapsed:.1f}s', '37')}
""")


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="API Optimizer — Profile → AI Diagnose → Fix → PR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Full pipeline
  python run.py --url https://your-api/v1/products --source /path/to/repo

  # Profile only (no LLM call)
  python run.py --url https://your-api/v1/products --profile-only

  # Analyse without committing
  python run.py --url https://your-api/v1/products --source /path/to/repo --no-pr

  # POST with auth, then push PR
  python run.py --url https://your-api/v1/search \\
                --method POST --data '{"q":"bearings"}' \\
                --headers 'Authorization: Bearer <token>' \\
                --source /path/to/repo --push

  # List available LLM models
  python run.py --list-models
        """
    )

    # Endpoint
    ep = parser.add_argument_group("endpoint")
    ep.add_argument("--url",     "-u", help="API endpoint URL")
    ep.add_argument("--method",  "-m", default="GET",
                    help="HTTP method (default: GET)")
    ep.add_argument("--data",    "-d", default="",
                    help="Request body (JSON string)")
    ep.add_argument("--headers", "-H", default="",
                    help="Extra headers, pipe-separated: 'Authorization: Bearer x|X-Trace: y'")
    ep.add_argument("--runs",    "-r", type=int, default=20,
                    help="Profiling runs (default: 20)")

    # Source
    src = parser.add_argument_group("source")
    src.add_argument("--source",  "-s", help="Source code directory")

    # LLM
    ai = parser.add_argument_group("LLM")
    ai.add_argument("--api-key", "-k", help="API key (overrides IMLLM_API_KEY)")
    ai.add_argument("--model",        help="LLM model name (overrides IMLLM_MODEL)")
    ai.add_argument("--list-models",  action="store_true",
                    help="List available models and exit")

    # PR / git
    pr = parser.add_argument_group("git / PR")
    pr.add_argument("--branch",  "-b", help="Branch name for the fix (auto-generated if omitted)")
    pr.add_argument("--push",          action="store_true",
                    help="Push branch and open PR after committing")
    pr.add_argument("--dry-run",       action="store_true",
                    help="Show what would change without modifying files or running git")

    # Pipeline control
    ctl = parser.add_argument_group("pipeline control")
    ctl.add_argument("--profile-only", action="store_true",
                     help="Run Stage 1 only (no LLM, no PR)")
    ctl.add_argument("--no-pr",        action="store_true",
                     help="Run Stages 1 + 2 only (analyse but don't apply fixes)")
    ctl.add_argument("--output-dir",   default="./results",
                     help="Directory for output files (default: ./results)")

    args = parser.parse_args()

    # Wire env vars
    if args.api_key:
        os.environ["IMLLM_API_KEY"] = args.api_key
        llm.API_KEY = args.api_key
    if args.model:
        os.environ["IMLLM_MODEL"] = args.model
        llm.DEFAULT_MODEL = args.model

    # ── list models ───────────────────────────────────────────────────────────
    if args.list_models:
        banner()
        print(c("\n  AVAILABLE MODELS ON imllm.intermesh.net\n", "96"))
        try:
            models = llm.list_models()
            if not models:
                warn("No models returned. Check IMLLM_API_KEY.")
            for m in models:
                mid   = m.get("id") or m.get("model") or str(m)
                owner = m.get("owned_by", "")
                print(f"  •  {c(mid, '92')}  {c(owner, '37')}")
            print()
            info("Set your model:  export IMLLM_MODEL=<model-id>")
            info("Then run:        python run.py --url <url> --source <path>")
        except Exception as e:
            fail(str(e))
            sys.exit(1)
        return

    # ── validate required args ────────────────────────────────────────────────
    if not args.url:
        parser.error("--url is required (or use --list-models)")

    # ── gate LLM stages behind API key check ──────────────────────────────────
    need_llm = not args.profile_only
    if need_llm and not llm.API_KEY:
        fail("IMLLM_API_KEY is not set.")
        info("Set it:  export IMLLM_API_KEY=<your-key>")
        info("Or run profile-only:  --profile-only")
        sys.exit(1)

    if need_llm and not llm.DEFAULT_MODEL:
        fail("IMLLM_MODEL is not set.")
        info("List models:  python run.py --list-models")
        info("Then set:     export IMLLM_MODEL=<model-id>")
        sys.exit(1)

    # ── output directory ──────────────────────────────────────────────────────
    out_dir = make_output_dir(args.output_dir, args.url)

    banner()
    print(c(f"\n  Endpoint  : {args.url}", "96"))
    if args.source:
        print(c(f"  Source    : {args.source}", "96"))
    if llm.DEFAULT_MODEL:
        print(c(f"  Model     : {llm.DEFAULT_MODEL}", "96"))
    print(c(f"  Output    : {out_dir}", "96"))
    flags = []
    if args.profile_only: flags.append("profile-only")
    if args.no_pr:        flags.append("no-pr")
    if args.dry_run:      flags.append("dry-run")
    if args.push:         flags.append("push")
    if flags:
        print(c(f"  Flags     : {', '.join(flags)}", "93"))

    t_total = time.time()
    s1, s2, s3 = _import_stages()

    report   = None
    analysis = None
    pr_result = None

    # ── STAGE 1 ───────────────────────────────────────────────────────────────
    report = run_stage1(args, s1)
    if report:
        report_path = os.path.join(out_dir, "stage1_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        ok(f"Saved: {report_path}")

    if args.profile_only or not report:
        if report:
            stage_skip(2, "--profile-only flag set")
            stage_skip(3, "--profile-only flag set")
        print_summary(report, None, None, out_dir, time.time() - t_total)
        return

    # ── STAGE 2 ───────────────────────────────────────────────────────────────
    analysis = run_stage2(args, s2, report)
    if analysis:
        analysis_path = os.path.join(out_dir, "stage2_analysis.json")
        with open(analysis_path, "w") as f:
            json.dump(analysis, f, indent=2)
        ok(f"Saved: {analysis_path}")

    if args.no_pr or not analysis:
        if analysis:
            stage_skip(3, "--no-pr flag set")
        print_summary(report, analysis, None, out_dir, time.time() - t_total)
        return

    # ── STAGE 3 ───────────────────────────────────────────────────────────────
    pr_result = run_stage3(args, s3, analysis, out_dir, report_path)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    print_summary(report, analysis, pr_result, out_dir, time.time() - t_total)


if __name__ == "__main__":
    main()
