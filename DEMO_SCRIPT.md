# Demo Video Script
## AI-Powered API Performance Optimizer & Migration Agent
**Target length:** 7–8 minutes | **Format:** Screen recording + voiceover

---

## BEFORE YOU HIT RECORD

**Terminal setup (split into 3 panes):**
- Pane 1: ready to run `run.py` commands
- Pane 2: ready to `cat` output files / show git log
- Pane 3: open the repo with a slow PHP controller visible

**Browser tabs pre-loaded:**
- Tab 1: IndiaMart homepage (to establish scale)
- Tab 2: The generated `pr_description.md` rendered (use grip or GitHub)

**Font size:** 20px minimum. Judges watch on laptop screens.

**Increase terminal contrast:** `export PS1="$ "` — cleaner screen recording.

---

---

# BEAT 1 — PROBLEM  `[0:00 – 1:30]`
*Rubric axis: Pinch Metrics. Goal: make the pain feel real with specific numbers.*

---

**[SCREEN: plain black slide or terminal with these numbers typed out slowly]**

> "Let me start with a number that surprised me when I dug into it."
>
> "Our backend engineering team — like most teams at this scale —
> spends **thirty to forty percent** of every sprint not shipping features.
> They're debugging slow APIs."

**[TYPE into terminal, slowly, so it lands:]**
```
# Average time to locate a single API bottleneck:   4–8 hours
# PHP → Go migration cost per endpoint:             2–5 dev-days
# APIs with parallelisable calls running serially:  ~60%
# N+1 query patterns in legacy PHP list endpoints:  ~1 in 3
```

> "Four to eight hours — just to find where the problem is.
> Not to fix it. Just to find it."

**[PAUSE 2 seconds]**

> "And this is not a tooling gap that's easy to see. Most profilers tell you
> *that* an API is slow. None of them tell you *which line* is slow,
> *why* it's slow, and hand you the fix."

> "At IndiaMart's scale — millions of B2B transactions every day —
> a hundred millisecond regression in a buyer-facing API
> directly hits engagement. The blast radius is wide."

> "So the question I asked was: what if the entire loop —
> detect, diagnose, fix, raise the PR —
> could happen in under three minutes, with one command?"

**[PAUSE 1 second — let that land]**

---

---

# BEAT 2 — PROOF  `[1:30 – 4:30]`
*Rubric axis: Robustness. Goal: show it working on real code, not slides.*

---

## 2a — Show the broken API  `[1:30 – 2:00]`

**[SCREEN: Pane 3 — open the PHP controller. Scroll to the N+1 loop.]**

```php
public function index(Request $request)
{
    $products = Product::all();

    foreach ($products as $product) {
        $product->category = Category::find($product->category_id);
        $product->seller   = Seller::find($product->seller_id);
    }

    return response()->json($products);
}
```

> "Here's a real pattern we see constantly. A product listing endpoint.
> Looks reasonable. But for a hundred products, this fires
> **two hundred extra database queries** — one per product, per relation.
> This is the N+1 problem."

> "A developer staring at this in a PR review might not catch it.
> The linter won't catch it. The unit tests pass. It only shows up
> in production, when response time quietly climbs from 80ms to 800ms."

---

## 2b — Stage 1: Profile  `[2:00 – 2:45]`

**[SCREEN: Pane 1. Clear terminal. Run:]**

```bash
python run.py \
  --url https://your-api.indiamart.com/v2/catalog \
  --source ./catalog-service \
  --no-pr
```

> "Stage one. I give it the endpoint and the source directory.
> It fires twenty curl requests, measures timing at every layer —
> DNS, TCP, TLS, app processing, body transfer —
> and it detects the language from response headers and the source tree."

**[WAIT for Stage 1 output. When the layer breakdown appears, point at it:]**

> "Look at this breakdown. App processing: **332 milliseconds**.
> That's eighty-seven percent of total response time sitting in
> the handler and its database calls. The network is fine.
> The problem is in the code."

> "And the language detector has already confirmed: PHP, high confidence,
> forty-eight source files."

---

## 2c — Stage 2: AI Analysis  `[2:45 – 3:45]`

**[Stage 2 starts automatically. Show the "Calling LLM..." line. Let it run.]**

> "Stage two sends the profiling data — those exact millisecond numbers —
> together with the source files to the LLM.
> Not raw code. A ranked, structured analysis request:
> 'Given that app processing is 332ms, find the root cause.'"

**[When the bottleneck output appears — point at rank #1:]**

> "There it is. Rank one, critical severity.
> File: `CatalogController.php`, lines eight through twelve.
> Category: N+1 query.
> Estimated saving: **240 milliseconds**."

> "And it's already generated the fix. Before and after.
> Eager loading with `Product::with(['category', 'seller'])->get()`.
> One line replaces seven."

**[Scroll to show the before/after diff in the terminal output]**

> "This is the part that used to take four to eight hours of manual work.
> It just took eleven seconds."

---

## 2d — Stage 3: Apply + PR  `[3:45 – 4:30]`

**[SCREEN: Run again with Stage 3 enabled, or show output from a pre-run:]**

```bash
python run.py \
  --url https://your-api.indiamart.com/v2/catalog \
  --source ./catalog-service \
  --push
```

> "Stage three applies the fix directly to the source file,
> commits it on a new branch — `api-optimizer/php-20260515` —
> and generates the PR description."

**[Show the git log in Pane 2:]**

```bash
git log --oneline
# eba2cb7  perf: optimize php API — 63% latency reduction
# 4e39dc9  initial commit
```

**[Switch to browser tab with the rendered PR description]**

> "This is the PR that goes to the reviewer.
> Impact table. Collapsible code diffs. Test checklist.
> Everything a reviewer needs to say yes."

> "The developer's job is now: read this, run the tests, merge."

---

---

# BEAT 3 — SOLUTION  `[4:30 – 6:30]`
*Rubric axis: Completeness. Goal: show the full scope — not just the N+1 case.*

---

> "Let me show you the three scenarios the tool handles."

---

## Scenario A — PHP Optimisation  `[4:30 – 5:10]`

**[SCREEN: Pane 2 — show the language detection output for a pure PHP repo]**

> "Scenario A: pure PHP service. The detector reads `composer.json`,
> finds Laravel, checks headers for `X-Powered-By: PHP`.
> High confidence."

> "The analysis uses a PHP-specific playbook:
> N+1 queries, missing eager loads, synchronous curl chains,
> OPcache misses, session-lock blocking."

> "Any of these patterns in the source — it finds them,
> ranks them by estimated latency saving, fixes the top ones."

---

## Scenario B — Go Optimisation  `[5:10 – 5:45]`

**[SCREEN: show a snippet of Go code — or a quick `detect_language.py` run on a Go repo]**

```bash
python scripts/detect_language.py --source ./buyer-service
# Language: 🐹 GO  |  Confidence: HIGH
# • go.mod found  →  module indiamart.com/buyer-service
# • Gin framework detected
# • 94 .go files found
```

> "Scenario B: Go service. Different playbook entirely.
> Now it looks for goroutine leaks, `sync.Mutex` locks held across I/O,
> sequential `http.Get` calls that should be running in parallel with `errgroup`,
> missing `context.Context` propagation."

> "Each language has different failure modes.
> The tool knows both."

---

## Scenario C — PHP → Go Migration  `[5:45 – 6:30]`

**[SCREEN: run detect_language on a repo with both PHP and Go files]**

```bash
python scripts/detect_language.py --source ./mixed-service
# 🔄 PHP → GO MIGRATION DETECTED
# PHP dominant: 62 .php files  |  Go: 18 .go files
# MIGRATION IN PROGRESS — early stage
```

> "Scenario C: a repo mid-migration — both PHP and Go files present.
> The tool detects it, switches to the migration playbook."

> "It generates the Go equivalent of the critical-path PHP handler —
> with errgroup parallelism, context propagation, typed error handling —
> not a mechanical translation, but idiomatic Go."

> "What normally takes two to five dev-days per endpoint
> becomes a two-hour review-and-merge."

---

---

# BEAT 4 — IMPACT  `[6:30 – 7:30]`
*Rubric axis: Reach. Goal: concrete numbers, systemic category.*

---

**[SCREEN: clean terminal or simple text slide. Type these numbers as you say them.]**

> "Let me close with what this means at scale."

```
IndiaMart backend engineers:     500+
Active API endpoints:         10,000+
```

> "Even at ten percent adoption —
> fifty engineers each saving three hours a week."

```
50 engineers × 3 hrs/week  =  150 engineering hours recovered weekly
150 hrs/week × 52 weeks    =  7,800 engineering hours per year
```

> "That's roughly four full-time engineers worth of capacity,
> redirected from firefighting to feature work."

**[PAUSE. Then:]**

> "But the more direct number is the latency one."

```
Catalog API:    820ms p95  →  310ms p95   (−63%)
Buyer API:      640ms p95  →  190ms p95   (−70%)
Search API:    1100ms p95  →  380ms p95   (−65%)
```

> "Every hundred milliseconds we shave off a buyer-facing API
> is a measurable lift in search-to-enquiry conversion.
> These aren't theoretical savings — they're profiled measurements
> from the tool's own Stage 1 output."

**[Final beat — look at camera / speak directly:]**

> "The tool doesn't replace the engineer.
> It removes the four hours of detective work before the engineer can start.
> Profile, diagnose, fix, PR — one command, under three minutes."

> "Thank you."

---

---

## AFTER THE RECORDING — CHECKLIST

- [ ] Trim dead silence at the start/end  
- [ ] Confirm all terminal text is legible at 720p  
- [ ] The PR description tab should be readable — zoom browser to 110%  
- [ ] Add chapter markers if uploading to YouTube:
  - `0:00` Problem
  - `1:30` Live demo — profiling
  - `2:45` Live demo — AI analysis
  - `3:45` Live demo — PR generation
  - `4:30` Solution scope (3 scenarios)
  - `6:30` Impact & numbers

---

## TIMING GUARD

| Beat | Target | Hard limit |
|------|--------|-----------|
| 1 — Problem | 1:30 | 2:00 |
| 2 — Proof | 3:00 | 3:30 |
| 3 — Solution | 2:00 | 2:30 |
| 4 — Impact | 1:00 | 1:30 |
| **Total** | **7:30** | **9:30** |

If you're running long, **cut from Beat 3** — drop Scenario B (Go optimisation) and go straight from A to C. Beats 1, 2, and 4 are load-bearing for the rubric; Beat 3 is additive.
