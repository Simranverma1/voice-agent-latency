# Latency Report

Turns analysed: **88** across **5** profile(s)


## Headline: median and p95 response latency

| Profile | n | median e2e | p95 e2e | mean | min | max |
|---|---|---|---|---|---|---|
| `v1_baseline` | 19 | **8851 ms** | **15231 ms** | 10143 | 4874 | 16374 |
| `v2_fast_llm` | 18 | **10287 ms** | **22960 ms** | 11524 | 6166 | 24786 |
| `v3_stream_llm_tts` | 17 | **4412 ms** | **22818 ms** | 7977 | 3402 | 26517 |
| `v4_stream_stt` | 17 | **2942 ms** | **7004 ms** | 3798 | 1843 | 17982 |
| `v5_full` | 17 | **2244 ms** | **7133 ms** | 3523 | 1644 | 21258 |

## Per-stage latency (median / p95, ms)

| Stage | `v1_baseline` | `v2_fast_llm` | `v3_stream_llm_tts` | `v4_stream_stt` | `v5_full` |
|---|---|---|---|---|---|
| End-of-speech detection | 1412 / 1496 | 1414 / 1497 | 1420 / 1499 | 1427 / 1507 | 833 / 1645 |
| Speech-to-text | 2538 / 8177 | 3095 / 17641 | 2521 / 21190 | 321 / 436 | 0 / 0 |
| LLM time-to-first-token | 408 / 696 | 405 / 1095 | 402 / 517 | 410 / 504 | 409 / 553 |
| LLM total response | 408 / 696 | 405 / 1095 | 444 / 585 | 455 / 633 | 437 / 632 |
| TTS time-to-first-audio | 371 / 609 | 343 / 403 | 350 / 416 | 1016 / 5353 | 964 / 5725 |
| END-TO-END (perceived) | 8851 / 15231 | 10287 / 22960 | 4412 / 22818 | 2942 / 7004 | 2244 / 7133 |

## Where the time goes (critical path, median ms)

These segments are contiguous and non-overlapping, so they sum to e2e.

| Segment | `v1_baseline` | `v2_fast_llm` | `v3_stream_llm_tts` | `v4_stream_stt` | `v5_full` |
|---|---|---|---|---|---|
| eot detection | 1412 | 1414 | 1420 | 1427 | 833 |
| stt finalize | 2538 | 3095 | 2521 | 321 | 0 |
| llm dispatch | 0 | 0 | 0 | 0 | 1 |
| llm ttft | 408 | 405 | 402 | 410 | 409 |
| text buffering | 0 | 0 | 11 | 9 | 11 |
| tts ttfa | 371 | 343 | 350 | 1016 | 964 |
| audio to speaker | 4004 | 3175 | 0 | 0 | 0 |
| **total** | **8734** | **8432** | **4703** | **3184** | **2218** |

## Before vs after

| | `v1_baseline` (before) | `v5_full` (after) | change |
|---|---|---|---|
| median | 8851 ms | 2244 ms | **-6606 ms (75% faster)** |
| p95 | 15231 ms | 7133 ms | **-8098 ms (53% faster)** |
