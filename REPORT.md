# Voice Agent — Latency Report

## 1. Setup

- **Machine / OS:** Windows 10, Python 3.14, laptop mic (Realtek/WDM-KS input, downsampled 48 kHz → 16 kHz with a windowed-sinc anti-aliasing filter).
- **Network:** User is located in India; both providers (Groq, Deepgram) are US-hosted. Initial connectivity checks measured ~450 ms for a minimal Groq completion, ~1360 ms for a Deepgram streaming-websocket handshake, and ~3160 ms for a full Deepgram TTS round trip — all before any pipeline work begins. This RTT is baked into every number below; it is not a code artifact.
- **Providers:** Groq (`openai/gpt-oss-120b` / `openai/gpt-oss-20b`) for the LLM, Deepgram (STT `nova-3`, TTS `aura-2-thalia-en`) for speech. OpenAI was originally planned as the "before" baseline (Whisper + GPT-4o + tts-1) but was dropped entirely: the account had no billing credits and no free tier was available, so every profile — including the baseline — had to run on Groq + Deepgram, both usable on free-tier keys. The baseline's "slow LLM" is therefore the larger `gpt-oss-120b` on Groq rather than a different vendor (see §5 and §6).
- **Test set:** 20 fixed utterances, recorded with a real human voice (not synthetic), replayed identically into every profile via `WavSource` so all five profiles see byte-identical audio.
- **Turns measured:** 90 successful turns out of 100 attempted (20 prompts × 5 profiles). The 10 failures were transient network errors (timeouts, empty transcripts from a dropped streaming connection) under real conditions — see §6.
- **Clock:** `time.perf_counter()`, one monotonic event timeline per turn; every metric is derived by subtraction from that single timeline.
- **Zero point:** the last audio block containing real signal in the source recording (`speech_end_true`), not when the recording file ends.

## 2. Headline result

| | Before (`v1_baseline`) | After (`v5_full`) | Change |
|---|---|---|---|
| Median e2e | 9563 ms | 3439 ms | **−6124 ms (64% faster)** |
| p95 e2e | 14615 ms | 4503 ms | **−10113 ms (69% faster)** |


## 3. Full per-stage measurements

Turns analysed: **90** across **5** profiles (`n` = successful turns per profile, out of 20 attempted).

| Profile | n | median e2e | p95 e2e | mean | min | max |
|---|---|---|---|---|---|---|
| `v1_baseline` | 20 | **9563 ms** | **14615 ms** | 9917 | 5077 | 18102 |
| `v2_fast_llm` | 20 | **8340 ms** | **14388 ms** | 9261 | 4596 | 17453 |
| `v3_stream_llm_tts` | 20 | **5358 ms** | **10896 ms** | 6294 | 4167 | 11579 |
| `v4_stream_stt` | 13 | **4158 ms** | **6862 ms** | 4556 | 3113 | 7353 |
| `v5_full` | 17 | **3439 ms** | **4503 ms** | 3521 | 2563 | 4545 |

Per-stage latency (median / p95, ms) — these do **not** sum to e2e in the streaming profiles because stages overlap; see §4 for the non-overlapping breakdown.

| Stage | `v1_baseline` | `v2_fast_llm` | `v3_stream_llm_tts` | `v4_stream_stt` | `v5_full` |
|---|---|---|---|---|---|
| End-of-speech detection | 1422 / 1506 | 1416 / 1497 | 1413 / 1505 | 1421 / 1503 | 815 / 1638 |
| Speech-to-text | 1974 / 7726 | 2114 / 8537 | 2170 / 8102 | 296 / 751 | 0 / 0 |
| LLM time-to-first-token | 561 / 1485 | 410 / 522 | 422 / 571 | 439 / 613 | 416 / 611 |
| LLM total response | 561 / 1485 | 410 / 522 | 480 / 600 | 488 / 692 | 468 / 665 |
| TTS time-to-first-audio | 359 / 469 | 356 / 390 | 338 / 376 | 985 / 1829 | 942 / 1007 |
| **END-TO-END (perceived)** | **9563 / 14615** | **8340 / 14388** | **5358 / 10896** | **4158 / 6862** | **3439 / 4503** |

STT drops from a ~2000 ms median for batch requests (v1-v3) to 0 ms for streaming (v4/v5) — the transcript is effectively already in hand by the time the turn ends, because audio was uploaded continuously while the user was still talking, rather than uploaded as one blob only after they stopped.

## 4. Where the time goes

![breakdown](reports/breakdown.png)

**Note on reading this chart:** in the streaming profiles, LLM generation, TTS synthesis, and playback overlap. The bars show the *critical path* — seven contiguous, non-overlapping segments — not the raw per-stage timings above, which would double-count overlapping work. Within any single turn the seven segments sum exactly to that turn's e2e (checked across all 90 turns in `logs/bench.jsonl`: the largest discrepancy is 0.2 ms, which is rounding). Each bar segment, however, is that segment's **median across turns**, and medians don't add: the sum of the segment medians is close to the profile's median e2e but not equal to it (v1: 7764 vs 9563 ms; v5: 3510 vs 3439 ms). Read the bars as "the typical share of each segment", not as a decomposition of the headline median.

Baseline (`v1_baseline`) critical path (median of each segment):

| Segment | ms | % of total |
|---|---|---|
| end-of-turn detection | 1422 | 18.3% |
| STT finalize | 1974 | 25.4% |
| LLM dispatch gap | 0 | 0.0% |
| LLM TTFT | 561 | 7.2% |
| text buffering | 0 | 0.0% |
| TTS TTFA | 359 | 4.6% |
| **audio to speaker** | **3448** | **44.4%** |
| sum of segment medians | 7764 | 100% |

The percentages are shares of that 7764 ms sum, not of the 9563 ms median e2e. The gap exists because slow turns are slow in *different* segments: of the 10 slowest v1 turns, 6 are dominated by a slow batch STT request (5.5–9.4 s, vs a 2.0 s median) and 4 by a long audio-to-speaker wait (6.0–14.0 s, vs a 3.4 s median). Each segment's median ignores the other group's outliers, so the summed medians understate the median total.

`audio to speaker` — the time between the first TTS byte arriving and playback actually starting — dominates the baseline. That is not audio-hardware latency; it is the direct consequence of Path A's design: "nothing plays until the whole file exists" (per `pipeline.py`'s baseline branch). The LLM finishes generating and TTS starts synthesizing quickly (TTFT 561 ms, TTS TTFA 359 ms), but the reply then has to be *fully* downloaded and assembled before a single sample reaches the speaker.

## 5. The optimizations, one at a time

| Step | Change | Median e2e | Δ from previous |
|---|---|---|---|
| v1 | Baseline: fully sequential, nothing streams, larger/slower LLM (`gpt-oss-120b`) | 9563 ms | — |
| v2 | Swap to the smaller/faster LLM (`gpt-oss-20b`), still non-streaming | 8340 ms | **−1223 ms** |
| v3 | Stream LLM tokens → sentence-chunk → streaming TTS, with chunk requests pipelined (see below) | 5358 ms | **−2982 ms** |
| v4 | Streaming STT (Deepgram `nova-3` websocket) | 4158 ms | **−1200 ms** |
| v5 | Server-side endpointing @ 300 ms instead of a 1500 ms fixed-silence timer | 3439 ms | **−719 ms** |

### The single biggest win: eliminating the wait for the full reply before playing anything (v2 → v3)

The baseline waits for the LLM to finish generating the entire reply, then for TTS to synthesize and download the entire audio file, before playing a single sample. Since the user only perceives latency up to the **first** sound, all of that remaining wall-clock time — 4070 ms of median `audio to speaker` wait in v2 — was pure waste. Streaming LLM tokens into a sentence chunker (flush the first chunk at the earliest clause boundary past 24 characters, later chunks at 90 characters) and dispatching each chunk to TTS as it becomes available collapses that segment to under a second: audio starts playing after roughly the first clause instead of after the full reply. This single change accounts for the large majority of the 2982 ms drop from v2 to v3, and — critically — it required no faster model and no different vendor. It is purely a scheduling change, which is why it is the highest-leverage optimization in this entire ablation.

**One refinement made during testing, not just the initial design:** the first version of chunked TTS dispatched each chunk's request only after the *previous* chunk's audio had fully arrived. On a fast connection that gap between chunks is inaudible; on this project's high-RTT link, a short chunk could finish playing before the next chunk's TTS request had even received its first byte back — the speaker would go silent mid-sentence. Fixed by dispatching each chunk's TTS request the instant its text is ready, so requests overlap in flight, while a separate feeder still plays the resulting audio back in the original order. This is exactly the "prefetch chunk N+1 while chunk N plays" mitigation this report originally flagged as a further improvement (§6) — it's implemented now, not just proposed, and it's part of why v5's p95 (4503 ms) sits only ~1 s above its median (3439 ms), versus ~5 s for v1 (14615 vs 9563 ms).


## 6. Honest limitations

- **No OpenAI baseline was possible.** The originally intended "before" (Whisper + GPT-4o + OpenAI TTS) required paid API credits that weren't available; every profile, including v1, runs on free-tier Groq + Deepgram. The before/after story is real (64% median improvement) but it is an architecture comparison (sequential/batch vs. streaming/chunked/fast-endpointed), not a cross-vendor comparison.
- **v4 and v5's sample sizes are smaller than v1-v3's** (13/20 and 17/20 successful vs. a clean 20/20), specifically because streaming STT's persistent connection is more exposed to network flakiness than a fresh request per turn (see §6). This is itself a measured finding about the reliability trade-off of that optimization, not just noise to discount.
- **TTS time-to-first-audio is consistently higher for v4/v5 (~940-1000 ms) than v1-v3 (~340-480 ms)**, reproduced across multiple independent runs. The likely explanation is that v1-v3's batch STT request and TTS request both go over the same shared HTTP connection pool to `api.deepgram.com`, so TTS benefits from a connection already warmed by STT moments earlier in the same turn; v4/v5's STT instead uses a separate raw websocket that doesn't warm that pool. An equally plausible confound: v4 and v5 always run last in the benchmark sequence, so cumulative provider load over a ~30 minute session could contribute too. I have not isolated which effect dominates — flagging this as an open question rather than a settled explanation.
- **Single network location, single measurement window.** All 100 turns were collected in one extended session from one Indian ISP connection to US-hosted providers; provider load and route quality both vary by time of day and were not controlled for.
- **Recordings are clean.** 20 real (not synthetic) recorded utterances, but with no background noise, accents, or crosstalk in the test set.
- **Playback start is measured at the first write to the sound card**; true acoustic latency adds a further ~10-40 ms of OS buffering and DAC delay not instrumented here.
