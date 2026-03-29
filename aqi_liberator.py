#!/usr/bin/env python3
"""Decode aqicn.org historical AQI data from their proprietary SSE encoding.

Data source: https://att.waqi.info/api/attsse/{station_id}/yd.json
Encoding: custom run-length delta encoding, reverse-engineered from historic-full.js

Exit codes: 0=success, 1=user error, 2=network error, 3=decode error
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ATTSSE_URL = "https://att.waqi.info/api/attsse"
SEARCH_URL = "https://api.waqi.info/v2/search"
USAGE_PATH = Path.home() / ".aqi-liberator" / "usage.jsonl"


# ---------------------------------------------------------------------------
# Decoder — reverse-engineered from aqicn.org/webapp/dist/historic-full.js
# ---------------------------------------------------------------------------

def decode_series(encoded: str) -> list[tuple[int, float]]:
    """Decode a single aqicn encoded series into (time_index, value) pairs.

    Encoding spec:
      A-Z (65-90):   delta = ch - 65 (A=0, B=1, ..., Z=25)
      a-z (97-122):  delta = -(ch - 97) - 1 (a=-1, b=-2, ..., z=-26)
      0-9 (48-57):   accumulate repeat count
      ! (33):        emit with explicit delta from following signed integer
      | (124):       skip time slots (advance index by N-1)
      $ (36):        skip 1 slot
      % (37):        skip 2 slots
      ' (39):        skip 3 slots
      / (47):        set scale factor
      * (42):        set inverse scale (position 0 only)
    """
    n = 0       # time index
    r = 0       # running value
    o = 0       # repeat count accumulator
    scale = 1.0
    result = []

    def emit(delta, count):
        nonlocal n, r
        if count == 0:
            count = 1
        for _ in range(count):
            n += 1
            r += delta
            result.append((n, r * scale))

    i = 0
    while i < len(encoded):
        ch = ord(encoded[i])

        if i == 0 and ch == 42:  # *
            i += 1
            num, i = _read_number(encoded, i)
            scale = 1.0 / num if num else 1.0
            continue
        elif ch == 36:
            n += 1
        elif ch == 37:
            n += 2
        elif ch == 39:
            n += 3
        elif ch == 47:  # /
            i += 1
            num, i = _read_number(encoded, i)
            scale = num
            continue
        elif ch == 33:  # !
            i += 1
            num, i = _read_number(encoded, i)
            emit(num, o)
            o = 0
            continue
        elif ch == 124:  # |
            i += 1
            num, i = _read_number(encoded, i)
            n += int(num) - 1
            continue
        elif 65 <= ch <= 90:
            emit(ch - 65, o)
            o = 0
        elif 97 <= ch <= 122:
            emit(-(ch - 97) - 1, o)
            o = 0
        elif 48 <= ch <= 57:
            o = 10 * o + ch - 48
        else:
            raise ValueError(f"invalid char '{encoded[i]}' (code {ch}) at pos {i}")
        i += 1

    return result


def _read_number(s: str, i: int) -> tuple[float, int]:
    sign = 1
    if i < len(s) and s[i] == '-':
        sign = -1
        i += 1
    num = 0
    while i < len(s) and s[i].isdigit():
        num = 10 * num + int(s[i])
        i += 1
    if i < len(s) and s[i] == '.':
        i += 1
    return sign * num, i


# ---------------------------------------------------------------------------
# SSE parsing + station decoding
# ---------------------------------------------------------------------------

def parse_sse(content: str) -> list[dict]:
    events = []
    for m in re.finditer(r'event: data\ndata: (.+)', content):
        try:
            events.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    return events


def decode_station(sse_content: str) -> list[dict]:
    """Decode SSE stream into list of {period, st, dh, pollutants, meta}."""
    chunks = []
    for event in parse_sse(sse_content):
        msg = event.get("msg")
        if not msg or "st" not in msg:
            continue
        st = msg["st"]
        dh = msg.get("dh", 24)
        ps = msg.get("ps", {})
        meta = msg.get("meta", {}).get("si", {})
        pollutants = {}
        for pol, encoded in ps.items():
            if not encoded or encoded[0] != "1":
                continue
            pairs = decode_series(encoded[1:])
            decoded = []
            for idx, value in pairs:
                epoch_hours = idx * dh + st
                dt = datetime.fromtimestamp(epoch_hours * 3600, tz=timezone.utc)
                decoded.append((dt.strftime("%Y-%m-%d"), round(value, 2)))
            pollutants[pol] = decoded
        chunks.append({"st": st, "dh": dh, "pollutants": pollutants, "meta": meta})
    return chunks


def flatten(chunks: list[dict], pollutant: str = "pm25") -> list[tuple[str, float]]:
    """Flatten chunks into sorted (date_str, value) pairs, deduped by date."""
    points = {}
    for chunk in chunks:
        for date_str, val in chunk.get("pollutants", {}).get(pollutant, []):
            points[date_str] = val
    return sorted(points.items())


def available_pollutants(chunks: list[dict]) -> list[str]:
    pols = set()
    for c in chunks:
        pols.update(c.get("pollutants", {}).keys())
    order = ["pm25", "pm10", "o3", "no2", "so2", "co"]
    return [p for p in order if p in pols] + sorted(pols - set(order))


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def fetch_sse(station_id: int, timeout: int = 30) -> str:
    import urllib.request
    url = f"{ATTSSE_URL}/{station_id}/yd.json"
    req = urllib.request.Request(url, headers={"User-Agent": "aqi-liberator/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def search_stations(keyword: str, timeout: int = 10) -> list[dict]:
    import urllib.request, urllib.parse
    url = f"{SEARCH_URL}/?keyword={urllib.parse.quote(keyword)}&token=demo"
    req = urllib.request.Request(url, headers={"User-Agent": "aqi-liberator/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if data.get("status") != "ok":
        return []
    return [
        {"uid": s["uid"], "name": s.get("station", {}).get("name", ""), "aqi": s.get("aqi", "")}
        for s in data.get("data", [])
    ]


# ---------------------------------------------------------------------------
# Output formatting (AX: structured, minimal, pipeable)
# ---------------------------------------------------------------------------

def write_csv(rows: list[list], headers: list[str] | None = None):
    w = csv.writer(sys.stdout, lineterminator="\n")
    if headers:
        w.writerow(headers)
    w.writerows(rows)


def write_json(data):
    json.dump(data, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Telemetry (AX principle #10)
# ---------------------------------------------------------------------------

def log_usage(cmd: str, ok: bool, ms: int, **extra):
    try:
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "cmd": cmd, "ok": ok, "ms": ms, **extra}
        with open(USAGE_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_fetch(args):
    t0 = time.monotonic()
    pols = args.pol.split(",") if args.pol else None
    all_rows = []

    for sid in args.stations:
        try:
            stderr(f"fetching {sid}...")
            content = fetch_sse(int(sid), timeout=args.timeout)
        except Exception as e:
            stderr(f"error fetching {sid}: {e}")
            log_usage("fetch", False, _ms(t0), station=sid, error=str(e)[:100])
            sys.exit(2)

        if args.save:
            path = f"{sid}.sse"
            with open(path, "w") as f:
                f.write(content)
            stderr(f"saved {path} ({len(content)} bytes)")

        chunks = decode_station(content)
        if not chunks:
            stderr(f"station {sid}: no data decoded")
            stderr(f"  try: aqi-liberator stations --search 'city name' to find valid station IDs")
            continue

        station_pols = pols or available_pollutants(chunks)
        meta = chunks[0].get("meta", {})
        station_name = meta.get("city", {}).get("name", str(sid))

        for pol in station_pols:
            series = flatten(chunks, pol)
            series = _filter_dates(series, args)
            for date_str, val in series:
                all_rows.append([date_str, sid, station_name, pol, val])

    if not all_rows:
        stderr(f"no data. searched {len(args.stations)} station(s), pol={args.pol or 'all'}")
        stderr(f"  try: aqi-liberator stations --search 'bangkok' to find stations")
        log_usage("fetch", False, _ms(t0), stations=args.stations)
        sys.exit(1)

    headers = ["date", "station_id", "station_name", "pollutant", "value"]
    if args.json:
        write_json([dict(zip(headers, r)) for r in all_rows])
    else:
        write_csv(all_rows, headers)

    log_usage("fetch", True, _ms(t0), stations=args.stations, rows=len(all_rows))


def cmd_decode(args):
    t0 = time.monotonic()
    pols = args.pol.split(",") if args.pol else None

    if args.raw:
        # Decode a single encoded string from stdin
        encoded = sys.stdin.read().strip()
        if not encoded:
            stderr("no input on stdin")
            sys.exit(1)
        try:
            pairs = decode_series(encoded)
        except ValueError as e:
            stderr(f"decode error: {e}")
            sys.exit(3)
        if args.json:
            write_json([{"index": idx, "value": val} for idx, val in pairs])
        else:
            write_csv([[idx, val] for idx, val in pairs], ["index", "value"])
        log_usage("decode", True, _ms(t0), raw=True, points=len(pairs))
        return

    all_rows = []
    for path in args.files:
        try:
            with open(path) as f:
                content = f.read()
        except FileNotFoundError:
            stderr(f"file not found: {path}")
            sys.exit(1)

        chunks = decode_station(content)
        station_pols = pols or available_pollutants(chunks)
        meta = chunks[0].get("meta", {}) if chunks else {}
        sid = meta.get("city", {}).get("idx", path)
        name = meta.get("city", {}).get("name", path)

        for pol in station_pols:
            series = flatten(chunks, pol)
            series = _filter_dates(series, args)
            for date_str, val in series:
                all_rows.append([date_str, sid, name, pol, val])

    if not all_rows:
        stderr(f"no data decoded from {len(args.files)} file(s)")
        sys.exit(1)

    headers = ["date", "station_id", "station_name", "pollutant", "value"]
    if args.json:
        write_json([dict(zip(headers, r)) for r in all_rows])
    else:
        write_csv(all_rows, headers)
    log_usage("decode", True, _ms(t0), files=len(args.files), rows=len(all_rows))


def cmd_compare(args):
    t0 = time.monotonic()
    pol = args.pol or "pm25"
    station_data = {}

    for sid in args.stations:
        try:
            stderr(f"fetching {sid}...")
            content = fetch_sse(int(sid), timeout=args.timeout)
        except Exception as e:
            stderr(f"error fetching {sid}: {e}")
            log_usage("compare", False, _ms(t0), error=str(e)[:100])
            sys.exit(2)
        chunks = decode_station(content)
        meta = chunks[0].get("meta", {}) if chunks else {}
        name = meta.get("city", {}).get("name", str(sid))
        series = flatten(chunks, pol)
        series = _filter_dates(series, args)
        station_data[sid] = {"name": name, "series": dict(series)}

    # Merge by date
    all_dates = set()
    for sd in station_data.values():
        all_dates.update(sd["series"].keys())

    if not all_dates:
        stderr(f"no {pol} data for stations {args.stations}")
        stderr(f"  available pollutants vary by station. try: --pol pm10")
        sys.exit(1)

    sids = args.stations
    if args.json:
        rows = []
        for d in sorted(all_dates):
            row = {"date": d}
            for sid in sids:
                row[station_data[sid]["name"]] = station_data[sid]["series"].get(d)
            rows.append(row)
        write_json(rows)
    else:
        headers = ["date"] + [station_data[s]["name"] for s in sids]
        rows = []
        for d in sorted(all_dates):
            row = [d] + [station_data[s]["series"].get(d, "") for s in sids]
            rows.append(row)
        write_csv(rows, headers)

    log_usage("compare", True, _ms(t0), stations=sids, pol=pol, dates=len(all_dates))


def cmd_stations(args):
    t0 = time.monotonic()
    if not args.search:
        stderr("usage: aqi-liberator stations --search 'city name'")
        sys.exit(1)

    try:
        results = search_stations(args.search, timeout=args.timeout)
    except Exception as e:
        stderr(f"search error: {e}")
        log_usage("stations", False, _ms(t0), error=str(e)[:100])
        sys.exit(2)

    if not results:
        stderr(f"no stations found for '{args.search}'")
        stderr(f"  try a broader term (country name, region)")
        log_usage("stations", False, _ms(t0), search=args.search, results=0)
        sys.exit(1)

    if args.json:
        write_json(results)
    else:
        write_csv([[r["uid"], r["name"], r["aqi"]] for r in results], ["uid", "name", "aqi"])

    log_usage("stations", True, _ms(t0), search=args.search, results=len(results))


def cmd_usage(args):
    if not USAGE_PATH.exists():
        stderr("no usage data yet")
        sys.exit(0)
    from collections import Counter
    cmds = Counter()
    ok_count = 0
    fail_count = 0
    total_ms = 0
    n = 0
    with open(USAGE_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            cmds[entry.get("cmd", "?")] += 1
            if entry.get("ok"):
                ok_count += 1
            else:
                fail_count += 1
            total_ms += entry.get("ms", 0)
    if n == 0:
        stderr("no usage data")
        sys.exit(0)
    data = {
        "invocations": n,
        "success": ok_count,
        "failures": fail_count,
        "avg_ms": round(total_ms / n),
        "commands": dict(cmds.most_common()),
    }
    if args.json:
        write_json(data)
    else:
        for k, v in data.items():
            print(f"{k}: {v}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stderr(msg: str):
    sys.stderr.write(msg + "\n")


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _filter_dates(series: list[tuple[str, float]], args) -> list[tuple[str, float]]:
    out = series
    if hasattr(args, "month") and args.month:
        m = args.month.zfill(2)
        out = [(d, v) for d, v in out if d[5:7] == m]
    if hasattr(args, "from_date") and args.from_date:
        out = [(d, v) for d, v in out if d >= args.from_date]
    if hasattr(args, "to_date") and args.to_date:
        out = [(d, v) for d, v in out if d <= args.to_date]
    return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="aqi-liberator",
        description="Decode aqicn.org historical AQI data",
    )
    sub = p.add_subparsers(dest="cmd")

    # fetch
    f = sub.add_parser("fetch", help="fetch & decode station data")
    f.add_argument("stations", nargs="+", help="station IDs (numeric)")
    f.add_argument("--json", action="store_true", help="JSON output")
    f.add_argument("--pol", help="pollutants (comma-separated, default: all)")
    f.add_argument("--month", help="filter to month (01-12)")
    f.add_argument("--from-date", help="start date (YYYY-MM-DD)")
    f.add_argument("--to-date", help="end date (YYYY-MM-DD)")
    f.add_argument("--save", action="store_true", help="save raw SSE to {id}.sse")
    f.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")

    # decode
    d = sub.add_parser("decode", help="decode local SSE files")
    d.add_argument("files", nargs="*", help="SSE files to decode")
    d.add_argument("--raw", action="store_true", help="decode single string from stdin")
    d.add_argument("--json", action="store_true", help="JSON output")
    d.add_argument("--pol", help="pollutants (comma-separated)")
    d.add_argument("--month", help="filter to month (01-12)")
    d.add_argument("--from-date", help="start date (YYYY-MM-DD)")
    d.add_argument("--to-date", help="end date (YYYY-MM-DD)")

    # compare
    c = sub.add_parser("compare", help="compare stations side by side")
    c.add_argument("stations", nargs="+", help="station IDs")
    c.add_argument("--json", action="store_true", help="JSON output")
    c.add_argument("--pol", help="pollutant (default: pm25)")
    c.add_argument("--month", help="filter to month (01-12)")
    c.add_argument("--from-date", help="start date (YYYY-MM-DD)")
    c.add_argument("--to-date", help="end date (YYYY-MM-DD)")
    c.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")

    # stations
    s = sub.add_parser("stations", help="search for station IDs")
    s.add_argument("--search", required=True, help="search keyword")
    s.add_argument("--json", action="store_true", help="JSON output")
    s.add_argument("--timeout", type=int, default=10, help="HTTP timeout seconds")

    # usage
    sub.add_parser("usage", help="show usage stats").add_argument("--json", action="store_true")

    args = p.parse_args()
    if not args.cmd:
        p.print_help(sys.stderr)
        sys.exit(1)

    {"fetch": cmd_fetch, "decode": cmd_decode, "compare": cmd_compare,
     "stations": cmd_stations, "usage": cmd_usage}[args.cmd](args)


if __name__ == "__main__":
    main()
