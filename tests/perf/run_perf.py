"""End-to-end performance probe for the Kotak Neo dashboard.

What it does:
  - For every read-only endpoint (pages + JSON APIs), measure latency + body
    size across three scenarios:
      1. cold      -- 1 request after a 5s pause (cache likely stale)
      2. warm      -- 20 sequential requests back-to-back (cache should be hot)
      3. load      -- 30 requests across 5 parallel workers
  - Probe WebSocket staleness: poll /api/feed-status every 1 s for 30 s,
    track last_tick_age so we can see how fresh the WS cache really is.
  - Snapshot data-file sizes (read locally; on the VPS pass --vps to fetch
    these via ssh).
  - Write a markdown report with sortable tables to docs/PERF_REPORT.md.

Why this shape:
  - No new dependencies — just requests + stdlib. Run anywhere.
  - Report is markdown so it diffs cleanly against past runs in git.
  - Parallelism is capped at 5 to avoid hammering the production strategy
    ticker. We're measuring, not load-testing.

Usage:
  python tests/perf/run_perf.py                       # local, http://127.0.0.1:5000
  python tests/perf/run_perf.py --base http://185.197.249.70:5000  # VPS
  python tests/perf/run_perf.py --vps                 # auto-target VPS + ssh stats
  python tests/perf/run_perf.py --skip-load           # quick smoke (cold+warm only)
"""
import argparse
import concurrent.futures as cf
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime

import requests


# ---- endpoint catalog ----------------------------------------------------
# Read-only only. Mutating routes (place-order, config POST, STOP, refresh)
# are intentionally excluded — perf probes must never move money.
PAGES = [
    "/",
    "/positions",
    "/orders",
    "/trade-book",
    "/limits",
    "/history",
    "/gann",
    "/options",
    "/futures",
    "/trades",
    "/paper-trades",
    "/blockers",
    "/audit",
    "/config",
    "/orderlog",
]

API_ENDPOINTS = [
    "/api/health",
    "/api/feed-status",
    "/api/snapshot-stats",
    "/api/gann-prices",
    "/api/gann-live",
    "/api/option-prices",
    "/api/future-prices",
    "/api/trades",
    "/api/paper-trades-live",
    "/api/recent-blocks",
    "/api/blocked-list",
    "/api/config",
    "/api/margin-summary",
]

# Endpoints that grow with date — sample with the new pagination + filter so
# we measure realistic page-1 reads, not the full file dump that used to
# happen.
PAGINATED_ENDPOINTS = [
    "/api/blocked-list?page=1",
    "/blockers?page=1",
    "/audit?page=1",
]

DATA_FILES = [
    "data/blocked_attempts.jsonl",
    "data/audit.log",
    "data/paper_ledger.json",
    "data/orders.jsonl",
    "data/trades.jsonl",
]


# ---- measurement helpers -------------------------------------------------
def time_one(session, url, timeout=15):
    """Single request -> (status, latency_ms, body_bytes, error_str_or_None)."""
    t0 = time.perf_counter()
    try:
        r = session.get(url, timeout=timeout)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        return (r.status_code, dt_ms, len(r.content), None)
    except Exception as e:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        return (0, dt_ms, 0, f"{type(e).__name__}: {e}")


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def summarize(samples):
    """samples: list[(status, lat_ms, bytes, err)] -> stats dict."""
    ok = [s for s in samples if 200 <= s[0] < 400]
    errs = [s for s in samples if not (200 <= s[0] < 400)]
    lat = [s[1] for s in ok]
    sizes = [s[2] for s in ok]
    return {
        "n": len(samples),
        "ok": len(ok),
        "err": len(errs),
        "err_rate_pct": round(100.0 * len(errs) / max(1, len(samples)), 1),
        "p50_ms": round(percentile(lat, 50), 1) if lat else 0.0,
        "p95_ms": round(percentile(lat, 95), 1) if lat else 0.0,
        "p99_ms": round(percentile(lat, 99), 1) if lat else 0.0,
        "max_ms": round(max(lat), 1) if lat else 0.0,
        "mean_ms": round(statistics.mean(lat), 1) if lat else 0.0,
        "body_bytes_p50": int(percentile(sizes, 50)) if sizes else 0,
        "body_bytes_max": max(sizes) if sizes else 0,
        "first_err": next((s[3] for s in errs if s[3]), None),
    }


# ---- scenarios -----------------------------------------------------------
def scenario_cold(session, base, paths):
    out = {}
    for p in paths:
        time.sleep(2.0)  # let caches age
        out[p] = summarize([time_one(session, base + p)])
    return out


def scenario_warm(session, base, paths, n=20):
    out = {}
    for p in paths:
        samples = [time_one(session, base + p) for _ in range(n)]
        out[p] = summarize(samples)
    return out


def scenario_load(base, paths, total=30, workers=5):
    """Hit each path `total` times across `workers` parallel threads."""
    out = {}
    for p in paths:
        url = base + p
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            # Each worker uses its own session — mimics distinct browser tabs.
            def _do():
                with requests.Session() as s:
                    return time_one(s, url)
            samples = list(ex.map(lambda _: _do(), range(total)))
        out[p] = summarize(samples)
    return out


def probe_ws_freshness(session, base, seconds=30):
    """Poll /api/feed-status every 1s; record last_tick_age.

    last_tick_age = seconds since the WS got its most recent tick.
    Out-of-hours this will climb forever (market closed). In-hours it
    should stay <2s. We surface both raw samples and a small histogram so
    the report shows reality, not just averages.
    """
    samples = []
    for _ in range(seconds):
        t0 = time.perf_counter()
        try:
            r = session.get(base + "/api/feed-status", timeout=5)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            data = r.json() if r.status_code == 200 else {}
            samples.append({
                "ts": time.time(),
                "lat_ms": round(dt_ms, 1),
                "connected": bool(data.get("connected")),
                "last_tick_age": data.get("last_tick_age"),
                "subs_index":  data.get("subs_index"),
                "subs_scrip":  data.get("subs_scrip"),
                "subs_option": data.get("subs_option"),
                "subs_future": data.get("subs_future"),
                "reconnects":  data.get("reconnects"),
                "errors":      len(data.get("errors", []) or []),
            })
        except Exception as e:
            samples.append({"ts": time.time(), "error": str(e)})
        time.sleep(1.0)
    ages = [s.get("last_tick_age") for s in samples
            if isinstance(s.get("last_tick_age"), (int, float))]
    return {
        "samples": samples,
        "n": len(samples),
        "connected_pct": round(100.0 * sum(1 for s in samples
                                           if s.get("connected"))
                               / max(1, len(samples)), 1),
        "age_p50": round(percentile(ages, 50), 2) if ages else None,
        "age_p95": round(percentile(ages, 95), 2) if ages else None,
        "age_max": round(max(ages), 2) if ages else None,
        # Sub-second freshness — what we actually care about for SL_TRAIL.
        "fresh_under_1s_pct": round(100.0 * sum(1 for a in ages if a < 1.0)
                                    / max(1, len(ages)), 1) if ages else None,
        "fresh_under_2s_pct": round(100.0 * sum(1 for a in ages if a < 2.0)
                                    / max(1, len(ages)), 1) if ages else None,
    }


def data_file_sizes(repo_root, ssh_host=None):
    """Local file sizes; if ssh_host given, run `du -sh` on the VPS instead."""
    out = {}
    if ssh_host:
        try:
            cmd = ["ssh", ssh_host,
                   "for f in " + " ".join(DATA_FILES)
                   + "; do echo -n \"$f \"; "
                   + "(stat -c '%s %y' /home/kotak/kotak-dashboard/$f "
                   + "2>/dev/null || echo missing); done"]
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=10)
            for line in (res.stdout or "").splitlines():
                parts = line.strip().split(" ", 1)
                if len(parts) >= 2:
                    out[parts[0]] = parts[1]
        except Exception as e:
            out["_error"] = f"ssh stat failed: {e}"
        return out
    for f in DATA_FILES:
        full = os.path.join(repo_root, f)
        try:
            st = os.stat(full)
            out[f] = {"bytes": st.st_size,
                      "kb": round(st.st_size / 1024, 1),
                      "mb": round(st.st_size / 1024 / 1024, 2),
                      "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}
        except FileNotFoundError:
            out[f] = "missing"
        except Exception as e:
            out[f] = f"err: {e}"
    return out


# ---- report writer -------------------------------------------------------
def write_report(report, out_path):
    lines = []
    push = lines.append
    push("# Perf Report")
    push("")
    push(f"Generated: {report['generated_at']}")
    push(f"Base URL:  `{report['base']}`")
    push(f"Scenarios: {', '.join(report['scenarios_run'])}")
    push("")
    push("## Summary")
    push("")
    # Worst offenders by p95
    all_warm = report.get("warm", {})
    rows = sorted(all_warm.items(), key=lambda kv: kv[1].get("p95_ms", 0),
                  reverse=True)[:10]
    push("### Top 10 slowest endpoints (warm p95)")
    push("")
    push("| Endpoint | p50 ms | p95 ms | p99 ms | max ms | body p50 | err % |")
    push("|---|---:|---:|---:|---:|---:|---:|")
    for path, st in rows:
        push(f"| `{path}` | {st['p50_ms']} | {st['p95_ms']} | "
             f"{st['p99_ms']} | {st['max_ms']} | "
             f"{st['body_bytes_p50']:,} | {st['err_rate_pct']} |")
    push("")
    # WS freshness summary
    ws = report.get("ws", {})
    if ws:
        push("### WebSocket freshness")
        push("")
        push(f"- Connected: **{ws.get('connected_pct')}%** of samples")
        push(f"- last_tick_age p50: **{ws.get('age_p50')} s** "
             f"(p95 {ws.get('age_p95')}, max {ws.get('age_max')})")
        push(f"- Sub-1s freshness: **{ws.get('fresh_under_1s_pct')}%** of samples")
        push(f"- Sub-2s freshness: **{ws.get('fresh_under_2s_pct')}%** of samples")
        push("")
    # Data files
    df = report.get("data_files", {})
    if df:
        push("### Data file sizes")
        push("")
        for f, info in df.items():
            push(f"- `{f}` -> {info}")
        push("")

    # Detailed per-scenario tables
    for sc in ["cold", "warm", "load"]:
        block = report.get(sc, {})
        if not block:
            continue
        push(f"## Scenario: {sc}")
        push("")
        push("| Endpoint | n | ok | err% | p50 | p95 | p99 | max | body p50 | err |")
        push("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for path, st in sorted(block.items()):
            err_str = (st.get("first_err") or "")[:60]
            push(f"| `{path}` | {st['n']} | {st['ok']} | {st['err_rate_pct']} "
                 f"| {st['p50_ms']} | {st['p95_ms']} | {st['p99_ms']} "
                 f"| {st['max_ms']} | {st['body_bytes_p50']:,} | {err_str} |")
        push("")

    push("## Raw WebSocket samples (last 30s)")
    push("")
    push("```json")
    push(json.dumps(report.get("ws", {}).get("samples", [])[-30:],
                    indent=2, default=str))
    push("```")
    push("")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[perf] wrote report -> {out_path}")


# ---- main ---------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5000",
                    help="base URL to probe")
    ap.add_argument("--vps", action="store_true",
                    help="shortcut: target the VPS at 185.197.249.70:5000 "
                         "and pull data-file stats over ssh")
    ap.add_argument("--ssh-host", default="root@185.197.249.70",
                    help="ssh target for data-file stats")
    ap.add_argument("--ws-seconds", type=int, default=30,
                    help="WebSocket freshness probe duration")
    ap.add_argument("--skip-load", action="store_true",
                    help="skip the parallel load scenario")
    ap.add_argument("--skip-cold", action="store_true",
                    help="skip the cold scenario (faster reruns)")
    ap.add_argument("--out", default=None,
                    help="report output path; defaults to docs/PERF_REPORT.md "
                         "(or docs/PERF_REPORT_VPS.md when --vps)")
    args = ap.parse_args()

    if args.vps:
        args.base = "http://185.197.249.70:5000"
        out_path = args.out or "docs/PERF_REPORT_VPS.md"
        ssh_host = args.ssh_host
    else:
        out_path = args.out or "docs/PERF_REPORT.md"
        ssh_host = None

    # Resolve repo_root from this file's location -> .../tests/perf/run_perf.py
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    print(f"[perf] target={args.base}")
    paths = PAGES + API_ENDPOINTS + PAGINATED_ENDPOINTS
    print(f"[perf] {len(paths)} endpoints, "
          f"warm n=20, load 30 reqs x 5 workers")

    session = requests.Session()
    # Warmup so the first cold sample isn't stuck waiting for the Kotak login
    # cache to populate. We deliberately do NOT count this in any scenario.
    try:
        session.get(args.base + "/api/health", timeout=10)
    except Exception:
        pass

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base": args.base,
        "scenarios_run": [],
    }

    if not args.skip_cold:
        print("[perf] cold scenario...")
        report["cold"] = scenario_cold(session, args.base, paths)
        report["scenarios_run"].append("cold")

    print("[perf] warm scenario (20x each)...")
    report["warm"] = scenario_warm(session, args.base, paths, n=20)
    report["scenarios_run"].append("warm")

    if not args.skip_load:
        print("[perf] load scenario (30 reqs x 5 workers)...")
        report["load"] = scenario_load(args.base, paths, total=30, workers=5)
        report["scenarios_run"].append("load")

    print(f"[perf] WebSocket freshness probe ({args.ws_seconds}s)...")
    report["ws"] = probe_ws_freshness(session, args.base,
                                      seconds=args.ws_seconds)

    print("[perf] data file stats...")
    report["data_files"] = data_file_sizes(repo_root, ssh_host=ssh_host)

    write_report(report, os.path.join(repo_root, out_path))
    # Also dump raw json for diffing across runs.
    raw_path = os.path.join(repo_root,
                            out_path.replace(".md", ".json"))
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"[perf] raw json -> {raw_path}")
    print("[perf] done.")


if __name__ == "__main__":
    sys.exit(main() or 0)
