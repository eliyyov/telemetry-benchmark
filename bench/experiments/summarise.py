#!/usr/bin/env python3
"""Fold the per-block diagnostic CSVs into results.csv + summary.md.

Runs on VM-Mon after the pull-back, on the files in the run folder.
"""
import csv, glob, os, statistics as st, sys

d = sys.argv[1] if len(sys.argv) > 1 else "."
files = [f for f in ("ablation.csv", "crossing.csv", "poll.csv") if os.path.exists(os.path.join(d, f))]
rows = []
for f in files:
    with open(os.path.join(d, f)) as fh:
        rows += list(csv.DictReader(fh))
if not rows:
    sys.exit("no rows to summarise")

cols = [k for k in rows[0].keys() if k != "_f"]
with open(os.path.join(d, "results.csv"), "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
    w.writerows([{k: r.get(k, "") for k in cols} for r in rows])

def m(stage, arm, col="cpu_mean_pct"):
    v = [float(r[col]) for r in rows if r.get("stage") == stage and r["arm"] == arm]
    return st.mean(v) if v else float("nan")

def n(stage, arm):
    return sum(1 for r in rows if r.get("stage") == stage and r["arm"] == arm)

stages, seen = [], set()
for r in rows:
    s = r.get("stage")
    if s and s not in seen:
        seen.add(s); stages.append(s)
rate = {r["stage"]: r.get("rate") for r in rows}

L = ["# Diagnostics - EC2 (vm-mon / vm-data)", "",
     f"{len(rows)} cells, {sum(1 for r in rows if r['status']=='valid')} valid, "
     f"{sum(1 for r in rows if r['status']!='valid')} not usable.", "",
     "| stage | rate | Vector % | OTel % | gap pp | Vector mem | OTel mem |", "|---|---|---|---|---|---|---|"]
for s in stages:
    v, o = m(s, "vector"), m(s, "otel")
    L.append(f"| {s} | {rate[s]} | {v:.2f} | {o:.2f} | {v-o:+.2f} | "
             f"{m(s,'vector','mem_peak_window_mib'):.0f} MiB | {m(s,'otel','mem_peak_window_mib'):.0f} MiB |")

if all(m(s, "vector") == m(s, "vector") for s in ("readonly", "parseonly", "full")):
    vr, oR = m("readonly", "vector"), m("readonly", "otel")
    vp, op = m("parseonly", "vector"), m("parseonly", "otel")
    vf, of = m("full", "vector"), m("full", "otel")
    L += ["", "## Marginal cost per stage (pp of one core)", "",
          f"- door (read only):  Vector {vr:.2f}  OTel {oR:.2f}  -> Vector pays {vr-oR:+.2f} more",
          f"- parse adds:        Vector {vp-vr:+.2f}  OTel {op-oR:+.2f}  -> Vector cheaper by {(op-oR)-(vp-vr):.2f}",
          f"- enrich adds:       Vector {vf-vp:+.2f}  OTel {of-op:+.2f}  -> Vector cheaper by {(of-op)-(vf-vp):.2f}",
          f"- net:               Vector {vf:.2f}  OTel {of:.2f}  -> gap {vf-of:+.2f}"]

if "poll50" in seen and "full" in seen:
    L += ["", f"- OTel poll 200 -> 50 ms: {m('full','otel'):.2f} -> {m('poll50','otel'):.2f} "
              f"(+{m('poll50','otel')-m('full','otel'):.2f} pp); "
              f"Vector {m('full','vector'):.2f} -> {m('poll50','vector'):.2f}"]

L += ["", "Explain, don't conclude: these runs say where the cost sits, not who wins.", "",
      "Every row carries the sha256 of its config (config_sha); configs are in ./configs/.", ""]
open(os.path.join(d, "summary.md"), "w").write("\n".join(L))
print("\n".join(L))
