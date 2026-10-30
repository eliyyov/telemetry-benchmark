#!/usr/bin/env python3
"""Diagnostic cell runner - executes on VM-Data, driven by bench/diagnose.

Why a wrapper instead of more flags on orchestrator.py: the official sweep path
must stay untouched. This module imports orchestrator, points each arm at a
generated config, and calls the same run_once() the sweep uses - so the sampler,
the cgroup counter, the ABBA order and the validity checks are literally the
same code, and a diagnostic number cannot drift away from an official one.

Blocks
  ablation   source-only / +parse / full, both arms, one rate
  crossing   two rates around the predicted crossing, both arms
  poll       OTel poll_interval lowered, both arms, one rate

Every cell records the sha256 of the config file it ran, so a number can always
be traced back to the exact setting that produced it.
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
sys.path.insert(0, BENCH)

import orchestrator as O  # noqa: E402

CFG = os.path.join(BENCH, "diag_cfg")
OUT = os.path.join(BENCH, "diag_out")

# block -> list of (level name, rate, vector cfg, otel cfg)
BLOCKS = {
    "ablation": [
        ("abl-readonly", 5000, "vector-readonly.yaml", "otel-readonly.yaml"),
        ("abl-parseonly", 5000, "vector-parseonly.yaml", "otel-parseonly.yaml"),
        ("abl-full", 5000, "vector-base.yaml", "otel-base.yaml"),
    ],
    "crossing": [
        ("r25000", 25000, "vector-base.yaml", "otel-base.yaml"),
        ("r36000", 36000, "vector-base.yaml", "otel-base.yaml"),
    ],
    "poll": [
        ("poll50", 5000, "vector-base.yaml", "otel-poll50.yaml"),
    ],
}

FIELDS = ["block", "stage", "arm", "level", "rate", "rep", "attempt", "status",
          "cpu_mean_pct", "mem_peak_window_mib", "offered_eps", "consumed_eps",
          "samples", "config_sha", "started"]


def sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]
    except OSError:
        return "missing"


def compose_path(arm_tag, stage):
    return os.path.join(CFG, f"compose.{arm_tag}.{stage}.yml")


def run_block(block, reps, warmup, measure):
    os.makedirs(OUT, exist_ok=True)
    csv_path = os.path.join(OUT, f"{block}.csv")
    new = not os.path.exists(csv_path)
    fh = open(csv_path, "a", newline="")
    w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
    if new:
        w.writeheader()

    O.WARMUP, O.MEASURE = warmup, measure

    arms = ["v", "o"]
    for level, rate, vcfg, ocfg in BLOCKS[block]:
        O.LEVELS[level] = rate
        cfgs = {"v": os.path.join(CFG, vcfg), "o": os.path.join(CFG, ocfg)}
        O.ARMS["v"].update(compose=compose_path("vector", vcfg.split(".")[0].split("-", 1)[-1]),
                           container="v-vector")
        O.ARMS["o"].update(compose=compose_path("otel", ocfg.split(".")[0].split("-", 1)[-1]),
                           container="o-otel")
        print(f"== {block} / {level} ({rate} ev/s)  reps {reps}  "
              f"V={os.path.basename(cfgs['v'])} O={os.path.basename(cfgs['o'])}", flush=True)

        for rep in range(1, reps + 1):
            order = arms if rep % 2 else list(reversed(arms))
            for arm in order:
                rec = None
                for attempt in range(1, O.MAX_RETRIES + 2):
                    rec = O.run_once(arm, level, rep, attempt)
                    if rec.get("status") in ("valid", "lower_bound"):
                        break
                if rec is None:
                    continue
                w.writerow({
                    "block": block,
                    "stage": level.split("-", 1)[-1] if "-" in level else level,
                    "arm": rec.get("arm"),
                    "level": level,
                    "rate": rate,
                    "rep": rep,
                    "attempt": rec.get("attempt"),
                    "status": rec.get("status"),
                    "cpu_mean_pct": rec.get("cpu_mean_pct"),
                    "mem_peak_window_mib": rec.get("mem_peak_window_mib"),
                    "offered_eps": rec.get("offered_eps"),
                    "consumed_eps": rec.get("consumed_eps"),
                    "samples": rec.get("samples"),
                    "config_sha": sha(cfgs[arm]),
                    "started": rec.get("started", ""),
                })
                fh.flush()
    fh.close()
    O.teardown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", required=True, choices=sorted(BLOCKS))
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--warmup", type=float, default=15.0)
    ap.add_argument("--measure", type=float, default=60.0)
    g = ap.parse_args()
    t0 = time.time()
    run_block(g.block, g.reps, g.warmup, g.measure)
    print(f"block {g.block} done in {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
