#!/usr/bin/env python3
"""cgroup v2 sampler for the Vector vs OTel benchmark.

Reads each container's counters straight from the kernel at a fixed interval:
  /sys/fs/cgroup/system.slice/docker-<id>.scope/
      cpu.stat        usage_usec, nr_throttled, throttled_usec
      memory.current  memory in use now (bytes)
      memory.peak     highest memory since the container started (bytes)
      memory.events   oom_kill
One CSV row per container per sample. `ts` is wall-clock time so the orchestrator
can cut the measurement window exactly.
"""
import argparse, csv, os, subprocess, sys, time

ROOT = "/sys/fs/cgroup/system.slice"
FIELDS = ["ts", "elapsed_s", "container", "cpu_usage_usec", "cpu_pct_of_one_core",
          "mem_current_bytes", "mem_peak_bytes", "nr_throttled", "throttled_usec", "oom_kill"]


def docker_id(name):
    cmd = f"docker inspect --format '{{{{.Id}}}}' {name}"
    for c in (cmd, f'sg docker -c "{cmd}"'):
        r = subprocess.run(c, shell=True, capture_output=True, text=True)
        out = r.stdout.strip().strip("'")
        if r.returncode == 0 and len(out) >= 12:
            return out
    return None


def kv(path):
    d = {}
    try:
        with open(path) as fh:
            for line in fh:
                k, v = line.split()
                d[k] = int(v)
    except (OSError, ValueError):
        pass
    return d


def one(path):
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--containers", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0.0)
    a = ap.parse_args()

    paths = {}
    for n in [x for x in a.containers.split(",") if x]:
        cid = docker_id(n)
        p = os.path.join(ROOT, f"docker-{cid}.scope") if cid else None
        if p and os.path.isdir(p):
            paths[n] = p
        else:
            print(f"WARNING: no cgroup for {n}", file=sys.stderr)

    start = time.monotonic()
    prev = {}
    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        while True:
            now = time.monotonic()
            el = now - start
            if a.duration and el >= a.duration:
                break
            ts = time.time()
            for n, p in paths.items():
                cs = kv(os.path.join(p, "cpu.stat"))
                ev = kv(os.path.join(p, "memory.events"))
                u = cs.get("usage_usec")
                pct = ""
                if u is not None and n in prev:
                    pu, pt = prev[n]
                    if now > pt:
                        pct = round((u - pu) / ((now - pt) * 1e6) * 100, 4)
                if u is not None:
                    prev[n] = (u, now)
                w.writerow([f"{ts:.3f}", f"{el:.3f}", n, u, pct,
                            one(os.path.join(p, "memory.current")),
                            one(os.path.join(p, "memory.peak")),
                            cs.get("nr_throttled"), cs.get("throttled_usec"), ev.get("oom_kill")])
            fh.flush()
            sl = a.interval - (time.monotonic() - now)
            if sl > 0:
                time.sleep(sl)


if __name__ == "__main__":
    main()
