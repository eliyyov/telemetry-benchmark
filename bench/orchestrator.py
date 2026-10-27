#!/usr/bin/env python3
"""Orchestrator v2 - Vector vs OpenTelemetry Collector.

Runs on the data-plane host (VM-Data). On EC2 it is driven over SSH from VM-Mon by run.sh.
The only differences from the LXC rehearsal come from env vars:
  AGENT_CPUSET   cores given to the agent container   LXC "1"   EC2 "2-3"
  AGENT_CORES    how many cores that is (for V4)       LXC 1     EC2 2
  LOGGEN_CPUS    taskset core list for loggen          LXC "0"   EC2 "0"
  SAMPLER_CPUS   taskset core list for the sampler     LXC "0"   EC2 "1"
EC2 core plan on the 4-vCPU VM-Data: core 0 loggen, core 1 cAdvisor + sampler, cores 2-3 the agent.

Order: level by level (normal, medium, high). Inside a level the arms alternate in an
ABBA pattern (rep 1: V,O  rep 2: O,V ...) so slow drift cannot line up with one agent.

Per run: teardown -> reset log -> agent up -> ready -> sampler + loggen ->
warm-up -> 120 s window -> stop -> teardown -> aggregate + validity checks.
A run that fails a check is repeated up to MAX_RETRIES times.
"""
import argparse, csv, json, os, signal, statistics, subprocess, sys, time, urllib.request

BENCH = os.path.dirname(os.path.abspath(__file__))
LOGS, RAW, RES = (os.path.join(BENCH, d) for d in ("logs", "raw_metrics", "results"))

ARMS = {
    "v": {"name": "vector", "compose": "compose.vector.yml", "container": "v-vector",
          "url": "http://localhost:9598/metrics",
          "metric": "vector_component_received_events_total", "match": 'component_id="logs"'},
    "o": {"name": "otel", "compose": "compose.otel.yml", "container": "o-otel",
          "url": "http://localhost:8888/metrics",
          "metric": "otelcol_receiver_accepted_log_records", "match": 'receiver="file_log"'},
}
LEVELS = {"normal": 5000, "medium": 20000, "high": 50000}

ENV = {
    "AGENT_CPUSET": os.environ.get("AGENT_CPUSET", "2-3"),
    "AGENT_CORES": int(os.environ.get("AGENT_CORES", "2")),
    "LOGGEN_CPUS": os.environ.get("LOGGEN_CPUS", "0"),
    "SAMPLER_CPUS": os.environ.get("SAMPLER_CPUS", "1"),
}
WARMUP, MEASURE, MAX_RETRIES = 30.0, 120.0, 2
# Pinned sweep parameters. These are recorded into every run.json and every runs.csv
# row, so a data file is self-describing: a smoke run (10 s / 30 s) can never be
# mistaken for a sweep run (30 s / 120 s) after the fact.
SWEEP = {"reps": 5, "levels": "normal,medium,high", "arms": "v,o", "resume": False,
         "warmup_s": WARMUP, "measure_s": MEASURE}
FIELDS = ["arm", "level", "rep", "attempt", "status", "rate_target", "offered_eps", "consumed_eps",
          "consumed_ratio", "cpu_mean_pct", "cpu_max_pct", "cpu_p95_pct", "mem_peak_window_mib",
          "mem_mean_window_mib", "mem_peak_lifetime_mib", "throttled_delta", "oom_kill",
          "v1_ok", "v3_ok", "v4_saturated", "v10_ok", "v11_ok", "samples", "agent_cpuset",
          "warmup_s", "measure_s", "reps", "levels", "arms", "resume",
          "loggen_cpus", "sampler_cpus",
          "started", "finished"]

_DIRECT = subprocess.run("docker ps", shell=True, capture_output=True).returncode == 0


def dk(cmd, env=""):
    full = f"{env} docker {cmd}".strip()
    if not _DIRECT:
        full = f'sg docker -c "{full}"'
    return subprocess.run(full, shell=True, capture_output=True, text=True, timeout=180, cwd=BENCH)


def teardown():
    for a in ARMS.values():
        dk(f"compose -p bench-{a['name']} -f {a['compose']} down -v --remove-orphans")
        dk(f"rm -f {a['container']}")
    time.sleep(2)


def http(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode()
    except Exception:
        return None


def counter(arm):
    a = ARMS[arm]
    for _ in range(5):
        txt = http(a["url"])
        if txt:
            tot, hit = 0.0, False
            for line in txt.splitlines():
                if line.startswith(a["metric"]) and a["match"] in line:
                    try:
                        tot += float(line.rsplit("}", 1)[1].split()[0]); hit = True
                    except (IndexError, ValueError):
                        pass
            if hit:
                return tot
        time.sleep(1)
    return None


def loggen_written(path):
    try:
        return json.load(open(path))["written"]
    except Exception:
        return None


def aggregate(csv_path, ws, we):
    rows = [r for r in csv.DictReader(open(csv_path)) if r["cpu_usage_usec"]]
    win = [r for r in rows if ws <= float(r["ts"]) <= we]
    if len(win) < 2:
        return {}
    f, l = win[0], win[-1]
    dt = float(l["ts"]) - float(f["ts"])
    pcts = sorted(float(r["cpu_pct_of_one_core"]) for r in win if r["cpu_pct_of_one_core"])
    mem = [int(r["mem_current_bytes"]) for r in win if r["mem_current_bytes"]]
    MiB = 1048576
    return {
        # O1: exact mean from the cumulative counter, not an average of 1 s samples
        "cpu_mean_pct": round((int(l["cpu_usage_usec"]) - int(f["cpu_usage_usec"])) / (dt * 1e6) * 100, 4),
        "cpu_max_pct": round(pcts[-1], 4) if pcts else None,
        "cpu_p95_pct": round(pcts[max(0, int(len(pcts) * 0.95) - 1)], 4) if pcts else None,
        # O2: highest memory.current seen inside the window
        "mem_peak_window_mib": round(max(mem) / MiB, 2) if mem else None,
        "mem_mean_window_mib": round(statistics.mean(mem) / MiB, 2) if mem else None,
        "mem_peak_lifetime_mib": round(int(rows[-1]["mem_peak_bytes"]) / MiB, 2),
        "throttled_delta": int(rows[-1]["nr_throttled"]) - int(rows[0]["nr_throttled"]),
        "oom_kill": int(rows[-1]["oom_kill"]),
        "samples": len(win),
    }


def run_once(arm, level, rep, attempt):
    a, rate = ARMS[arm], LEVELS[level]
    d = os.path.join(RAW, a["name"], level, f"run{rep}")
    os.makedirs(d, exist_ok=True)
    rec = {"arm": a["name"], "level": level, "rep": rep, "attempt": attempt, "rate_target": rate,
           "agent_cpuset": ENV["AGENT_CPUSET"],
           "loggen_cpus": ENV["LOGGEN_CPUS"], "sampler_cpus": ENV["SAMPLER_CPUS"],
           **SWEEP,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    print(f"  {a['name']:6s} {level:6s} rep {rep} (attempt {attempt}) ... ", end="", flush=True)

    teardown()
    log = os.path.join(LOGS, "bench.log")
    with open(log, "wb") as fh:
        fh.write(b'{"seed":"x"}\n' * 200)
    status = os.path.join(d, "loggen_status.json")
    if os.path.exists(status):
        os.remove(status)

    dk(f"compose -p bench-{a['name']} -f {a['compose']} up -d", env=f"AGENT_CPUSET={ENV['AGENT_CPUSET']}")
    t = time.time() + 60
    while time.time() < t and counter(arm) is None:
        time.sleep(1)
    st = dk(f"inspect {a['container']} --format '{{{{.State.Status}}}}'").stdout.strip().strip("'")
    if st != "running" or counter(arm) is None:
        rec.update(status="error", finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        json.dump(rec, open(os.path.join(d, "run.json"), "w"), indent=2)
        print("ERROR: agent not ready")
        teardown()
        return rec

    total = WARMUP + MEASURE
    samp_csv = os.path.join(d, "cgroup_samples.csv")
    sampler = subprocess.Popen(["taskset", "-c", ENV["SAMPLER_CPUS"], sys.executable,
                                os.path.join(BENCH, "sampler.py"), "--containers", a["container"],
                                "--out", samp_csv, "--interval", "1", "--duration", str(total + 4)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    gen = subprocess.Popen(["taskset", "-c", ENV["LOGGEN_CPUS"], sys.executable,
                            os.path.join(BENCH, "loggen.py"), "--out", log,
                            "--template", os.path.join(BENCH, "corpus", "template.json"),
                            "--rate", str(rate), "--duration", str(total + 2), "--status", status],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(WARMUP)
    ws, w0, c0 = time.time(), loggen_written(status), counter(arm)
    time.sleep(MEASURE)
    we, w1, c1 = time.time(), loggen_written(status), counter(arm)

    gen.send_signal(signal.SIGTERM)
    try:
        out, _ = gen.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        gen.kill(); out, _ = gen.communicate()
    sampler.wait(timeout=30)
    with open(os.path.join(d, "agent_metrics.txt"), "w") as fh:
        fh.write(http(a["url"]) or "")
    teardown()

    rec.update(aggregate(samp_csv, ws, we))
    rec["window"] = {"start": ws, "end": we, "written": [w0, w1], "counter": [c0, c1]}
    try:
        rec["loggen"] = json.loads(out.strip().splitlines()[-1])
    except Exception:
        rec["loggen"] = {"raw": out}
    rec["offered_eps"] = round((w1 - w0) / (we - ws), 1) if None not in (w0, w1) else None
    rec["consumed_eps"] = round((c1 - c0) / (we - ws), 1) if None not in (c0, c1) else None
    if rec["offered_eps"] and rec["consumed_eps"] is not None:
        rec["consumed_ratio"] = round(rec["consumed_eps"] / rec["offered_eps"], 4)

    rec["v1_ok"] = bool(rec["offered_eps"]) and abs(rec["offered_eps"] / rate - 1) <= 0.02
    rec["v3_ok"] = (rec.get("consumed_ratio") or 0) >= 0.98
    rec["v4_saturated"] = (rec.get("cpu_max_pct") or 0) >= 0.95 * ENV["AGENT_CORES"] * 100
    rec["v10_ok"] = rec.get("throttled_delta") == 0
    rec["v11_ok"] = rec.get("oom_kill") == 0
    if not (rec["v1_ok"] and rec["v10_ok"] and rec["v11_ok"] and rec.get("samples")):
        rec["status"] = "repeat"
    elif rec["v3_ok"]:
        rec["status"] = "valid"
    elif rec["v4_saturated"]:
        rec["status"] = "lower_bound"
    else:
        rec["status"] = "repeat"
    rec["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    json.dump(rec, open(os.path.join(d, "run.json"), "w"), indent=2)
    print(f"{rec['status']}  cpu={rec.get('cpu_mean_pct')}%  mem={rec.get('mem_peak_window_mib')} MiB  "
          f"offered={rec['offered_eps']}  consumed={rec['consumed_eps']}")
    return rec


def append_row(rec):
    p = os.path.join(RES, "runs.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(rec)


def main():
    global WARMUP, MEASURE
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--levels", default="normal,medium,high")
    ap.add_argument("--arms", default="v,o")
    ap.add_argument("--warmup", type=float, help="smoke tests only")
    ap.add_argument("--measure", type=float, help="smoke tests only")
    ap.add_argument("--resume", action="store_true")
    g = ap.parse_args()
    WARMUP = g.warmup if g.warmup is not None else WARMUP
    MEASURE = g.measure if g.measure is not None else MEASURE
    SWEEP.update(reps=g.reps, levels=g.levels, arms=g.arms, resume=bool(g.resume),
                 warmup_s=WARMUP, measure_s=MEASURE)
    for p in (LOGS, RAW, RES):
        os.makedirs(p, exist_ok=True)
    arms = [x for x in g.arms.split(",") if x]
    print(f"env {ENV}  warmup {WARMUP}s  measure {MEASURE}s  docker direct={_DIRECT}")
    t0 = time.time()
    for level in [x for x in g.levels.split(",") if x]:
        for rep in range(1, g.reps + 1):
            order = arms if rep % 2 else list(reversed(arms))
            for arm in order:
                rj = os.path.join(RAW, ARMS[arm]["name"], level, f"run{rep}", "run.json")
                if g.resume and os.path.exists(rj) and json.load(open(rj)).get("status") in ("valid", "lower_bound"):
                    print(f"  skip {ARMS[arm]['name']} {level} rep {rep}")
                    continue
                for attempt in range(1, MAX_RETRIES + 2):
                    rec = run_once(arm, level, rep, attempt)
                    if rec["status"] in ("valid", "lower_bound"):
                        break
                if rec["status"] not in ("valid", "lower_bound"):
                    rec["status"] = "excluded"
                append_row(rec)
    print(f"done in {(time.time() - t0) / 60:.1f} min -> results/runs.csv")


if __name__ == "__main__":
    main()
