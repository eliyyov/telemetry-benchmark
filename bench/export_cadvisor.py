#!/usr/bin/env python3
"""Export cAdvisor series from Prometheus for every finished run window (run on VM-Mon).

Cross-check path (V5/V6 in Table 3.3): the cgroup sampler is the primary measurement,
cAdvisor via Prometheus is the independent second path. For each run in
raw_metrics/<arm>/<level>/runN/ this script queries Prometheus for that run's exact
measurement window (run.json "window") and writes cadvisor_samples.csv beside
cgroup_samples.csv. analyze.py then compares the two paths.

Identity: every cell of an arm reuses the same container NAME (compose
container_name: v-vector / o-otel), so a name query returns one series per historical
container and the first one may be a corpse that Prometheus still answers flat for its
5-minute staleness lookback. The orchestrator therefore records the container ID in
run.json, and this script queries by that ID first.

Usage: python3 export_cadvisor.py [--prom http://127.0.0.1:9090] [--root raw_metrics]
"""
import argparse
import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request

CONTAINER = {"vector": "v-vector", "otel": "o-otel"}
CPU_METRIC = "container_cpu_usage_seconds_total"
MEM_METRIC = "container_memory_usage_bytes"

# A window that Prometheus can only partly answer must not be written: a flat or
# truncated series would look like a valid measurement downstream and silently
# defeat the cross-check (V5/V6). Validate first, refuse loudly.
MIN_POINTS = 5        # 120 s window at a 5 s step should give ~24
MIN_DISTINCT = 3      # a cumulative CPU counter has to move
MIN_COVERAGE = 0.90   # the series must span >=90% of the run window


# ---------------------------------------------------------------- Prometheus
def query_json(prom, query, start, end, step=5):
    url = (f"{prom}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": query, "start": start, "end": end, "step": step}))
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r).get("data", {}).get("result", [])


def query_all(prom, metric, container, start, end):
    """Every series matching the container label, not just the first one."""
    for label in ("name", "container_name"):
        res = query_json(prom, f'{metric}{{{label}="{container}"}}', start, end)
        res = [s for s in res if s.get("values")]
        if res:
            return res
    return []


def query_by_id(prom, metric, ident, start, end):
    res = query_json(prom, f'{metric}{{id="{ident}"}}', start, end)
    return res[0]["values"] if res else []


# ---------------------------------------------------------------- series shape
def finite(values):
    """Drop NaN/Inf. NaN != NaN, so a broken series can otherwise look like it has a
    distinct value at every point and pass every check below."""
    out = []
    for t, v in values:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv):
            out.append((t, fv))
    return out


def pick_series(series, start, end):
    """The series that actually represents this window: most movement, then points."""
    def score(s):
        vals = [v for t, v in finite(s["values"]) if start <= float(t) <= end]
        return (len(set(vals)), len(vals))

    return max(series, key=score) if series else None


def cpu_problems(values, ws, we):
    """Why this CPU counter cannot serve as a cross-check ([] = usable)."""
    if not values:
        return ["empty series"]
    pts = finite(values)
    if len(pts) < MIN_POINTS:
        return [f"{len(pts)} finite points (of {len(values)})"]
    problems = []
    distinct = len({v for _, v in pts})
    if distinct < MIN_DISTINCT:
        problems.append(f"{distinct} distinct counter value")
    span = pts[-1][0] - pts[0][0]
    if (we - ws) > 0 and span / (we - ws) < MIN_COVERAGE:
        problems.append(f"covers {100 * span / (we - ws):.0f}% of the window")
    return problems


def coverage_problem(values, ws, we):
    """Memory is a gauge: a flat value is normal, so only length and span are judged."""
    pts = finite(values)
    if len(pts) < MIN_POINTS:
        return f"{len(pts)} finite points (of {len(values)})"
    span = pts[-1][0] - pts[0][0]
    if (we - ws) > 0 and span / (we - ws) < MIN_COVERAGE:
        return f"covers {100 * span / (we - ws):.0f}% of the window"
    return None


# ---------------------------------------------------------------- one run
def export_run(prom, d, rec, container):
    """Write cadvisor_samples.csv for one run window. Returns (ok, message)."""
    w = rec.get("window") or {}
    ws, we = w.get("start"), w.get("end")
    if not (ws and we):
        return False, "skip: no window in run.json"

    note = ""
    cid = (rec.get("container_id") or "").strip()
    cpu, used_id = None, None
    if cid:
        for form in (f"/docker/{cid}", f"/system.slice/docker-{cid}.scope"):
            vals = query_by_id(prom, CPU_METRIC, form, ws, we)
            if vals:
                cpu, used_id = vals, form
                break
        if cpu is None:
            note = f" [WARN no series for container_id {cid[:12]}…, fell back to name]"

    if cpu is None:
        s = pick_series(query_all(prom, CPU_METRIC, container, ws, we), ws, we)
        if s:
            cpu = s["values"]
            used_id = (s.get("metric") or {}).get("id")

    if not cpu:
        return False, "no cAdvisor CPU series"

    mem = query_by_id(prom, MEM_METRIC, used_id, ws, we) if used_id else []
    if not mem:
        series = query_all(prom, MEM_METRIC, container, ws, we)
        # never pair container A's CPU with container B's memory
        same = [s for s in series
                if used_id and (s.get("metric") or {}).get("id") == used_id]
        s = same[0] if same else pick_series(series, ws, we)
        mem = s["values"] if s else []
    if not mem:
        return False, "no cAdvisor memory series"

    problems = cpu_problems(cpu, ws, we)
    if problems:
        return False, f"CPU series unusable ({', '.join(problems)})"
    mp = coverage_problem(mem, ws, we)
    if mp:
        return False, f"memory series unusable ({mp})"

    mem_at = {t: v for t, v in finite(mem)}
    rows = [(t, v, mem_at[t]) for t, v in finite(cpu) if t in mem_at]
    if len(rows) < MIN_COVERAGE * len(finite(cpu)):
        return False, (f"only {len(rows)} of {len(finite(cpu))} CPU samples have a "
                       f"matching memory sample")

    out = os.path.join(d, "cadvisor_samples.csv")
    with open(out, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["ts", "cpu_usage_seconds_total", "mem_usage_bytes"])
        for t, v, m in rows:
            wr.writerow([t, v, m])
    return True, f"wrote {out} ({len(rows)} samples){note}"


# ---------------------------------------------------------------- sweep
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", default="http://127.0.0.1:9090")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "raw_metrics"))
    g = ap.parse_args()
    if not os.path.isdir(g.root):
        print(f"no raw_metrics yet at {g.root}")
        return 1

    written = failed = 0
    for arm in sorted(os.listdir(g.root)):
        container = CONTAINER.get(arm)
        if not container:
            continue
        for level in sorted(os.listdir(os.path.join(g.root, arm))):
            for rep in sorted(os.listdir(os.path.join(g.root, arm, level))):
                d = os.path.join(g.root, arm, level, rep)
                rj = os.path.join(d, "run.json")
                if not os.path.exists(rj):
                    continue
                try:
                    rec = json.load(open(rj))
                    ok, msg = export_run(g.prom, d, rec, container)
                except Exception as e:                    # network blip, bad json, ...
                    ok, msg = False, f"{type(e).__name__}: {e}"
                if ok:
                    written += 1
                    print(msg)
                else:
                    failed += 1
                    print(f"ERROR {d}: {msg}; cadvisor_samples.csv NOT written "
                          f"— re-export or re-run this cell")

    if written + failed == 0:
        print(f"ERROR no run.json found under {g.root} — is --root pointing at raw_metrics?")
        return 1
    print(f"done: {written} run windows exported, {failed} unusable")
    if failed:
        print("cross-check incomplete — fix the failed windows before analysing")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
