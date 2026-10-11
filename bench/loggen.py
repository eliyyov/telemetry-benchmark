#!/usr/bin/env python3
"""
Rate-limited log writer for the benchmark.

Appends pre-generated log lines to a file at a controlled events/sec rate so
agents can be measured under a continuous, steady stream rather than a batch
drain. Keeps the on-disk footprint small (the corpus is cycled, not extended).

Usage:
  loggen.py --out FILE --template FILE --rate N [--duration SEC] [--chunk N]

--rate is events/sec. --duration 0 means run until SIGTERM.
Prints a single JSON line to stdout on exit with the achieved rate.
"""
import argparse
import json
import os
import signal
import sys
import time

STOP = False


def _handle(signum, frame):
    global STOP
    STOP = True


def _status(path, written, elapsed):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"written": written, "elapsed": round(elapsed, 3)}, fh)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--rate", type=float, required=True)
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--max-bytes", type=int, default=0,
                    help="truncate+restart the output file once it exceeds this size")
    ap.add_argument("--status", default="",
                    help="write {written, elapsed} to this file about once per second")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    with open(args.template, "rb") as fh:
        blob = fh.read()
    lines = [ln + b"\n" for ln in blob.split(b"\n") if ln]

    # target seconds per chunk
    chunk_secs = args.chunk / args.rate

    start = time.monotonic()
    written = 0
    idx = 0
    next_deadline = start
    last_status = 0.0
    out = open(args.out, "ab", buffering=1024 * 256)
    bytes_written = os.path.getsize(args.out) if os.path.exists(args.out) else 0

    try:
        while not STOP:
            now = time.monotonic()
            if args.duration and (now - start) >= args.duration:
                break

            payload = bytearray()
            for _ in range(args.chunk):
                payload += lines[idx]
                idx += 1
                if idx >= len(lines):
                    idx = 0
            out.write(payload)
            out.flush()
            written += args.chunk
            if args.status and now - last_status >= 1.0:
                _status(args.status, written, now - start)
                last_status = now
            bytes_written += len(payload)

            if args.max_bytes and bytes_written >= args.max_bytes:
                out.flush()
                out.close()
                os.truncate(args.out, 0)
                out = open(args.out, "ab", buffering=1024 * 256)
                bytes_written = 0

            next_deadline += chunk_secs
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # we're behind: resync so we don't spiral
                next_deadline = time.monotonic()
    finally:
        out.flush()
        out.close()
        if args.status:
            _status(args.status, written, time.monotonic() - start)

    elapsed = time.monotonic() - start
    result = {
        "written": written,
        "elapsed_sec": round(elapsed, 3),
        "target_rate": args.rate,
        "achieved_rate": round(written / elapsed, 1) if elapsed > 0 else 0,
    }
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()