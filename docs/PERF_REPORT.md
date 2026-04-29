# Perf Report

Generated: 2026-04-28T23:41:32
Base URL:  `http://127.0.0.1:5000`
Scenarios: cold, warm

## Summary

### Top 10 slowest endpoints (warm p95)

| Endpoint | p50 ms | p95 ms | p99 ms | max ms | body p50 | err % |
|---|---:|---:|---:|---:|---:|---:|
| `/trade-book` | 289.8 | 693.5 | 793.1 | 818.0 | 8,566 | 0.0 |
| `/` | 361.3 | 570.2 | 711.3 | 746.5 | 8,365 | 0.0 |
| `/limits` | 403.5 | 486.6 | 542.3 | 556.2 | 11,575 | 0.0 |
| `/orders` | 191.4 | 426.9 | 470.8 | 481.8 | 8,566 | 0.0 |
| `/history` | 207.1 | 287.9 | 331.8 | 342.7 | 13,139 | 0.0 |
| `/api/trades` | 213.2 | 282.4 | 320.8 | 330.4 | 3,083 | 0.0 |
| `/positions` | 128.5 | 235.2 | 446.3 | 499.1 | 8,576 | 0.0 |
| `/api/margin-summary` | 138.5 | 227.4 | 339.3 | 367.2 | 2,091 | 0.0 |
| `/api/recent-blocks` | 19.9 | 180.1 | 276.2 | 300.2 | 54 | 0.0 |
| `/api/future-prices` | 102.1 | 174.5 | 199.3 | 205.5 | 1,009 | 0.0 |

### WebSocket freshness

- Connected: **100.0%** of samples
- last_tick_age p50: **7.75 s** (p95 11.88, max 12.34)
- Sub-1s freshness: **0.0%** of samples
- Sub-2s freshness: **0.0%** of samples

### Data file sizes

- `data/blocked_attempts.jsonl` -> missing
- `data/audit.log` -> {'bytes': 3972, 'kb': 3.9, 'mb': 0.0, 'mtime': '2026-04-28T23:40:41'}
- `data/paper_ledger.json` -> missing
- `data/orders.jsonl` -> missing
- `data/trades.jsonl` -> missing

## Scenario: cold

| Endpoint | n | ok | err% | p50 | p95 | p99 | max | body p50 | err |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `/` | 1 | 1 | 0.0 | 473.0 | 473.0 | 473.0 | 473.0 | 8,365 |  |
| `/api/blocked-list` | 1 | 1 | 0.0 | 479.9 | 479.9 | 479.9 | 479.9 | 98 |  |
| `/api/blocked-list?page=1` | 1 | 1 | 0.0 | 105.5 | 105.5 | 105.5 | 105.5 | 98 |  |
| `/api/config` | 1 | 1 | 0.0 | 202.8 | 202.8 | 202.8 | 202.8 | 931 |  |
| `/api/feed-status` | 1 | 1 | 0.0 | 26.4 | 26.4 | 26.4 | 26.4 | 1,522 |  |
| `/api/future-prices` | 1 | 1 | 0.0 | 22.3 | 22.3 | 22.3 | 22.3 | 1,009 |  |
| `/api/gann-live` | 1 | 1 | 0.0 | 28.2 | 28.2 | 28.2 | 28.2 | 682 |  |
| `/api/gann-prices` | 1 | 1 | 0.0 | 19.2 | 19.2 | 19.2 | 19.2 | 3,626 |  |
| `/api/health` | 1 | 1 | 0.0 | 28.7 | 28.7 | 28.7 | 28.7 | 1,966 |  |
| `/api/margin-summary` | 1 | 1 | 0.0 | 372.0 | 372.0 | 372.0 | 372.0 | 2,091 |  |
| `/api/option-prices` | 1 | 1 | 0.0 | 17.4 | 17.4 | 17.4 | 17.4 | 89 |  |
| `/api/paper-trades-live` | 1 | 1 | 0.0 | 15.1 | 15.1 | 15.1 | 15.1 | 34 |  |
| `/api/recent-blocks` | 1 | 1 | 0.0 | 24.4 | 24.4 | 24.4 | 24.4 | 54 |  |
| `/api/snapshot-stats` | 1 | 1 | 0.0 | 17.9 | 17.9 | 17.9 | 17.9 | 352 |  |
| `/api/trades` | 1 | 1 | 0.0 | 6.2 | 6.2 | 6.2 | 6.2 | 3,083 |  |
| `/audit` | 1 | 1 | 0.0 | 76.9 | 76.9 | 76.9 | 76.9 | 16,647 |  |
| `/audit?page=1` | 1 | 1 | 0.0 | 188.8 | 188.8 | 188.8 | 188.8 | 16,647 |  |
| `/blockers` | 1 | 1 | 0.0 | 45.0 | 45.0 | 45.0 | 45.0 | 13,402 |  |
| `/blockers?page=1` | 1 | 1 | 0.0 | 118.3 | 118.3 | 118.3 | 118.3 | 13,402 |  |
| `/config` | 1 | 1 | 0.0 | 65.4 | 65.4 | 65.4 | 65.4 | 31,928 |  |
| `/futures` | 1 | 1 | 0.0 | 62.9 | 62.9 | 62.9 | 62.9 | 14,619 |  |
| `/gann` | 1 | 1 | 0.0 | 69.0 | 69.0 | 69.0 | 69.0 | 32,250 |  |
| `/history` | 1 | 1 | 0.0 | 414.8 | 414.8 | 414.8 | 414.8 | 13,139 |  |
| `/limits` | 1 | 1 | 0.0 | 510.6 | 510.6 | 510.6 | 510.6 | 11,575 |  |
| `/options` | 1 | 1 | 0.0 | 59.4 | 59.4 | 59.4 | 59.4 | 16,600 |  |
| `/orderlog` | 1 | 1 | 0.0 | 71.7 | 71.7 | 71.7 | 71.7 | 9,230 |  |
| `/orders` | 1 | 1 | 0.0 | 642.3 | 642.3 | 642.3 | 642.3 | 8,566 |  |
| `/paper-trades` | 1 | 1 | 0.0 | 98.8 | 98.8 | 98.8 | 98.8 | 11,993 |  |
| `/positions` | 1 | 1 | 0.0 | 124.2 | 124.2 | 124.2 | 124.2 | 8,576 |  |
| `/trade-book` | 1 | 1 | 0.0 | 435.9 | 435.9 | 435.9 | 435.9 | 8,566 |  |
| `/trades` | 1 | 1 | 0.0 | 93.3 | 93.3 | 93.3 | 93.3 | 13,903 |  |

## Scenario: warm

| Endpoint | n | ok | err% | p50 | p95 | p99 | max | body p50 | err |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `/` | 20 | 20 | 0.0 | 361.3 | 570.2 | 711.3 | 746.5 | 8,365 |  |
| `/api/blocked-list` | 20 | 20 | 0.0 | 15.7 | 50.4 | 86.6 | 95.6 | 98 |  |
| `/api/blocked-list?page=1` | 20 | 20 | 0.0 | 22.6 | 73.3 | 87.6 | 91.2 | 98 |  |
| `/api/config` | 20 | 20 | 0.0 | 15.7 | 22.3 | 27.4 | 28.7 | 931 |  |
| `/api/feed-status` | 20 | 20 | 0.0 | 15.3 | 24.9 | 25.5 | 25.6 | 1,521 |  |
| `/api/future-prices` | 20 | 20 | 0.0 | 102.1 | 174.5 | 199.3 | 205.5 | 1,009 |  |
| `/api/gann-live` | 20 | 20 | 0.0 | 16.2 | 25.5 | 25.9 | 26.0 | 691 |  |
| `/api/gann-prices` | 20 | 20 | 0.0 | 15.7 | 29.6 | 42.4 | 45.6 | 3,626 |  |
| `/api/health` | 20 | 20 | 0.0 | 15.6 | 29.0 | 31.3 | 31.9 | 1,975 |  |
| `/api/margin-summary` | 20 | 20 | 0.0 | 138.5 | 227.4 | 339.3 | 367.2 | 2,091 |  |
| `/api/option-prices` | 20 | 20 | 0.0 | 15.9 | 24.4 | 29.0 | 30.1 | 89 |  |
| `/api/paper-trades-live` | 20 | 20 | 0.0 | 25.5 | 158.8 | 171.8 | 175.1 | 34 |  |
| `/api/recent-blocks` | 20 | 20 | 0.0 | 19.9 | 180.1 | 276.2 | 300.2 | 54 |  |
| `/api/snapshot-stats` | 20 | 20 | 0.0 | 16.6 | 26.0 | 27.0 | 27.3 | 351 |  |
| `/api/trades` | 20 | 20 | 0.0 | 213.2 | 282.4 | 320.8 | 330.4 | 3,083 |  |
| `/audit` | 20 | 20 | 0.0 | 26.4 | 32.5 | 33.0 | 33.1 | 16,647 |  |
| `/audit?page=1` | 20 | 20 | 0.0 | 30.4 | 32.6 | 32.8 | 32.8 | 16,647 |  |
| `/blockers` | 20 | 20 | 0.0 | 15.9 | 30.3 | 31.8 | 32.2 | 13,402 |  |
| `/blockers?page=1` | 20 | 20 | 0.0 | 17.7 | 31.4 | 32.8 | 33.2 | 13,402 |  |
| `/config` | 20 | 20 | 0.0 | 29.5 | 31.6 | 31.6 | 31.6 | 31,928 |  |
| `/futures` | 20 | 20 | 0.0 | 16.2 | 154.8 | 515.0 | 605.1 | 14,619 |  |
| `/gann` | 20 | 20 | 0.0 | 15.4 | 23.4 | 27.5 | 28.6 | 32,250 |  |
| `/history` | 20 | 20 | 0.0 | 207.1 | 287.9 | 331.8 | 342.7 | 13,139 |  |
| `/limits` | 20 | 20 | 0.0 | 403.5 | 486.6 | 542.3 | 556.2 | 11,575 |  |
| `/options` | 20 | 20 | 0.0 | 16.6 | 26.3 | 27.4 | 27.6 | 16,600 |  |
| `/orderlog` | 20 | 20 | 0.0 | 21.3 | 31.6 | 32.1 | 32.3 | 9,230 |  |
| `/orders` | 20 | 20 | 0.0 | 191.4 | 426.9 | 470.8 | 481.8 | 8,566 |  |
| `/paper-trades` | 20 | 20 | 0.0 | 15.8 | 30.7 | 30.8 | 30.9 | 11,993 |  |
| `/positions` | 20 | 20 | 0.0 | 128.5 | 235.2 | 446.3 | 499.1 | 8,576 |  |
| `/trade-book` | 20 | 20 | 0.0 | 289.8 | 693.5 | 793.1 | 818.0 | 8,566 |  |
| `/trades` | 20 | 20 | 0.0 | 16.3 | 30.5 | 31.2 | 31.4 | 13,903 |  |

## Raw WebSocket samples (last 30s)

```json
[
  {
    "ts": 1777400011.64504,
    "lat_ms": 26.7,
    "connected": true,
    "last_tick_age": 3.111067056655884,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400012.6748195,
    "lat_ms": 28.8,
    "connected": true,
    "last_tick_age": 4.139824151992798,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400013.6916418,
    "lat_ms": 16.7,
    "connected": true,
    "last_tick_age": 5.157092809677124,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400014.753543,
    "lat_ms": 61.0,
    "connected": true,
    "last_tick_age": 6.186234474182129,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400015.7731075,
    "lat_ms": 18.4,
    "connected": true,
    "last_tick_age": 7.2367894649505615,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400016.7995281,
    "lat_ms": 28.9,
    "connected": true,
    "last_tick_age": 8.26653003692627,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400017.820116,
    "lat_ms": 17.8,
    "connected": true,
    "last_tick_age": 9.28492283821106,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400018.8342366,
    "lat_ms": 13.1,
    "connected": true,
    "last_tick_age": 10.299207925796509,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400019.8641975,
    "lat_ms": 28.9,
    "connected": true,
    "last_tick_age": 11.329183578491211,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  },
  {
    "ts": 1777400020.8702464,
    "lat_ms": 5.5,
    "connected": true,
    "last_tick_age": 12.335076332092285,
    "subs_index": 3,
    "subs_scrip": 6,
    "subs_option": 66,
    "subs_future": 3,
    "reconnects": 0,
    "errors": 0
  }
]
```
