# Latency Report

Turns analysed: **90** across **5** profile(s)


## Headline: median and p95 response latency

| Profile | n | median e2e | p95 e2e | mean | min | max |
|---|---|---|---|---|---|---|
| `v1_baseline` | 20 | **9563 ms** | **14615 ms** | 9917 | 5077 | 18102 |
| `v2_fast_llm` | 20 | **8340 ms** | **14388 ms** | 9261 | 4596 | 17453 |
| `v3_stream_llm_tts` | 20 | **5358 ms** | **10896 ms** | 6294 | 4167 | 11579 |
| `v4_stream_stt` | 13 | **4158 ms** | **6862 ms** | 4556 | 3113 | 7353 |
| `v5_full` | 17 | **3439 ms** | **4503 ms** | 3521 | 2563 | 4545 |

## Per-stage latency (median / p95, ms)

| Stage | `v1_baseline` | `v2_fast_llm` | `v3_stream_llm_tts` | `v4_stream_stt` | `v5_full` |
|---|---|---|---|---|---|
| End-of-speech detection | 1422 / 1506 | 1416 / 1497 | 1413 / 1505 | 1421 / 1503 | 815 / 1638 |
| Speech-to-text | 1974 / 7726 | 2114 / 8537 | 2170 / 8102 | 296 / 751 | 0 / 0 |
| LLM time-to-first-token | 561 / 1485 | 410 / 522 | 422 / 571 | 439 / 613 | 416 / 611 |
| LLM total response | 561 / 1485 | 410 / 522 | 480 / 600 | 488 / 692 | 468 / 665 |
| TTS time-to-first-audio | 359 / 469 | 356 / 390 | 338 / 376 | 985 / 1829 | 942 / 1007 |
| END-TO-END (perceived) | 9563 / 14615 | 8340 / 14388 | 5358 / 10896 | 4158 / 6862 | 3439 / 4503 |

## Where the time goes (critical path, median ms)

These segments are contiguous and non-overlapping, so they sum to e2e.

| Segment | `v1_baseline` | `v2_fast_llm` | `v3_stream_llm_tts` | `v4_stream_stt` | `v5_full` |
|---|---|---|---|---|---|
| eot detection | 1422 | 1416 | 1413 | 1421 | 815 |
| stt finalize | 1974 | 2114 | 2170 | 296 | 0 |
| llm dispatch | 0 | 0 | 0 | 0 | 4 |
| llm ttft | 561 | 410 | 422 | 439 | 416 |
| text buffering | 0 | 0 | 13 | 11 | 11 |
| tts ttfa | 359 | 356 | 338 | 985 | 942 |
| audio to speaker | 3448 | 4070 | 865 | 1304 | 1322 |
| **total** | **7764** | **8365** | **5221** | **4456** | **3510** |

## Before vs after

| | `v1_baseline` (before) | `v5_full` (after) | change |
|---|---|---|---|
| median | 9563 ms | 3439 ms | **-6124 ms (64% faster)** |
| p95 | 14615 ms | 4503 ms | **-10113 ms (69% faster)** |
