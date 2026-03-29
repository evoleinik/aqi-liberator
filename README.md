# aqi-liberator

Decode [aqicn.org](https://aqicn.org) historical air quality data from their proprietary encoding. Get 10+ years of daily AQI as plain CSV or JSON.

## Why

aqicn.org has the best historical AQI database in the world — thousands of stations, data since 2012. But there's no download button. The data is served in a custom binary-ish encoding through Server-Sent Events, rendered client-side in JavaScript, and never exposed as raw numbers.

This tool cracks that encoding and gives you the data as CSV or JSON. Pipe it into pandas, DuckDB, Excel, whatever.

## Install

```bash
# with uv (recommended)
uv tool install aqi-liberator

# or pip
pip install aqi-liberator

# or just run it directly
uvx aqi-liberator fetch 5775 --pol pm25
```

## Quick start

```bash
# Fetch 10 years of daily PM2.5 for Chiang Mai (station 5775)
aqi-liberator fetch 5775

# Compare two cities, April only
aqi-liberator compare 5775 5774 --month 04

# JSON output for piping to jq
aqi-liberator fetch 5775 --json --pol pm25 | jq '.[0]'

# Save raw data for offline analysis
aqi-liberator fetch 5775 --save
aqi-liberator decode 5775.sse --pol pm25,pm10

# Find station IDs
aqi-liberator stations --search "bangkok"
```

## Finding station IDs

Every station on aqicn.org has a numeric ID. You can find it by:

1. Going to `https://aqicn.org/city/YOUR-CITY/` and checking the URL
2. Using `aqi-liberator stations --search "city"`
3. Looking at the historical page: `https://aqicn.org/historical/` — hover over stations

Some well-known stations:

| ID | City |
|----|------|
| 5775 | Chiang Mai, Thailand |
| 5774 | Rayong, Thailand |
| 5773 | Bangkok, Thailand |
| 1827 | Phuket, Thailand |
| 1826 | Surat Thani, Thailand |
| 1849 | Chonburi, Thailand |

## Commands

### `fetch` — download and decode

```bash
aqi-liberator fetch STATION_ID [STATION_ID ...]
  --pol pm25,pm10     # filter pollutants (default: all)
  --month 04          # filter to month
  --from-date 2024-01-01 --to-date 2024-12-31
  --json              # JSON instead of CSV
  --save              # also save raw SSE file
  --timeout 30        # HTTP timeout in seconds
```

Output (CSV):
```
date,station_id,station_name,pollutant,value
2024-04-01,5775,Chiang Mai,pm25,164.0
2024-04-02,5775,Chiang Mai,pm25,227.0
```

### `decode` — decode local SSE files

```bash
aqi-liberator decode FILE [FILE ...]
  --raw               # decode a single encoded string from stdin
  --json / --pol / --month / --from-date / --to-date  (same as fetch)
```

### `compare` — side-by-side comparison

```bash
aqi-liberator compare STATION_ID STATION_ID [...]
  --pol pm25          # single pollutant (default: pm25)
  --json / --month / --from-date / --to-date  (same as fetch)
```

Output (CSV, wide format):
```
date,Chiang Mai,Rayong
2025-04-01,154.0,56.0
2025-04-02,153.0,59.0
```

### `stations` — find station IDs

```bash
aqi-liberator stations --search "city name"
  --json
```

### `usage` — telemetry

```bash
aqi-liberator usage [--json]
```

## Piping examples

```bash
# Average April PM2.5 by year
aqi-liberator fetch 5775 --pol pm25 --month 04 \
  | awk -F, 'NR>1{y=substr($1,1,4); s[y]+=$5; n[y]++} END{for(y in s) print y, s[y]/n[y]}'

# Load into DuckDB
aqi-liberator fetch 5775 5774 --pol pm25 \
  | duckdb -c "SELECT station_name, avg(value) FROM read_csv('/dev/stdin') GROUP BY 1"

# Side-by-side with jq
aqi-liberator compare 5775 5774 --json --month 04 --from-date 2025-04-01 \
  | jq '.[] | select(."Chiang Mai" > 150)'
```

## The encoding format

This section documents the proprietary encoding used by aqicn.org, reverse-engineered from their `historic-full.js`.

### Data source

Historical data is served as Server-Sent Events from:

```
https://att.waqi.info/api/attsse/{station_id}/yd.json
```

The response is a stream of SSE events:

```
event: debug
data: "Fetching 2026-P3"

event: data
data: {"msg":{"st":492312,"dh":24,"ps":{"pm25":"1!104eZXJg!-34lMP"},"time":{"span":["2026-03-29T00:00:00Z","2026-03-29T00:00:00Z"]},"meta":{"si":{"city":{"name":"Chiang Mai","idx":5775}}}}}
```

Each `event: data` message contains one time chunk (typically a month or quarter) with:

| Field | Description |
|-------|-------------|
| `msg.st` | Start time in hours since Unix epoch |
| `msg.dh` | Hours per data point (24 = daily) |
| `msg.ps` | Pollutant series — keys are pollutant names, values are encoded strings |
| `msg.time.span` | Date range this chunk covers |
| `msg.meta.si.city` | Station metadata |

### The delta encoding

Each pollutant value is a string like `"1!104eZXJg!-34lMP"`.

The first character is the format version:
- `1` = daily data (one value per `dh` hours)
- `2` = monthly/weekly aggregates (different time indexing)

The rest is a compact delta-encoded series. The decoder maintains:
- `n` — time slot index (starts at 0)
- `r` — running value (cumulative delta)
- `o` — pending repeat count
- `scale` — value multiplier (default 1)

Each output point is: `value = r * scale` at time `epoch = (n * dh + st) * 3600 seconds`

#### Character table

| Char | Code | Action |
|------|------|--------|
| `A`-`Z` | 65-90 | Emit delta = code - 65 (A=0, B=1, ..., Z=25) |
| `a`-`z` | 97-122 | Emit delta = -(code - 97) - 1 (a=-1, b=-2, ..., z=-26) |
| `0`-`9` | 48-57 | Accumulate repeat count: `o = 10*o + digit` |
| `!` | 33 | Emit delta from following signed integer |
| `\|` | 124 | Skip slots: advance n by following number - 1 |
| `$` | 36 | Skip 1 slot |
| `%` | 37 | Skip 2 slots |
| `'` | 39 | Skip 3 slots |
| `/` | 47 | Set scale factor from following number |
| `*` | 42 | Set scale = 1/following number (position 0 only) |

When a repeat count `o` is accumulated before a letter or `!`, the emit happens `o` times instead of once.

#### Worked example

Encoded: `!104eZXJg`

```
!104  → delta=104, emit: n=1, r=104  → value=104
e     → delta=-5,  emit: n=2, r=99   → value=99
Z     → delta=25,  emit: n=3, r=124  → value=124
X     → delta=23,  emit: n=4, r=147  → value=147
J     → delta=9,   emit: n=5, r=156  → value=156
g     → delta=-7,  emit: n=6, r=149  → value=149
```

Result: 6 daily values: `[104, 99, 124, 147, 156, 149]`

#### Time reconstruction

For daily data (`dh=24`) with `st=492312`:
- Point at index `n` has timestamp: `(n * 24 + 492312) * 3600` seconds since epoch
- In Python: `datetime.utcfromtimestamp((n * 24 + 492312) * 3600)`

### Available pollutants

Each station may have any combination of:

| Key | Pollutant |
|-----|-----------|
| `pm25` | PM2.5 (AQI) |
| `pm10` | PM10 (AQI) |
| `o3` | Ozone (AQI) |
| `no2` | Nitrogen dioxide (AQI) |
| `so2` | Sulfur dioxide (AQI) |
| `co` | Carbon monoxide (AQI) |

Values are US EPA AQI scale (0-500), not raw concentrations.

## AX compliance

This tool follows [Agent Experience principles](https://evoleinik.com/posts/vx-launch/):

- Structured output: `--json` on every command, bare arrays
- stdout = data, stderr = diagnostics
- No interactive prompts
- Deterministic exit codes: 0=ok, 1=user error, 2=network, 3=decode
- `--timeout` on all network operations
- Guides on empty results (stderr hints with concrete flags)
- Usage telemetry: `aqi-liberator usage`

## License

MIT
