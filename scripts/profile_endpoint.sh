#!/usr/bin/env bash
# profile_endpoint.sh — curl-based latency profiler
# Usage: ./profile_endpoint.sh <URL> [runs=20] [method=GET] [data='{}'] [headers='H1: v1|H2: v2']
#
# Output: per-layer timing breakdown (DNS / TCP / TLS / TTFB / Total) with p50/p95/p99

set -euo pipefail

URL="${1:?Usage: $0 <URL> [runs] [method] [data] [headers]}"
RUNS="${2:-20}"
METHOD="${3:-GET}"
DATA="${4:-}"
EXTRA_HEADERS="${5:-}"   # pipe-separated: 'Authorization: Bearer xyz|Content-Type: application/json'

# curl write-out format — all timings in seconds, will convert to ms in Python
CURL_FORMAT='%{time_namelookup}|%{time_connect}|%{time_appconnect}|%{time_pretransfer}|%{time_starttransfer}|%{time_total}|%{http_code}|%{size_download}'

echo ""
echo "  Profiling: $URL"
echo "  Method   : $METHOD"
echo "  Runs     : $RUNS"
echo "  ─────────────────────────────────────────────"

# Build curl command args
CURL_ARGS=(-o /dev/null -s --max-time 30 -w "$CURL_FORMAT" -X "$METHOD")

# Add data for POST/PUT
if [[ -n "$DATA" ]]; then
    CURL_ARGS+=(-d "$DATA")
fi

# Add extra headers
if [[ -n "$EXTRA_HEADERS" ]]; then
    IFS='|' read -ra HEADERS <<< "$EXTRA_HEADERS"
    for h in "${HEADERS[@]}"; do
        CURL_ARGS+=(-H "$h")
    done
fi

# First hit — warm-up, discarded
curl "${CURL_ARGS[@]}" "$URL" > /dev/null 2>&1 || true

# Collect measurements
RESULTS=()
ERRORS=0
for i in $(seq 1 "$RUNS"); do
    raw=$(curl "${CURL_ARGS[@]}" "$URL" 2>/dev/null) || { ((ERRORS++)); continue; }
    http_code=$(echo "$raw" | cut -d'|' -f7)
    if [[ "$http_code" -ge 200 ]] && [[ "$http_code" -lt 600 ]]; then
        RESULTS+=("$raw")
        printf "  run %2d: %s ms  [HTTP %s]\n" "$i" \
            "$(echo "$raw" | awk -F'|' '{printf "%.0f", $6 * 1000}')" "$http_code"
    else
        ((ERRORS++))
        printf "  run %2d: failed (HTTP %s)\n" "$i" "$http_code"
    fi
done

if [[ ${#RESULTS[@]} -eq 0 ]]; then
    echo "  ERROR: All $RUNS requests failed."
    exit 1
fi

# Pass raw data to Python for percentile calculation
JOINED=$(printf '%s\n' "${RESULTS[@]}")

python3 - "$JOINED" <<'PYEOF'
import sys, statistics

raw = sys.argv[1]
rows = [r.strip() for r in raw.strip().split('\n') if r.strip()]
if not rows:
    print("No data to analyze")
    sys.exit(1)

parsed = []
for r in rows:
    parts = r.split('|')
    if len(parts) < 8:
        continue
    try:
        parsed.append({
            "dns":   float(parts[0]) * 1000,
            "tcp":   (float(parts[1]) - float(parts[0])) * 1000,
            "tls":   (float(parts[2]) - float(parts[1])) * 1000,
            "app":   (float(parts[4]) - float(parts[3])) * 1000,   # TTFB minus pretransfer = app processing
            "ttfb":  float(parts[4]) * 1000,
            "total": float(parts[5]) * 1000,
            "code":  int(parts[6]),
            "bytes": int(float(parts[7])),
        })
    except (ValueError, IndexError):
        continue

if not parsed:
    print("Could not parse results")
    sys.exit(1)

totals = sorted(d["total"] for d in parsed)
ttfbs  = sorted(d["ttfb"]  for d in parsed)
n = len(totals)

def pct(data, p):
    idx = max(0, min(int(len(data) * p / 100), len(data) - 1))
    return data[idx]

avg_dns  = statistics.mean(d["dns"]  for d in parsed)
avg_tcp  = statistics.mean(d["tcp"]  for d in parsed)
avg_tls  = statistics.mean(d["tls"]  for d in parsed)
avg_app  = statistics.mean(d["app"]  for d in parsed)
avg_body = statistics.mean(d["total"] - d["ttfb"] for d in parsed)

codes = {}
for d in parsed:
    codes[d["code"]] = codes.get(d["code"], 0) + 1

avg_bytes = statistics.mean(d["bytes"] for d in parsed)

print()
print("  ═══════════════════════════════════════════")
print("  LATENCY SUMMARY (ms)")
print("  ───────────────────────────────────────────")
print(f"  p50   : {pct(totals, 50):>8.1f} ms")
print(f"  p75   : {pct(totals, 75):>8.1f} ms")
print(f"  p95   : {pct(totals, 95):>8.1f} ms")
print(f"  p99   : {pct(totals, 99):>8.1f} ms")
print(f"  mean  : {statistics.mean(totals):>8.1f} ms")
print(f"  min   : {totals[0]:>8.1f} ms")
print(f"  max   : {totals[-1]:>8.1f} ms")
print()
print("  PER-LAYER BREAKDOWN (mean)")
print("  ───────────────────────────────────────────")
print(f"  DNS resolution : {avg_dns:>6.1f} ms")
print(f"  TCP handshake  : {avg_tcp:>6.1f} ms")
print(f"  TLS handshake  : {avg_tls:>6.1f} ms")
print(f"  App processing : {avg_app:>6.1f} ms  ← bottleneck target")
print(f"  Body download  : {avg_body:>6.1f} ms")
print()
print("  RESPONSE INFO")
print("  ───────────────────────────────────────────")
print(f"  HTTP codes  : { {k:v for k,v in sorted(codes.items())} }")
print(f"  Avg size    : {avg_bytes/1024:.1f} KB")
print(f"  Samples     : {n}")

# Bottleneck hint
total_mean = statistics.mean(totals)
if avg_app / total_mean > 0.6:
    print()
    print("  ⚠  App processing is >60% of total time — likely bottleneck in handler logic or DB queries")
elif avg_tls / total_mean > 0.3:
    print()
    print("  ⚠  TLS handshake is >30% of total time — consider HTTP/2 or connection keep-alive")
elif avg_dns / total_mean > 0.2:
    print()
    print("  ⚠  DNS resolution is >20% of total time — consider DNS caching or /etc/hosts for internal services")

print("  ═══════════════════════════════════════════")
print()
PYEOF
