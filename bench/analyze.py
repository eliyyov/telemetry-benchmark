#!/usr/bin/env python3
"""Analysis for the Vector vs OpenTelemetry Collector benchmark (run on VM-Mon).

Reads results/runs.csv plus per-run raw files, then writes results/summary.md and prints
the same content:
  - per level x arm: n, mean/median CPU (O1) and peak memory (O2), offered/consumed eps
  - per level: Vector/OTel ratios and the exact two-sided Mann-Whitney test (permutation
    over all C(10,5) label assignments; handles ties; p floor 2/252 = 0.0079 at n=5 vs 5)
  - V5/V6 cross-check: cgroup sampler vs cAdvisor (Prometheus export), tolerance 5%
  - validity summary (V1/V3/V4/V10/V11) and excluded runs

Usage: python3 analyze.py [--root .] [--selftest]
"""
import argparse, csv, itertools, json, os, statistics
from fractions import Fraction

MiB = 1048576
TOL = 0.05  # V5/V6 relative tolerance on the arm total


# ---------- exact two-sided Mann-Whitney (permutation over pooled values) ----------
def rankdata(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def u_stat(a, b):
    pooled, n1 = a + b, len(a)
    ranks = rankdata(pooled)
    r1 = sum(ranks[:n1])
    return r1 - n1 * (n1 + 1) / 2


def mann_whitney_exact(a, b):
    """Two-sided exact p by permutation of labels; U and p. Exact under ties."""
    n1, n2 = len(a), len(b)
    obs = u_stat(a, b)
    mu = n1 * n2 / 2
    pooled = a + b
    count, extreme = 0, 0
    for idx in itertools.combinations(range(n1 + n2), n1):
        grp = [pooled[i] for i in idx]
        rest = [pooled[i] for i in range(n1 + n2) if i not in idx]
        u = u_stat(grp, rest)
        count += 1
        if abs(u - mu) >= abs(obs - mu):
            extreme += 1
    return obs, Fraction(extreme, count)


def fmt_p(p):
    return f"p = {float(p):.4f}" + (" (= exact floor)" if p == Fraction(2, 252) or (p.numerator == 1 and p.denominator == 126) else "")


# ---------- cross-check ----------
def cross_check(run_dir):
    """V5/V6: compare cgroup sampler with cAdvisor for one run window."""
    cj, sj = os.path.join(run_dir, "cadvisor_samples.csv"), os.path.join(run_dir, "cgroup_samples.csv")
    rj = os.path.join(run_dir, "run.json")
    if not (os.path.exists(cj) and os.path.exists(sj) and os.path.exists(rj)):
        return None
    rec = json.load(open(rj))
    ws, we = (rec.get("window") or {}).get("start"), (rec.get("window") or {}).get("end")
    if not (ws and we):
        return None
    srows = [r for r in csv.DictReader(open(sj))
             if r["cpu_usage_usec"] and ws <= float(r["ts"]) <= we]
    crows = [r for r in csv.DictReader(open(cj)) if ws <= float(r["ts"]) <= we]
    if len(srows) < 2 or len(crows) < 2:
        return None
    # CPU: mean % of one core from the cumulative counter on both paths
    def mean_cpu(rows, ts_k, cpu_k):
        dt = float(rows[-1][ts_k]) - float(rows[0][ts_k])
        return (float(rows[-1][cpu_k]) - float(rows[0][cpu_k])) / dt
    s_cpu = mean_cpu(srows, "ts", "cpu_usage_usec") / 1e6 * 100
    c_cpu = mean_cpu(crows, "ts", "cpu_usage_seconds_total") * 100
    s_mem = statistics.mean(int(r["mem_current_bytes"]) for r in srows)
    c_mem = statistics.mean(float(r["mem_usage_bytes"]) for r in crows)
    rel = lambda a, b: abs(a - b) / b if b else 0.0
    return {"sampler_cpu": s_cpu, "cadvisor_cpu": c_cpu, "cpu_diff": rel(s_cpu, c_cpu),
            "sampler_mem": s_mem / MiB, "cadvisor_mem": c_mem / MiB, "mem_diff": rel(s_mem, c_mem),
            "cpu_ok": rel(s_cpu, c_cpu) <= TOL, "mem_ok": rel(s_mem, c_mem) <= TOL}


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--selftest", action="store_true")
    g = ap.parse_args()

    if g.selftest:
        assert mann_whitney_exact([1, 2, 3], [4, 5, 6])[1] == Fraction(2, 20)   # complete separation, 3v3
        assert mann_whitney_exact([5, 5, 5], [5, 5, 5])[1] == Fraction(1, 1)     # identical
        u, p = mann_whitney_exact([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
        assert p == Fraction(2, 252), p                                            # exact floor at 5v5
        assert mann_whitney_exact([10, 20, 30], [20, 30, 40])[1] == Fraction(10, 20)  # ties case, hand-enumerated
        print("selftest ok: exact Mann-Whitney validated on 4 hand-computable cases")
        return

    csv_path = os.path.join(g.root, "results", "runs.csv")
    if not os.path.exists(csv_path):
        print(f"no runs yet ({csv_path} missing) - run ./run.sh first")
        return
    rows = list(csv.DictReader(open(csv_path)))
    out = []
    P = out.append
    P("# Vector vs OpenTelemetry Collector - run summary\n")
    P(f"runs in runs.csv: {len(rows)}\n")

    arms = sorted({r["arm"] for r in rows})
    levels = [l for l in ("normal", "medium", "high") if any(r["level"] == l for r in rows)]
    good = lambda r: r["status"] in ("valid", "lower_bound")

    P("## Per level and arm (valid + lower-bound runs)\n")
    P("| level | arm | n | CPU mean % | CPU median % | mem peak MiB (mean) | offered eps | consumed eps |")
    P("|---|---|---|---|---|---|---|---|")
    cells = {}
    for lv in levels:
        for a in arms:
            rs = [r for r in rows if r["level"] == lv and r["arm"] == a and good(r)]
            if not rs:
                continue
            cpu = [float(r["cpu_mean_pct"]) for r in rs if r["cpu_mean_pct"]]
            mem = [float(r["mem_peak_window_mib"]) for r in rs if r["mem_peak_window_mib"]]
            off = [float(r["offered_eps"]) for r in rs if r["offered_eps"]]
            con = [float(r["consumed_eps"]) for r in rs if r["consumed_eps"]]
            cells[(lv, a)] = {"cpu": cpu, "mem": mem}
            P(f"| {lv} | {a} | {len(rs)} | {statistics.mean(cpu):.2f} | {statistics.median(cpu):.2f} "
              f"| {statistics.mean(mem):.1f} | {statistics.mean(off):.0f} | {statistics.mean(con):.0f} |")
    P("")

    if len(arms) == 2:
        a1, a2 = arms
        P(f"## Ratios and exact Mann-Whitney U (ratio = {a1}/{a2}, two-sided, per level)\n")
        P("| level | CPU ratio | CPU test | mem ratio | mem test |")
        P("|---|---|---|---|---|")
        for lv in levels:
            c1, c2 = cells.get((lv, a1), {}).get("cpu"), cells.get((lv, a2), {}).get("cpu")
            m1, m2 = cells.get((lv, a1), {}).get("mem"), cells.get((lv, a2), {}).get("mem")
            row = [lv]
            for v1, v2 in ((c1, c2), (m1, m2)):
                if v1 and v2 and len(v1) >= 2 and len(v2) >= 2:
                    ratio = statistics.mean(v1) / statistics.mean(v2)
                    _, p = mann_whitney_exact(v1, v2)
                    row += [f"{ratio:.2f}x", f"U test {fmt_p(p)} (n={len(v1)} vs {len(v2)})"]
                else:
                    row += ["-", "needs >= 2 runs per cell"]
            P("| " + " | ".join(row) + " |")
        P("")

    P("## V5/V6 cross-check: cgroup sampler vs cAdvisor (tolerance 5%)\n")
    P("| arm | level | rep | CPU diff | mem diff | verdict |")
    P("|---|---|---|---|---|---|")
    fails = 0
    for arm in arms:
        for lv in levels:
            d0 = os.path.join(g.root, "raw_metrics", arm, lv)
            if not os.path.isdir(d0):
                continue
            for rep in sorted(os.listdir(d0)):
                cc = cross_check(os.path.join(d0, rep))
                if not cc:
                    continue
                ok = cc["cpu_ok"] and cc["mem_ok"]
                fails += 0 if ok else 1
                P(f"| {arm} | {lv} | {rep} | {cc['cpu_diff']*100:.1f}% | {cc['mem_diff']*100:.1f}% "
                  f"| {'pass' if ok else 'FAIL (repeat <=2x, then exclude)'} |")
    if not any("pass" in l or "FAIL" in l for l in out):
        P("| - | - | - | run export_cadvisor.py first | - | - |")
    P("")

    P("## Validity (V1 offered rate, V3 >= 0.98 consumed, V4 saturated, V10 no throttle, V11 no OOM)\n")
    for lv in levels:
        for a in arms:
            rs = [r for r in rows if r["level"] == lv and r["arm"] == a]
            if not rs:
                continue
            n = lambda k: sum(1 for r in rs if r.get(k) == "True" or r.get(k) is True)
            P(f"- {lv}/{a}: n={len(rs)}  V1 {n('v1_ok')}  V3 {n('v3_ok')}  "
              f"saturated(V4) {n('v4_saturated')}  V10 {n('v10_ok')}  V11 {n('v11_ok')}")
    excl = [r for r in rows if r["status"] == "excluded"]
    P(f"\nexcluded runs: {len(excl)}" + ("" if not excl else " -> " + ", ".join(f"{r['arm']}/{r['level']}/run{r['rep']}" for r in excl)))
    P(f"\ncross-check failures: {fails}")

    text = "\n".join(out) + "\n"
    with open(os.path.join(g.root, "results", "summary.md"), "w") as fh:
        fh.write(text)
    print(text)
    print("-> results/summary.md")


if __name__ == "__main__":
    main()
