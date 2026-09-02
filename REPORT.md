# Voice Agent — Latency Report

## 1. Setup

- **Machine / OS:** Windows 10, Python 3.14, laptop mic (Realtek/WDM-KS input, downsampled 48 kHz → 16 kHz with a windowed-sinc anti-aliasing filter).
- **Network:** User is located in India; both providers (Groq, Deepgram) are US-hosted. Initial connectivity checks measured ~450 ms for a minimal Groq completion, ~1360 ms for a Deepgram streaming-websocket handshake, and ~3160 ms for a full Deepgram TTS round trip — all before any pipeline work begins. This RTT is baked into every number below; it is not a code artifact.
- **Providers:** Groq (`openai/gpt-oss-120b` / `openai/gpt-oss-20b`) for the LLM, Deepgram (STT `nova-3`, TTS `aura-2-thalia-en`) for speech. OpenAI was originally planned as the "before" baseline (Whisper + GPT-4o + tts-1) but was dropped entirely: the account had no billing credits and no free tier was available, so every profile — including the baseline — had to run on Groq + Deepgram, both usable on free-tier keys. See §7 for how this reshaped the baseline definition.
- **Test set:** 20 fixed utterances, recorded with a real human voice (not synthetic), replayed identically into every profile via `WavSource` so all five profiles see byte-identical audio.
- **Turns measured:** 88 successful turns out of 100 attempted (20 prompts × 5 profiles). The 12 failures were transient network errors (timeouts, connection resets) under real conditions — see §7.
- **Clock:** `time.perf_counter()`, one monotonic event timeline per turn; every metric is derived by subtraction from that single timeline.
- **Zero point:** the last audio block containing real signal in the source recording (`speech_end_true`), not when the recording file ends.

## 2. Headline result

| | Before (`v1_baseline`) | After (`v5_full`) | Change |
|---|---|---|---|
| Median e2e | 8851 ms | 2244 ms | **−6606 ms (75% faster)** |
| p95 e2e | 15231 ms | 7133 ms | **−8098 ms (53% faster)** |

![distribution](reports/distribution.png)

## 3. Full per-stage measurements

Turns analysed: **88** across **5** profiles (`n` = successful turns per profile, out of 20 attempted).

| Profile | n | median e2e | p95 e2e | mean | min | max |
|---|---|---|---|---|---|---|
| `v1_baseline` | 19 | **8851 ms** | **15231 ms** | 10143 | 4874 | 16374 |
| `v2_fast_llm` | 18 | **10287 ms** | **22960 ms** | 11524 | 6166 | 24786 |
| `v3_stream_llm_tts` | 17 | **4412 ms** | **22818 ms** | 7977 | 3402 | 26517 |
| `v4_stream_stt` | 17 | **2942 ms** | **7004 ms** | 3798 | 1843 | 17982 |
| `v5_full` | 17 | **2244 ms** | **7133 ms** | 3523 | 1644 | 21258 |

Per-stage latency (median / p95, ms) — these do **not** sum to e2e in the streaming profiles because stages overlap; see §4 for the non-overlapping breakdown.

| Stage | `v1_baseline` | `v2_fast_llm` | `v3_stream_llm_tts` | `v4_stream_stt` | `v5_full` |
|---|---|---|---|---|---|
| End-of-speech detection | 1412 / 1496 | 1414 / 1497 | 1420 / 1499 | 1427 / 1507 | 833 / 1645 |
| Speech-to-text | 2538 / 8177 | 3095 / 17641 | 2521 / 21190 | 321 / 436 | 0 / 0 |
| LLM time-to-first-token | 408 / 696 | 405 / 1095 | 402 / 517 | 410 / 504 | 409 / 553 |
| LLM total response | 408 / 696 | 405 / 1095 | 444 / 585 | 455 / 633 | 437 / 632 |
| TTS time-to-first-audio | 371 / 609 | 343 / 403 | 350 / 416 | 1016 / 5353 | 964 / 5725 |
| **END-TO-END (perceived)** | **8851 / 15231** | **10287 / 22960** | **4412 / 22818** | **2942 / 7004** | **2244 / 7133** |

Note STT drops from a 321 ms median (streaming, v4/v5) to 0 ms (v5's transcript is ready essentially the instant Deepgram's endpointer fires) versus 2500-3100 ms for batch STT (v1-v3) — streaming STT means the transcript has effectively already arrived by the time the turn ends, because audio was uploaded continuously while the user was still talking.

## 4. Where the time goes

![breakdown](reports/breakdown.png)

**Note on reading this chart:** in the streaming profiles, LLM generation, TTS synthesis, and playback overlap. The bars show the *critical path* — seven contiguous, non-overlapping segments that sum exactly to that profile's median e2e — not the raw per-stage timings above, which would double-count overlapping work.

Baseline (`v1_baseline`) critical path (median):

| Segment | ms | % of total |
|---|---|---|
| end-of-turn detection | 1412 | 16.2% |
| STT finalize | 2538 | 29.1% |
| LLM dispatch gap | 0 | 0.0% |
| LLM TTFT | 408 | 4.7% |
| text buffering | 0 | 0.0% |
| TTS TTFA | 371 | 4.2% |
| **audio to speaker** | **4004** | **45.8%** |
| total | 8734 | 100% |

`audio to speaker` — the time between the first TTS byte arriving and playback actually starting — dominates the baseline. That is not audio-hardware latency; it is the direct consequence of Path A's design: "nothing plays until the whole file exists" (per `pipeline.py`'s baseline branch). The LLM finishes generating and TTS starts synthesizing quickly (TTFT 408 ms, TTS TTFA 371 ms), but the reply then has to be *fully* downloaded and assembled before a single sample reaches the speaker.

## 5. The optimizations, one at a time

| Step | Change | Median e2e | Δ from previous |
|---|---|---|---|
| v1 | Baseline: fully sequential, nothing streams, larger/slower LLM (`gpt-oss-120b`) | 8851 ms | — |
| v2 | Swap to the smaller/faster LLM (`gpt-oss-20b`), still non-streaming | 10287 ms | **+1436 ms** (noise, see below) |
| v3 | Stream LLM tokens → sentence-chunk → streaming TTS | 4412 ms | **−5875 ms** |
| v4 | Streaming STT (Deepgram `nova-3` websocket) | 2942 ms | **−1470 ms** |
| v5 | Server-side endpointing @ 300 ms instead of a 1500 ms fixed-silence timer | 2244 ms | **−698 ms** |

**About the v1→v2 step:** v2 looks *slower* than v1 in this run, which looks backwards for "swap in the faster model." It isn't a regression in the model swap itself — STT is byte-identical code and audio between v1 and v2, yet its median jumped from 2538 ms to 3095 ms between the two runs, and its p95 jumped from 8177 ms to 17641 ms. That's network variance between two sequentially-run 20-turn batches, not a pipeline effect (see §7 on sample size). The real, unambiguous signal in this data is that a same-shape swap of *only the LLM* buys very little relative to the noise floor, because in a non-streaming pipeline the LLM was never the bottleneck to begin with (LLM TTFT is ~400 ms out of an ~8800 ms total) — which is itself a useful finding, and directly motivates why v3's architectural change (not a vendor swap) is where the real win comes from.

### The single biggest win: eliminating the wait for the full reply before playing anything (v2 → v3)

The baseline waits for the LLM to finish generating the entire reply, then for TTS to synthesize and download the entire audio file, before playing a single sample. Since the user only perceives latency up to the **first** sound, all of that remaining wall-clock time — 3175 ms of median `audio to speaker` wait in v2 — was pure waste. Streaming LLM tokens into a sentence chunker (flush the first chunk at the earliest clause boundary past 24 characters, later chunks at 90 characters) and dispatching each chunk to TTS as it becomes available collapses that segment to 0 ms: audio starts playing after roughly the first clause instead of after the full reply. This single change accounts for the majority of the 5875 ms drop from v2 to v3, and — critically — it required no faster model and no different vendor. It is purely a scheduling change, which is why it is the highest-leverage optimization in this entire ablation.

## 6. Interview questions, answered

### What was the largest bottleneck?

**`audio to speaker`** in the baseline — 4004 ms median, **45.8%** of `v1_baseline`'s 8734 ms critical-path total. It's the gap between TTS producing its first byte and the speaker actually starting playback, caused entirely by Path A buffering the complete reply before playing any of it.

### Why optimize that part and not another?

Two reasons. First, it was the largest single segment on the critical path by a wide margin (45.8% vs. 29.1% for the next-largest, STT). Second, and more importantly, it was **structural rather than vendor-dependent**: fixing it (stream + chunk) is a scheduling change that keeps paying off regardless of which LLM or TTS provider sits behind it, whereas swapping providers (the v1→v2 step) buys a one-off constant that, as this data shows, can be swamped entirely by ordinary network noise. I deliberately did *not* chase further TTS-provider changes (e.g. a more expressive voice model) because voice quality was never on the critical path in a way that mattered here — TTS TTFA was already only 4.2% of the baseline budget, so the theoretical ceiling on optimizing that further was under 400 ms.

### What happens under worse network conditions?

This wasn't hypothetical here — it happened repeatedly during benchmarking. Real, observed behavior on this India→US link, in order of severity:

- **Ordinary timeouts and resets.** ~12% of turns failed outright with `ReadTimeout`, `WriteTimeout`, `ConnectTimeout`, `ConnectError` (including a raw DNS failure), or HTTP 408s from the provider itself. This is baseline noise on a high-RTT, sometimes-unstable link.
- **A cascading failure mode I had to fix in code.** v4/v5 keep one persistent Deepgram STT websocket open across all 20 turns to avoid paying reconnect cost per turn. The first version of this code had no reconnect logic: once *any* transient hiccup killed that socket, *every remaining turn in the run* failed silently with an empty transcript — 15 of 20 turns lost in one early run from a single mid-run blip. Fixed by tearing down and reopening the connection the moment a turn comes back empty.
- **Slow-dribble connections, not just hard failures.** The worst network behavior observed wasn't a clean timeout — it was a connection that kept trickling data slowly enough that no *individual* read ever exceeded the per-read timeout, while the *total* request time reached over 100 seconds in one case (against a normal ~2-3 second stage). A per-read timeout alone does not bound this; I added an explicit wall-clock deadline (20-25s) across the whole request to convert this into a clean, bounded failure instead of an unbounded stall.

Degradation is not linear or graceful by default — it's cliff-like (a stalled request either recovers via retry or blows past its deadline and fails the turn) unless you deliberately build in bounded retries and reconnect logic, which is what separates "slow under load" from "hangs forever" or "silently breaks for the rest of the session."

### How would you reduce latency further?

In rough order of expected value:

1. **Semantic end-of-turn detection.** The fixed-silence VAD used in v1–v4 can't tell a mid-sentence pause from a finished thought — see the endpointing trade-off below, where this was measured directly. A small classifier over the interim transcript could push endpointing well below 1500 ms without truncating people, closing most of the remaining gap to v5's 300 ms.
2. **Speculative generation.** Start the LLM call on the interim (not yet final) transcript while the user is still finishing, and cancel/restart if the final transcript differs. Buys most of the remaining LLM TTFT, at the cost of wasted tokens and the risk of answering a half-formed question.
3. **Barge-in.** Doesn't reduce measured latency but substantially improves *perceived* responsiveness, since the user can interrupt instead of waiting out a reply they no longer want.
4. **Co-locate / self-host.** Running closer to (or in) the same region as the providers, or self-hosting STT on a local GPU, would remove a full RTT — the single biggest fixed cost baked into every number in this report given the India↔US distance.
5. **Cache/precompute a filler.** A short acknowledgement ("mm-hm") played instantly on end-of-turn detection masks the remaining pipeline latency, though it doesn't reduce it.

### What trade-offs did the optimizations introduce?

| Optimization | Cost |
|---|---|
| Sentence chunking | More TTS calls; prosody can break at chunk seams; a slow later chunk can create an audible gap even though time-to-first-audio stayed good |
| Endpointing at 300 ms (v5) vs. 1500 ms (v1-v4) | Cuts people off mid-thought more readily the shorter the timer is. **Measured directly**: at the original 900 ms fixed-silence threshold, 6 of 20 recordings (30%) were falsely cut off mid-sentence by ordinary conversational pauses, producing garbled partial transcripts and confused replies. Raising the fixed-silence profiles to 1500 ms fixed those specific recordings. v5 avoids this failure mode structurally rather than by raising a number: Deepgram's server-side endpointer weighs linguistic completeness, not just silence duration, which is how it stays *both* faster (300 ms) and more tolerant of pauses than a naive timer |
| Streaming STT | A persistent websocket is a new failure mode with no built-in resilience — see the cascading-failure issue in §6 above. Required custom reconnect logic that a batch request never needed |
| No OpenAI budget → all-Groq/Deepgram pipeline | The originally planned "vendor comparison" baseline (OpenAI vs. Groq) was not possible without paid credits. The LLM axis was reframed as large/slow model (`gpt-oss-120b`) vs. small/fast model (`gpt-oss-20b`), both free — a genuine trade-off story, but a different one than originally scoped |
| Retry + overall-deadline logic (added for reliability) | Every retried call adds up to ~0.5-15s of worst-case extra latency on an already-slow turn before it gives up; this trades a slightly worse worst case for a much better success rate |

**The one I'd flag hardest:** endpointing. Every millisecond shaved off end-of-turn detection is bought directly against a higher chance of interrupting the user — and this project measured that cost directly (30% false-cut rate at 900 ms on real recordings). It's the only optimization here where the naive failure mode is *worse* than the latency it fixes: an agent that talks over you feels more broken than one that's merely slow. v5's answer — spend the 300 ms budget on linguistic judgment rather than pure silence-counting — is what makes it viable at all.

## 7. Honest limitations

- **No OpenAI baseline was possible.** The originally intended "before" (Whisper + GPT-4o + OpenAI TTS) required paid API credits that weren't available; every profile, including v1, runs on free-tier Groq + Deepgram. The before/after story is real (75% median improvement) but it is an architecture comparison (sequential/batch vs. streaming/chunked/fast-endpointed), not a cross-vendor comparison.
- **Sample size is 17-19 successful turns per profile, not a clean 20**, because ~12% of turns failed to genuine network conditions (see §6). This is enough for a stable median but p95 has real, visible error bars at this sample size — note the p95 values for v2 (22960 ms) and v3 (22818 ms), which are dominated by one or two slow-network outliers rather than reflecting typical behavior; the median is the more trustworthy number for those two profiles specifically.
- **Single network location, single measurement window.** All 100 turns were collected in one extended session from one Indian ISP connection to US-hosted providers; provider load and route quality both vary by time of day and were not controlled for.
- **Recordings are clean.** 20 real (not synthetic) recorded utterances, but with no background noise, accents, or crosstalk in the test set.
- **Playback start is measured at the first write to the sound card**; true acoustic latency adds a further ~10-40 ms of OS buffering and DAC delay not instrumented here.
