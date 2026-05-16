---
name: optimize-api
description: >
  Diagnose and fix API performance bottlenecks in Go or PHP source code. Given profiling
  data (p95 latency, per-layer breakdown) and a source repo, trace the hot request path,
  identify what is slow, and report specific code fixes with exact before/after diffs.
  Use when app_ms, dns_ms, or body_ms is elevated, or when the user reports a slow endpoint.
compatibility: Go or PHP source repo. Requires profiling data (p95, layer breakdown).
metadata:
  author: indiamartplatform
  version: "2.1"
---

# Optimize API — Reasoning Playbook

## 1. Read the profiling data first — it tells you where to look

| Layer elevated | Where to focus |
|---|---|
| `app_ms` high | Handler, service layer, DB calls, cache calls |
| `dns_ms` high | Outbound HTTP clients — missing `DialContext` timeout or no connection reuse |
| `body_ms` high | Response serialization — payload too large or marshal called multiple times |
| p99 >> p95 | Lock contention or GC pauses — look for `sync.Mutex` around I/O |

Never start reading code until you know which layer is hot.

---

## 2. Trace the request path top-down

**Go repos — follow this order exactly:**

1. Call `list_go_packages` — understand the module layout before touching any file.
2. Call `find_route` with the URL path segment — jumps directly to the handler registration.
3. `read_file` the handler. Trace every function call down to DB / cache / HTTP.
4. `search_symbol` to find service or repository implementations referenced in the handler.
5. Stop when you reach the DB driver, Redis client, or outbound HTTP call — those are the leaves.

**PHP repos:**

1. `list_directory` from the root to find `app/Http/Controllers` or similar.
2. `read_file` the controller for the route. Trace into services, repositories, models.
3. `search_symbol` to find Eloquent scopes, query builders, or middleware.

**Skip these always** (no bottlenecks live here):
- `vendor/`, `node_modules/`, `.git/`
- `*_test.go`, `*_mock.go`, `*.pb.go`, `*_gen.go`, `wire_gen.go`
- `migrations/`, `seeders/`, `lang/`, `i18n/`
- Config files, Dockerfile, CI YAML

---

## 3. Look for these patterns — in priority order

### Go patterns

| Priority | Category | What it looks like in code |
|---|---|---|
| 1 | `serial_io` | `result1 := db.Query(...)` then `result2 := redis.Get(...)` — sequential with no data dependency between them |
| 2 | `context_propagation` | `db.QueryContext(context.Background(), ...)` or `db.Query(...)` inside a handler that has `ctx` in scope |
| 3 | `http_client_reuse` | `client := &http.Client{}` inside a handler function, or `http.Get(url)` (uses the global default client) |
| 4 | `mutex_contention` | `mu.Lock()` immediately followed by a DB query, Redis call, or `http.Do()` inside the locked region |
| 5 | `n_plus_one_query` | `db.Query(...)` or ORM call inside a `for range` loop |
| 6 | `missing_cache` | Same DB/HTTP query on every request, result does not vary per user, no `redis.Get` before it |
| 7 | `connection_pool_sizing` | `sql.Open(...)` with no `db.SetMaxOpenConns` / `db.SetMaxIdleConns`, or Redis `redis.NewClient` with default `PoolSize` |
| 8 | `goroutine_leak` | `go func() { ... }()` with no `WaitGroup` or `errgroup` and no way for the caller to know it completed |
| 9 | `response_serialization` | `json.Marshal` called twice on the same struct, or `json.Marshal` on a very wide struct when only 3 fields are needed |
| 10 | `memory_alloc` | String concatenation with `+` in a loop, or `append(slice, items...)` inside a hot loop without a pre-allocated capacity |

### PHP patterns

| Priority | Category | What it looks like in code |
|---|---|---|
| 1 | `serial_io` | Sequential Guzzle calls with no data dependency; `$client->get(url1)` then `$client->get(url2)` |
| 2 | `n_plus_one_query` | Eloquent relationship accessed in a `foreach` without `->with(...)` eager load |
| 3 | `missing_cache` | `DB::table(...)->get()` with no `Cache::remember(...)` wrapper; same query on every request |
| 4 | `sync_to_async` | Email, push notification, audit log, or counter incremented synchronously inside the request handler |
| 5 | `mutex_contention` | Laravel `Cache::lock()` or file lock held across a DB query |
| 6 | `memory_alloc` | `implode` or `.=` string build in a loop over a large collection |

---

## 4. Code examples — what to look for and how to fix it

### `serial_io` → `errgroup` (Go)

```go
// BEFORE — serial: total time = t1 + t2
products, _ := repo.GetProducts(ctx, ids)
inventory, _ := inventorySvc.GetStock(ctx, ids)

// AFTER — parallel: total time = max(t1, t2)
g, gctx := errgroup.WithContext(ctx)
var products []Product
var inventory []Stock
g.Go(func() error { products, err = repo.GetProducts(gctx, ids); return err })
g.Go(func() error { inventory, err = inventorySvc.GetStock(gctx, ids); return err })
if err := g.Wait(); err != nil { ... }
```

### `context_propagation` (Go)

```go
// BEFORE — ignores request deadline; query runs forever if client disconnects
rows, err := db.Query("SELECT * FROM products WHERE id = ?", id)

// AFTER — tied to request lifecycle
rows, err := db.QueryContext(ctx, "SELECT * FROM products WHERE id = ?", id)
```

### `http_client_reuse` (Go)

```go
// BEFORE — creates a new TCP connection on every request; no keepalive
func (h *Handler) GetExternal(c *gin.Context) {
    client := &http.Client{Timeout: 5 * time.Second}
    resp, _ := client.Get(externalURL)
    ...
}

// AFTER — package-level client reused across requests
var httpClient = &http.Client{
    Timeout:   5 * time.Second,
    Transport: &http.Transport{MaxIdleConns: 100, IdleConnTimeout: 90 * time.Second},
}
```

### `connection_pool_sizing` (Go)

```go
// BEFORE — defaults: MaxOpenConns=0 (unlimited), MaxIdleConns=2 (starves under load)
db, _ := sql.Open("mysql", dsn)

// AFTER — sized for production QPS
db, _ := sql.Open("mysql", dsn)
db.SetMaxOpenConns(50)
db.SetMaxIdleConns(25)
db.SetConnMaxLifetime(5 * time.Minute)
```

### `mutex_contention` (Go)

```go
// BEFORE — lock held across a DB round-trip; serialises all requests
mu.Lock()
row := db.QueryRowContext(ctx, "SELECT count FROM stats WHERE id = ?", id)
row.Scan(&count)
mu.Unlock()

// AFTER — fetch without lock, then lock only for the write
row := db.QueryRowContext(ctx, "SELECT count FROM stats WHERE id = ?", id)
row.Scan(&count)
mu.Lock()
cache[id] = count
mu.Unlock()
```

---

## 5. Filling in `report_fix` fields

**`before` / `after`** — copy the exact lines from the file. Do not paraphrase. Max 8 lines each. The patch engine does a literal string search using `before`.

**`anchor`** — the function signature plus 2–3 lines of its opening body. Must be unique within the file. For Go, use the full receiver + method signature:

```
func (h *ProductHandler) List(c *gin.Context) {
	ids := c.QueryArray("id")
	ctx := c.Request.Context()
```

**`estimated_ms`** — use these rules:
- `serial_io`: saved = (sum of serial call times) − (slowest single call). If you see two 50ms DB calls in sequence, saved ≈ 50ms.
- `context_propagation`: 0ms saved for correctness; 5–10ms saved if it enables the caller to cancel early under load.
- `http_client_reuse`: 20–80ms saved on the first request per connection (TCP + TLS handshake avoided). Set to 30ms as a conservative default.
- `connection_pool_sizing`: 10–50ms saved under load (avoids queue wait for a free connection). Default 20ms unless QPS data suggests more.
- `mutex_contention`: 10–100ms saved depending on lock hold time and concurrency. Estimate = (avg DB time) × (avg concurrent requests − 1).
- `n_plus_one_query`: saved = (N − 1) × avg single-query time.
- `missing_cache`: saved = avg query time × expected cache hit rate (default 80%).
- `memory_alloc` / `response_serialization`: 5–15ms unless payload is very large.

**`severity`** — `critical` >100ms, `high` 50–100ms, `medium` 10–50ms, `low` <10ms.

---

## 6. Rules — do not break these

- Do not `report_fix` for a file you have not read. Every fix must be grounded in code you saw.
- Do not guess `estimated_ms`. If you cannot estimate, use 10ms and severity `low`.
- `before` must exactly match the file. Even a single extra space breaks the patch.
- If you find an issue but the fix requires more than 8 lines, report the `before` as the smallest uniquely-matchable block and the `after` as the minimal change that fixes the root cause.
- Call `finish_analysis` only after you have traced the full request path.
