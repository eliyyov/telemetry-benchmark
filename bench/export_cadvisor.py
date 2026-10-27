#!/usr/bin/env python3
"""Export cAdvisor series from Prometheus for every finished run window (run on VM-Mon).

Cross-check path (V5/V6 in Table 3.3): the cgroup sampler is the primary measurement,
cAdvisor via Prometheus is the independent second path. For each run in
raw_metrics/<arm>/<level>/runN/ this script queries Prometheus for that run's exact
measurement window (run.json "window") and writes cadvisor_samples.csv beside
cgroup_samples.csv. analyze.py then compares the two paths.

Usage: python3 export_cadvisor.py [--prom http://127.0.0.1:9090] [--root raw_metrics]
"""
import argparse, csv, json, os, sys, urllib.parse, urllib.request

CONTAINER = {"vector": "v-vector", "otel": "o-otel"}

# A window that Prometheus can only partly answer must not be written: a flat or
# truncated series would look like a valid measurement downstream and silently
# defeat the cross-check (V5/V6). Validate first, refuse loudly.
MIN_POINTS = 5        # 120 s window at a 5 s step should give ~24
MIN_DISTINCT = 3      # a cumulative CPU counter has to move
MIN_COVERAGE = 0.90   # the series must span >=90% of the run window


def query_range(prom, metric, container, start, end, step=5):
    # cAdvisor labels containers with name= / id= / image= (verified at v0.60.6 tag,
    # lib/metrics/prometheus.go LabelName). Try container_name= as a fallback anyway.
    for label in ("name", "container_name"):
        q = f'{metric}{{{label}="{container}"}}'
        url = (f"{prom}/api/v1/query_range?" + urllib.parse.urlencode(
            {"query": q, "start": start, "end": end, "step": step}))
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
        res = d.get("data", {}).get("result", [])
        if res:
            return res[0]["values"]
    return []


def series_problems(cpu, ws, we):
    """Reasons this cAdvisor series cannot serve as a cross-check ([] = usable)."""
    if not cpu:
        return ["empty series"]
    problems = []
    vals = [float(v) for _, v in cpu]
    if len(cpu) < MIN_POINTS:
        problems.append(f"{len(cpu)} points")
    if len(set(vals)) < MIN_DISTINCT:
        problems.append(f"{len(set(vals))} distinct counter value")
    span = float(cpu[-1][0]) - float(cpu[0][0])
    if (we - ws) > 0 and span / (we - ws) < MIN_COVERAGE:
        problems.append(f"covers {100 * span / (we - ws):.0f}% of the window")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", default="http://127.0.0.1:9090")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_metrics"))
    g = ap.parse_args()
    if not os.path.isdir(g.root):
        print(f"no raw_metrics yet at {g.root}")
        return
    runs = 0
    failed = 0
    for arm in sorted(os.listdir(g.root)):
        c = CONTAINER.get(arm)
        if not c:
            continue
        for level in sorted(os.listdir(os.path.join(g.root, arm))):
            for rep in sorted(os.listdir(os.path.join(g.root, arm, level))):
                d = os.path.join(g.root, arm, level, rep)
                rj = os.path.join(d, "run.json")
                if not os.path.exists(rj):
                    continue
                rec = json.load(open(rj))
                w = rec.get("window") or {}
                ws, we = w.get("start"), w.get("end")
                if not (ws and we):
                    print(f"skip {d}: no window in run.json")
                    continue
                cpu = query_range(g.prom, "container_cpu_usage_seconds_total", c, ws, we)
                mem = query_range(g.prom, "container_memory_usage_bytes", c, ws, we)
                if not cpu or not mem:
                    print(f"ERROR {d}: no cAdvisor series in Prometheus for {c}; "
                          f"cadvisor_samples.csv NOT written")
                    failed += 1
                    continue
                problems = series_problems(cpu, ws, we)
                if problems:
                    print(f"ERROR {d}: cAdvisor series unusable ({', '.join(problems)}); "
                          f"cadvisor_samples.csv NOT written — re-export or re-run this cell")
                    failed += 1
                    continue
                mem_at = {float(t): float(v) for t, v in mem}
                out = os.path.join(d, "cadvisor_samples.csv")
                with open(out, "w", newline="") as fh:
                    wr = csv.writer(fh)
                    wr.writerow(["ts", "cpu_usage_seconds_total", "mem_usage_bytes"])
                    for t, v in cpu:
                        t = float(t)
                        if t in mem_at:
                            wr.writerow([t, v, mem_at[t]])
                runs += 1
                print(f"wrote {out} ({len(cpu)} samples)")
    print(f"done: {runs} run windows exported, {failed} unusable")
    if failed:
        print("cross-check incomplete — fix the failed windows before analysing")
        sys.exit(1)


if __name__ == "__main__":
    main()
