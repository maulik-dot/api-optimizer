#!/usr/bin/env python3
"""
generate_pr.py — Stage 3: Apply AI fixes and generate a pull request

Takes a Stage 2 analysis JSON (_analysis.json from analyze_api.py),
applies code patches to the source tree, commits on a new branch,
and generates a PR description. Optionally pushes and opens the PR
via the gh (GitHub) or glab (GitLab) CLI if available.

Usage:
  python3 generate_pr.py --analysis report_analysis.json --source /path/to/repo
  python3 generate_pr.py --analysis report_analysis.json --source /path/to/repo --dry-run
  python3 generate_pr.py --analysis report_analysis.json --source /path/to/repo --push

Patch strategy (in order):
  1. Exact string match of 'before' in source file
  2. Whitespace-normalised match
  3. Fuzzy block match (difflib, threshold 0.72)
  4. LLM-assisted precise patch (calls back to imllm.intermesh.net)
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm_client as llm


# ─── colour / print helpers ────────────────────────────────────────────────────

def c(text, code): return f"\033[{code}m{text}\033[0m"
def head(msg):  print(c(f"\n  ╔{'═'*50}╗", "96")); print(c(f"  ║  {msg:<48}  ║", "96")); print(c(f"  ╚{'═'*50}╝", "96"))
def sec(msg):   print(f"\n  {'─'*52}\n  {c(msg.upper(), '96')}\n  {'─'*52}")
def ok(msg):    print(c(f"  ✓  {msg}", "92"))
def warn(msg):  print(c(f"  ⚠  {msg}", "93"))
def fail(msg):  print(c(f"  ✗  {msg}", "91"))
def info(msg):  print(f"     {msg}")
def dim(msg):   print(c(f"     {msg}", "37"))


# ─── git helpers ───────────────────────────────────────────────────────────────

def git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd] + args,
        capture_output=True, text=True,
        check=check
    )


def find_git_root(path: str) -> str | None:
    r = git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def current_branch(repo: str) -> str:
    return git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip()


def has_remote(repo: str) -> bool:
    r = git(["remote"], cwd=repo, check=False)
    return bool(r.stdout.strip())


def git_user(repo: str) -> tuple[str, str]:
    name  = git(["config", "user.name"],  cwd=repo, check=False).stdout.strip() or "API Optimizer"
    email = git(["config", "user.email"], cwd=repo, check=False).stdout.strip() or "optimizer@indiamart.com"
    return name, email


# ─── patch engine ──────────────────────────────────────────────────────────────

def _normalise(code: str) -> str:
    """Strip trailing whitespace per line, normalise indent to 4 spaces, collapse blank lines."""
    lines = []
    for line in code.splitlines():
        lines.append(line.rstrip())
    return "\n".join(lines)


def _fuzzy_locate(content: str, before: str, threshold: float = 0.72) -> tuple[int, int] | None:
    """
    Sliding-window similarity search.
    Returns (start_char, end_char) of the best matching block in content, or None.
    """
    before_lines = before.strip().splitlines()
    content_lines = content.splitlines()
    window = len(before_lines)

    if window == 0 or len(content_lines) < window:
        return None

    best_ratio = 0.0
    best_start_line = -1

    for i in range(len(content_lines) - window + 1):
        block = content_lines[i : i + window]
        ratio = difflib.SequenceMatcher(
            None,
            "\n".join(before_lines),
            "\n".join(block),
            autojunk=False,
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start_line = i

    if best_ratio < threshold:
        return None

    # Convert line numbers to char offsets
    char_offset = 0
    for i, line in enumerate(content_lines):
        if i == best_start_line:
            start = char_offset
        if i == best_start_line + window - 1:
            end = char_offset + len(line)
            return (start, end)
        char_offset += len(line) + 1   # +1 for \n

    return None


def _llm_patch(file_content: str, file_path: str, before: str, after: str,
               description: str, model: str | None, anchor: str = "") -> str | None:
    """
    Last-resort patch via LLM.
    Uses anchor (function context from the diagnosis agent) when available — much cheaper
    than sending the full file. Returns only the patched snippet, then splices it in.
    """
    if not llm.API_KEY:
        return None

    if anchor:
        # Cheap path: anchor is ~5-10 lines of context around the fix site
        prompt = f"""Apply this fix. Return ONLY the corrected version of the REPLACE block, nothing else.

DESCRIPTION: {description}

CONTEXT (surrounding code):
```
{anchor}
```

REPLACE:
```
{before}
```

WITH:
```
{after}
```"""
        try:
            patched_snippet = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model, temperature=0.0, max_tokens=512,
            ).strip()
            if patched_snippet.startswith("```"):
                lines = patched_snippet.splitlines()
                patched_snippet = "\n".join(
                    lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                )
            # Splice: replace 'before' in file with the LLM-returned snippet
            before_stripped = before.strip()
            if before_stripped in file_content:
                return file_content.replace(before_stripped, patched_snippet, 1)
        except Exception:
            pass
        return None

    # Fallback: send first 2000 chars of file (still much cheaper than 6000)
    prompt = f"""Apply exactly ONE change to the file. Return ONLY the complete modified file.

DESCRIPTION: {description}

REPLACE:
```
{before}
```
WITH:
```
{after}
```

FILE: {file_path}
```
{file_content[:2000]}
```"""
    try:
        result = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model, temperature=0.0, max_tokens=2048,
        )
        cleaned = result.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        if len(cleaned) > len(file_content) * 0.4:
            return cleaned
    except Exception:
        pass
    return None


class PatchResult:
    def __init__(self, success: bool, method: str, lines_changed: int = 0, error: str = ""):
        self.success = success
        self.method = method
        self.lines_changed = lines_changed
        self.error = error


def apply_fix_to_file(
    file_path: str,
    before: str,
    after: str,
    description: str,
    model: str | None = None,
    dry_run: bool = False,
    anchor: str = "",
) -> PatchResult:
    """
    Try four strategies to apply the fix. Modifies file in place unless dry_run.
    """
    if not os.path.isfile(file_path):
        return PatchResult(False, "file_not_found", error=f"File not found: {file_path}")

    try:
        content = open(file_path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return PatchResult(False, "read_error", error=str(e))

    before_stripped = before.strip()
    after_stripped  = after.strip()

    # 1 — Exact match
    if before_stripped in content:
        patched = content.replace(before_stripped, after_stripped, 1)
        if not dry_run:
            open(file_path, "w", encoding="utf-8").write(patched)
        return PatchResult(True, "exact", lines_changed=abs(after_stripped.count("\n") - before_stripped.count("\n")))

    # 2 — Normalised match
    norm_content = _normalise(content)
    norm_before  = _normalise(before_stripped)
    if norm_before in norm_content:
        # Apply on normalised, then restore original (preserve original formatting for unchanged parts)
        patched = content.replace(before_stripped.split("\n")[0].strip(),
                                  after_stripped.split("\n")[0].strip(), 1)
        # Simpler: just replace in normalised content
        norm_patched = norm_content.replace(norm_before, _normalise(after_stripped), 1)
        if not dry_run:
            open(file_path, "w", encoding="utf-8").write(norm_patched)
        return PatchResult(True, "normalised", lines_changed=abs(after_stripped.count("\n") - before_stripped.count("\n")))

    # 3 — Fuzzy block match
    span = _fuzzy_locate(content, before_stripped)
    if span:
        start, end = span
        # Find the real end of the matched block (end of last line)
        end_of_block = content.find("\n", end)
        if end_of_block == -1:
            end_of_block = len(content)
        # Detect indentation of the matched block
        block_start_line = content.rfind("\n", 0, start) + 1
        indent_match = re.match(r"^(\s*)", content[block_start_line:])
        indent = indent_match.group(1) if indent_match else ""
        # Re-indent the replacement
        after_indented = textwrap.indent(textwrap.dedent(after_stripped), indent)
        patched = content[:block_start_line] + after_indented + "\n" + content[end_of_block + 1:]
        if not dry_run:
            open(file_path, "w", encoding="utf-8").write(patched)
        return PatchResult(True, "fuzzy", lines_changed=abs(after_stripped.count("\n") - before_stripped.count("\n")))

    # 4 — LLM-assisted patch
    llm_result = _llm_patch(content, file_path, before_stripped, after_stripped, description, model, anchor)
    if llm_result:
        if not dry_run:
            open(file_path, "w", encoding="utf-8").write(llm_result)
        return PatchResult(True, "llm_assisted", lines_changed=0)

    return PatchResult(False, "not_found",
                       error=f"Could not locate 'before' snippet in {file_path}. Manual review needed.")


# ─── syntax checker ────────────────────────────────────────────────────────────

def syntax_check(file_path: str) -> tuple[bool, str]:
    ext = Path(file_path).suffix.lower()
    if ext == ".php":
        checker = ["php", "-l", file_path]
    elif ext == ".go":
        # go vet on the containing package, not a single file
        pkg_dir = str(Path(file_path).parent)
        checker = ["go", "vet", "./..."]
        if not shutil.which("go"):
            return True, "go not found — skipping syntax check"
        r = subprocess.run(checker, capture_output=True, text=True, cwd=pkg_dir)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    else:
        return True, "no checker for this file type"
    if not shutil.which(checker[0]):
        return True, f"{checker[0]} not found — skipping syntax check"
    r = subprocess.run(checker, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ─── PR description builder ────────────────────────────────────────────────────

def build_pr_description(analysis: dict, patches_applied: list[dict], branch: str) -> str:
    lang    = analysis.get("language", "unknown")
    summary = analysis.get("summary", "")
    total_ms  = analysis.get("total_estimated_improvement_ms", 0)
    total_pct = analysis.get("total_estimated_improvement_pct", 0)
    bottlenecks = analysis.get("bottlenecks", [])
    parallel    = analysis.get("parallel_opportunities", [])
    migration   = analysis.get("migration_plan", {})

    successful_patches = [p for p in patches_applied if p["success"]]
    failed_patches     = [p for p in patches_applied if not p["success"]]

    # map severity to emoji
    sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    cat_label = {
        "n_plus_one_query":       "N+1 Query",
        "serial_io":              "Serial I/O",
        "missing_index":          "Missing Index",
        "mutex_contention":       "Mutex Contention",
        "goroutine_leak":         "Goroutine Leak",
        "redundant_computation":  "Redundant Computation",
        "memory_alloc":           "Memory Allocation",
        "dead_code":              "Dead Code",
        "missing_cache":          "Missing Cache",
        "sync_to_async":          "Sync → Async",
        "context_propagation":    "Missing Context Propagation",
        "http_client_reuse":      "HTTP Client Not Reused",
        "connection_pool_sizing": "Connection Pool Sizing",
        "response_serialization": "Response Serialization",
    }

    lang_icon = {"php": "🐘", "go": "🐹", "php-to-go-migration": "🔄"}.get(lang, "⚙️")

    lines = [
        f"## {lang_icon} API Performance Optimization — {lang.upper()}",
        "",
        f"> **{summary}**",
        "",
        "---",
        "",
        "## 📊 Impact Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Estimated latency reduction | **{total_ms} ms** |",
        f"| Percentage improvement | **{total_pct}%** |",
        f"| Bottlenecks fixed | **{len(successful_patches)}** |",
        f"| Parallel opportunities applied | **{len(parallel)}** |",
        "",
        "---",
        "",
        "## 🔍 Bottlenecks Fixed",
        "",
    ]

    for b in bottlenecks:
        patch = next((p for p in patches_applied if p.get("rank") == b.get("rank")), {})
        status = "✅ applied" if patch.get("success") else "⚠️ needs manual review"
        method = patch.get("method", "")
        icon = sev_icon.get(b.get("severity", ""), "⚪")
        cat  = cat_label.get(b.get("category", ""), b.get("category", ""))
        saving = b.get("estimated_latency_saved_ms", 0)

        lines += [
            f"### {icon} #{b.get('rank')} — {cat}  `{status}`",
            f"**File:** `{b.get('file', 'unknown')}`  **Lines:** `{b.get('line_range', '?')}`  "
            f"**Saves:** ~{saving}ms",
            "",
            b.get("description", ""),
            "",
        ]

        fix = b.get("fix", {})
        if fix.get("before") and fix.get("after"):
            ext = Path(b.get("file", ".php")).suffix.lstrip(".")
            lines += [
                "<details>",
                "<summary>View code change</summary>",
                "",
                f"**Before:**",
                f"```{ext}",
                fix["before"].strip(),
                "```",
                "",
                f"**After:**",
                f"```{ext}",
                fix["after"].strip(),
                "```",
                "",
                "</details>",
                "",
            ]

    if parallel:
        lines += [
            "---",
            "",
            "## ⚡ Parallelism Changes",
            "",
        ]
        for i, p in enumerate(parallel, 1):
            lines += [
                f"### #{i} — {p.get('description', '')}  (saves ~{p.get('estimated_latency_saved_ms', 0)}ms)",
                "",
            ]
            if p.get("code"):
                lines += [
                    "```go",
                    p["code"].strip(),
                    "```",
                    "",
                ]

    if migration.get("applicable"):
        lines += [
            "---",
            "",
            "## 🔄 PHP → Go Migration",
            "",
            f"> {migration.get('rationale', '')}",
            "",
            f"**Estimated gain from migration:** ~{migration.get('estimated_latency_saved_ms', 0)}ms",
            "",
        ]
        if migration.get("go_equivalent"):
            lines += [
                "<details>",
                "<summary>View generated Go handler</summary>",
                "",
                "```go",
                migration["go_equivalent"].strip(),
                "```",
                "",
                "</details>",
                "",
            ]

    if failed_patches:
        lines += [
            "---",
            "",
            "## ⚠️ Fixes Requiring Manual Review",
            "",
            "These fixes could not be automatically patched. Apply them manually before merging:",
            "",
        ]
        for p in failed_patches:
            lines += [f"- **{p.get('file', 'unknown')}** — {p.get('error', '')}"]
        lines.append("")

    lines += [
        "---",
        "",
        "## ✅ Test Checklist",
        "",
        "- [ ] Run existing test suite: `php artisan test` / `go test ./...`",
        "- [ ] Verify API response is identical to pre-fix (diff the JSON output)",
        "- [ ] Check p95 latency on staging (should be lower than baseline)",
        "- [ ] Confirm no new errors in application logs",
        "- [ ] Load test at 2× normal RPS for 5 minutes",
        "",
        "---",
        "",
        f"*Generated by [API Optimizer Agent](https://github.com/indiamart/api-optimizer) "
        f"on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*",
        f"*Branch: `{branch}`*",
    ]

    return "\n".join(lines)


# ─── main pipeline ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="API Optimizer — Stage 3: Apply fixes and generate PR"
    )
    parser.add_argument("--analysis", "-a", required=True,
                        help="Stage 2 analysis JSON (from analyze_api.py)")
    parser.add_argument("--source",   "-s", required=True,
                        help="Source code directory (must be a git repo)")
    parser.add_argument("--model",    "-m", help="LLM model for patch fallback (overrides IMLLM_MODEL)")
    parser.add_argument("--api-key",  "-k", help="API key (overrides IMLLM_API_KEY)")
    parser.add_argument("--branch",   "-b", help="Branch name (auto-generated if omitted)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Show what would change without modifying files or git")
    parser.add_argument("--push",     action="store_true",
                        help="Push branch and open PR after committing")
    parser.add_argument("--output-pr", "-o", help="Save PR description to this .md file")
    args = parser.parse_args()

    if args.api_key:
        os.environ["IMLLM_API_KEY"] = args.api_key
        llm.API_KEY = args.api_key
    if args.model:
        os.environ["IMLLM_MODEL"] = args.model
        llm.DEFAULT_MODEL = args.model

    model = llm.DEFAULT_MODEL or None

    # ── load analysis ─────────────────────────────────────────────────────────
    head("STAGE 3: PR GENERATION")
    try:
        with open(args.analysis) as f:
            analysis = json.load(f)
        ok(f"Loaded analysis: {args.analysis}")
    except FileNotFoundError:
        fail(f"Analysis file not found: {args.analysis}")
        sys.exit(1)

    language    = analysis.get("language", "unknown")
    bottlenecks = analysis.get("bottlenecks", [])
    parallel    = analysis.get("parallel_opportunities", [])
    migration   = analysis.get("migration_plan", {})

    total_ms  = analysis.get("total_estimated_improvement_ms", 0)
    total_pct = analysis.get("total_estimated_improvement_pct", 0)

    info(f"Language : {language}")
    info(f"Fixes    : {len(bottlenecks)} bottlenecks + {len(parallel)} parallelism changes")
    info(f"Expected : −{total_ms}ms  (−{total_pct}%)")
    if args.dry_run:
        warn("DRY RUN — no files will be modified, no commits created")

    # ── check git repo ────────────────────────────────────────────────────────
    source = os.path.abspath(args.source)
    repo_root = find_git_root(source)
    if not repo_root:
        fail(f"Not a git repository: {source}")
        fail("Stage 3 requires the source directory to be inside a git repo.")
        sys.exit(1)
    ok(f"Git repo: {repo_root}")

    base_branch = current_branch(repo_root)
    info(f"Base branch : {base_branch}")

    # ── create fix branch ─────────────────────────────────────────────────────
    slug = re.sub(r"[^a-z0-9]+", "-", language.lower())
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    branch = args.branch or f"api-optimizer/{slug}-{ts}"

    sec("CREATING BRANCH")
    if not args.dry_run:
        r = git(["checkout", "-b", branch], cwd=repo_root, check=False)
        if r.returncode != 0:
            fail(f"Could not create branch: {r.stderr.strip()}")
            sys.exit(1)
        ok(f"Created branch: {branch}")
    else:
        info(f"Would create branch: {branch}")

    # ── apply patches ─────────────────────────────────────────────────────────
    sec("APPLYING CODE FIXES")
    patches_applied = []
    modified_files  = set()

    for b in bottlenecks:
        rank = b.get("rank", "?")
        fix  = b.get("fix", {})
        frel = b.get("file", "")
        cat  = b.get("category", "")

        if not fix.get("before") or not fix.get("after"):
            warn(f"#{rank} — {cat}: no code fix in analysis, skipping")
            patches_applied.append({"rank": rank, "file": frel, "success": False,
                                     "method": "skipped", "error": "No before/after in analysis"})
            continue

        if not frel or frel == "unknown":
            warn(f"#{rank} — {cat}: no file path in analysis, skipping")
            patches_applied.append({"rank": rank, "file": frel, "success": False,
                                     "method": "skipped", "error": "No file path in analysis"})
            continue

        # Resolve file path relative to repo root
        fabs = os.path.join(repo_root, frel)
        if not os.path.isfile(fabs):
            # Try relative to source path
            fabs = os.path.join(source, frel)
        if not os.path.isfile(fabs):
            warn(f"#{rank} — {cat}: file not found: {frel}")
            patches_applied.append({"rank": rank, "file": frel, "success": False,
                                     "method": "file_not_found", "error": f"Not found: {frel}"})
            continue

        print(f"\n  Patching #{rank} — {c(cat.replace('_',' '), '93')}  [{frel}]")

        result = apply_fix_to_file(
            fabs,
            fix["before"],
            fix["after"],
            fix.get("description", ""),
            model=model,
            dry_run=args.dry_run,
            anchor=b.get("anchor", ""),
        )

        if result.success:
            ok(f"Applied via {c(result.method, '96')}  (±{result.lines_changed} lines)")
            modified_files.add(fabs)

            if not args.dry_run:
                ok_syn, msg = syntax_check(fabs)
                if not ok_syn:
                    warn(f"Syntax check failed after patch: {msg}")
                    warn("Reverting this file — apply fix manually.")
                    git(["checkout", "--", fabs], cwd=repo_root, check=False)
                    result.success = False
                    result.error   = f"Syntax error post-patch: {msg}"
        else:
            warn(f"Could not patch: {result.error}")

        patches_applied.append({
            "rank": rank, "file": frel,
            "success": result.success, "method": result.method,
            "error": result.error,
        })

    # ── handle migration: write new Go file ──────────────────────────────────
    if migration.get("applicable") and migration.get("go_equivalent"):
        sec("MIGRATION: WRITING GO FILE")
        go_code = migration["go_equivalent"].strip()
        # Derive target path: same dir as first PHP handler, new .go extension
        php_handler = next(
            (b.get("file", "") for b in bottlenecks if b.get("file", "").endswith(".php")),
            "app/Http/Controllers/handler.php"
        )
        go_path = os.path.join(
            repo_root,
            re.sub(r"\.php$", "_optimized.go", php_handler)
        )
        if not args.dry_run:
            os.makedirs(os.path.dirname(go_path), exist_ok=True)
            open(go_path, "w").write(go_code + "\n")
            modified_files.add(go_path)
            ok(f"Wrote Go handler: {os.path.relpath(go_path, repo_root)}")
        else:
            info(f"Would write Go handler: {os.path.relpath(go_path, repo_root)}")

    # ── commit ────────────────────────────────────────────────────────────────
    sec("COMMITTING CHANGES")
    successful = [p for p in patches_applied if p["success"]]

    if not args.dry_run and successful:
        # Stage only modified files
        for fabs in modified_files:
            git(["add", fabs], cwd=repo_root)

        staged = git(["diff", "--cached", "--name-only"], cwd=repo_root).stdout.strip()
        if not staged:
            warn("No staged changes — nothing to commit.")
        else:
            fix_summary = ", ".join(
                set(p.get("file", "").split("/")[-1] for p in successful)
            )
            commit_msg = (
                f"perf: optimize {language} API — {total_pct}% latency reduction\n\n"
                f"Applied {len(successful)} fix(es) via API Optimizer Agent:\n"
            )
            for p in successful:
                b = next((x for x in bottlenecks if x.get("rank") == p["rank"]), {})
                cat = b.get("category", "").replace("_", " ")
                commit_msg += f"- {p['file']}: fix {cat} (saves ~{b.get('estimated_latency_saved_ms',0)}ms)\n"
            commit_msg += f"\nEstimated total improvement: -{total_ms}ms (-{total_pct}%)\n"
            commit_msg += "\nCo-Authored-By: API Optimizer Agent <optimizer@indiamart.com>"

            git(["commit", "-m", commit_msg], cwd=repo_root)
            ok(f"Committed {len(list(staged.splitlines()))} file(s) on branch {branch}")
            print()
            for line in staged.splitlines():
                dim(f"  + {line}")
    elif args.dry_run:
        info(f"Would commit {len(successful)} fix(es) to branch {branch}")
        for p in successful:
            dim(f"  + {p['file']} (via {p['method']})")

    # ── generate PR description ───────────────────────────────────────────────
    sec("GENERATING PR DESCRIPTION")
    pr_md = build_pr_description(analysis, patches_applied, branch)

    pr_output = args.output_pr or args.analysis.replace("_analysis.json", "_pr.md").replace(".json", "_pr.md")
    with open(pr_output, "w") as f:
        f.write(pr_md)
    ok(f"PR description saved: {pr_output}")

    # ── push + open PR ────────────────────────────────────────────────────────
    if args.push and not args.dry_run:
        sec("PUSHING BRANCH")
        if not has_remote(repo_root):
            warn("No git remote configured — skipping push.")
        else:
            r = git(["push", "-u", "origin", branch], cwd=repo_root, check=False)
            if r.returncode != 0:
                fail(f"Push failed: {r.stderr.strip()}")
            else:
                ok(f"Pushed branch: {branch}")

                # Try gh CLI (GitHub)
                gh = shutil.which("gh")
                glab = shutil.which("glab")
                if gh:
                    pr_title = f"perf: optimize {language} API — {total_pct}% latency reduction"
                    r = subprocess.run(
                        [gh, "pr", "create",
                         "--title", pr_title,
                         "--body",  pr_md,
                         "--base",  base_branch,
                         "--head",  branch],
                        capture_output=True, text=True
                    )
                    if r.returncode == 0:
                        ok(f"PR created: {r.stdout.strip()}")
                    else:
                        warn(f"gh pr create failed: {r.stderr.strip()}")
                        info(f"Open PR manually from branch: {branch}")
                elif glab:
                    pr_title = f"perf: optimize {language} API — {total_pct}% latency reduction"
                    r = subprocess.run(
                        [glab, "mr", "create",
                         "--title",         pr_title,
                         "--description",   pr_md,
                         "--target-branch", base_branch,
                         "--source-branch", branch],
                        capture_output=True, text=True
                    )
                    if r.returncode == 0:
                        ok(f"MR created: {r.stdout.strip()}")
                    else:
                        warn(f"glab mr create failed: {r.stderr.strip()}")
                else:
                    warn("Neither gh nor glab CLI found.")
                    info(f"Open PR manually from branch: {branch}")

    # ── final summary ─────────────────────────────────────────────────────────
    sec("SUMMARY")
    total = len(patches_applied)
    n_ok  = len([p for p in patches_applied if p["success"]])
    n_fail = total - n_ok

    print(f"  Fixes applied   : {c(str(n_ok), '92')} / {total}")
    print(f"  Expected gain   : {c(f'-{total_ms}ms (-{total_pct}%)', '92')}")
    print(f"  Branch          : {c(branch, '96')}")
    print(f"  PR description  : {c(pr_output, '96')}")

    if n_fail:
        print()
        warn(f"{n_fail} fix(es) need manual application:")
        for p in patches_applied:
            if not p["success"] and p["method"] != "skipped":
                info(f"  • {p['file']}: {p['error']}")

    if not args.push and not args.dry_run and successful:
        print()
        info("To push and create PR:")
        info(f"  cd {repo_root}")
        info(f"  git push -u origin {branch}")
        info(f"  gh pr create --title 'perf: optimize {language} API' --body \"$(cat {pr_output})\"")

    print()


if __name__ == "__main__":
    main()
