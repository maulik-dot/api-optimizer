#!/usr/bin/env python3
"""
Language detector: determines if an API backend is PHP, Go, or a PHP→Go migration.
Works from two signal sources: HTTP response headers (live URL) and source directory.
"""

import os
import sys
import glob
import subprocess
import json
import argparse
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DetectionResult:
    language: str           # "php" | "go" | "php-to-go-migration" | "unknown"
    confidence: str         # "high" | "medium" | "low"
    signals: list[str]      # human-readable evidence
    php_files: int = 0
    go_files: int = 0
    headers: dict = field(default_factory=dict)


def detect_from_headers(url: str) -> DetectionResult:
    """Fetch HTTP headers and score PHP vs Go signals."""
    try:
        result = subprocess.run(
            ["curl", "-sI", "--max-time", "10", "--location", url],
            capture_output=True, text=True, timeout=15
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return DetectionResult("unknown", "low", [f"curl failed: {e}"])

    raw = result.stdout
    headers = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("HTTP/"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

    php_score = 0
    go_score = 0
    signals = []

    # --- PHP signals ---
    powered_by = headers.get("x-powered-by", "")
    if "php" in powered_by.lower():
        php_score += 10
        signals.append(f"X-Powered-By: {powered_by}")

    if "phpsessid" in headers.get("set-cookie", "").lower():
        php_score += 5
        signals.append("Session cookie: PHPSESSID (PHP session)")

    server = headers.get("server", "")
    if "apache" in server.lower():
        php_score += 3
        signals.append(f"Server: {server} (common PHP host)")
    if "litespeed" in server.lower():
        php_score += 3
        signals.append(f"Server: {server} (common PHP host)")

    # Laravel/Symfony/CodeIgniter add these
    if "laravel_session" in headers.get("set-cookie", "").lower():
        php_score += 8
        signals.append("Cookie: laravel_session (Laravel framework)")
    if headers.get("x-laravel-version"):
        php_score += 10
        signals.append(f"X-Laravel-Version: {headers['x-laravel-version']}")

    # --- Go signals ---
    # Go's net/http default server header is absent unless set explicitly
    if server and "apache" not in server.lower() and "nginx" not in server.lower() \
            and "litespeed" not in server.lower() and "iis" not in server.lower():
        go_score += 4
        signals.append(f"Server: {server} (custom — common in Go services)")

    # Gin sets X-Content-Type-Options by default; so does Go's common middleware
    if headers.get("x-content-type-options") and not powered_by:
        go_score += 2
        signals.append("X-Content-Type-Options present without PHP header (Go middleware pattern)")

    # Go services often expose /metrics or /healthz
    if not powered_by and not server:
        go_score += 2
        signals.append("No Server or X-Powered-By header (Go default)")

    # Determine result
    if php_score == 0 and go_score == 0:
        return DetectionResult("unknown", "low", ["No identifying headers found"], headers=headers)

    if php_score > go_score:
        confidence = "high" if php_score >= 8 else "medium"
        return DetectionResult("php", confidence, signals, headers=headers)
    else:
        confidence = "high" if go_score >= 6 else "medium"
        return DetectionResult("go", confidence, signals, headers=headers)


def detect_from_source(path: str) -> DetectionResult:
    """Walk source directory and score language from file structure."""
    if not os.path.isdir(path):
        return DetectionResult("unknown", "low", [f"Path not found: {path}"])

    php_files = glob.glob(f"{path}/**/*.php", recursive=True)
    go_files  = glob.glob(f"{path}/**/*.go",  recursive=True)

    has_composer  = os.path.exists(os.path.join(path, "composer.json"))
    has_gomod     = os.path.exists(os.path.join(path, "go.mod"))
    has_packagejson = os.path.exists(os.path.join(path, "package.json"))

    signals = []
    php_score = 0
    go_score  = 0

    if has_gomod:
        go_score += 10
        signals.append("go.mod found (Go module)")
        # read module name
        try:
            with open(os.path.join(path, "go.mod")) as f:
                first_line = f.readline().strip()
            signals.append(f"  └─ {first_line}")
        except Exception:
            pass

    if has_composer:
        php_score += 10
        signals.append("composer.json found (PHP project)")
        try:
            with open(os.path.join(path, "composer.json")) as f:
                comp = json.load(f)
            if "require" in comp:
                if "laravel/framework" in comp["require"]:
                    php_score += 5
                    signals.append("  └─ Laravel framework detected")
                if "symfony/symfony" in comp["require"] or "symfony/http-kernel" in comp["require"]:
                    php_score += 5
                    signals.append("  └─ Symfony framework detected")
        except Exception:
            pass

    if go_files:
        go_score += min(len(go_files), 8)   # cap contribution
        signals.append(f"{len(go_files)} .go files found")

        # Check for Go web frameworks
        for gf in go_files[:20]:   # sample first 20
            try:
                content = open(gf).read()
                if '"github.com/gin-gonic/gin"' in content:
                    go_score += 3
                    signals.append("  └─ Gin framework detected")
                    break
                if '"github.com/labstack/echo"' in content:
                    go_score += 3
                    signals.append("  └─ Echo framework detected")
                    break
                if '"github.com/gofiber/fiber"' in content:
                    go_score += 3
                    signals.append("  └─ Fiber framework detected")
                    break
            except Exception:
                continue

    if php_files:
        php_score += min(len(php_files), 8)
        signals.append(f"{len(php_files)} .php files found")

    # Both present → migration
    if php_files and go_files:
        if go_score > php_score:
            language = "php-to-go-migration"
            confidence = "high"
            signals.insert(0, "MIGRATION DETECTED: Both PHP and Go source present")
            signals.append(f"  Go is dominant ({len(go_files)} go vs {len(php_files)} php files)")
        elif php_score > go_score:
            language = "php-to-go-migration"
            confidence = "high"
            signals.insert(0, "MIGRATION DETECTED: Both PHP and Go source present")
            signals.append(f"  PHP is dominant ({len(php_files)} php vs {len(go_files)} go files)")
        else:
            language = "php-to-go-migration"
            confidence = "medium"
            signals.insert(0, "MIGRATION DETECTED: Equal PHP and Go source — early-stage migration")
        return DetectionResult(language, confidence, signals,
                               php_files=len(php_files), go_files=len(go_files))

    if php_score > go_score:
        confidence = "high" if php_score >= 10 else "medium"
        return DetectionResult("php", confidence, signals,
                               php_files=len(php_files))

    if go_score > php_score:
        confidence = "high" if go_score >= 10 else "medium"
        return DetectionResult("go", confidence, signals,
                               go_files=len(go_files))

    return DetectionResult("unknown", "low", signals or ["No PHP or Go files found"])


def detect(url: Optional[str] = None, source_path: Optional[str] = None) -> DetectionResult:
    """
    Run detection from available signals. If both are provided, merge results —
    source is more authoritative than headers.
    """
    header_result = None
    source_result = None

    if url:
        header_result = detect_from_headers(url)

    if source_path:
        source_result = detect_from_source(source_path)

    # Source is ground truth; headers add evidence
    if source_result and header_result:
        merged_signals = ["[from source] " + s for s in source_result.signals] + \
                         ["[from headers] " + s for s in header_result.signals]

        # Migration flag from source overrides header opinion
        if source_result.language == "php-to-go-migration":
            return DetectionResult("php-to-go-migration", "high", merged_signals,
                                   php_files=source_result.php_files,
                                   go_files=source_result.go_files)

        # If source and headers agree, boost confidence
        if source_result.language == header_result.language:
            return DetectionResult(source_result.language, "high", merged_signals,
                                   php_files=source_result.php_files,
                                   go_files=source_result.go_files)

        # Disagree → trust source
        return DetectionResult(source_result.language, "medium",
                               merged_signals + ["[warning] headers suggest different language — trusting source"],
                               php_files=source_result.php_files,
                               go_files=source_result.go_files)

    return source_result or header_result or \
           DetectionResult("unknown", "low", ["No URL or source path provided"])


def print_result(r: DetectionResult):
    icons = {"php": "🐘", "go": "🐹", "php-to-go-migration": "🔄", "unknown": "❓"}
    conf_colors = {"high": "\033[92m", "medium": "\033[93m", "low": "\033[91m"}
    reset = "\033[0m"

    icon = icons.get(r.language, "❓")
    color = conf_colors.get(r.confidence, "")

    print(f"\n{'─'*50}")
    print(f"  Language : {icon}  {r.language.upper()}")
    print(f"  Confidence: {color}{r.confidence.upper()}{reset}")
    if r.php_files or r.go_files:
        print(f"  Files    : {r.php_files} PHP  |  {r.go_files} Go")
    print(f"  Signals  :")
    for s in r.signals:
        print(f"    • {s}")
    print(f"{'─'*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect API backend language (PHP / Go / migration)")
    parser.add_argument("--url",    "-u", help="Live API endpoint URL")
    parser.add_argument("--source", "-s", help="Path to source code directory")
    args = parser.parse_args()

    if not args.url and not args.source:
        parser.error("Provide at least one of --url or --source")

    result = detect(url=args.url, source_path=args.source)
    print_result(result)

    # Machine-readable exit codes: 0=go, 1=php, 2=migration, 3=unknown
    sys.exit({"go": 0, "php": 1, "php-to-go-migration": 2}.get(result.language, 3))
