# CLAUDE.md — Project context

Read this first. It carries over context from the chat session where this
project was built.

## What this project is

An interview assignment: build a low-latency real-time voice agent, instrument
the full pipeline, measure latency, make a meaningful optimization, and report
before/after numbers with reasoning.

Graders explicitly said they care **more about measurements and reasoning than
about achieving the lowest latency**. Do not sacrifice measurement clarity to
chase a smaller number.

Required deliverables: working demo, source code, latency measurements, median
and p95, breakdown of where time goes, before-and-after, explanation of the
optimization.

Interview questions to be ready for: largest bottleneck; why optimize one part
over another; behaviour under worse network conditions; how to reduce latency
further; what trade-offs the optimizations introduced.

## The user

Self-identified beginner. Explain things rather than just doing them. Give
exact commands with expected output. Do not assume familiarity with
virtualenvs, PATH, or audio stacks. They are on Windows and are the one running
every command — Claude cannot see their machine.

## Architecture

```
Mic → VAD/endpointing → STT → LLM → TTS → Speaker
```

One codebase, five profiles in `agent/config.py`. Every optimization is a
**flag, not a fork**, so the measurement code is identical across runs and
savings can be attributed to individual changes.

| Profile | Change |
|---|---|
| `v1_baseline` | Nothing streams. Whisper → GPT-4o → OpenAI TTS, sequential. |
| `v2_fast_llm` | Groq instead of OpenAI (still non-streaming) |
| `v3_stream_llm_tts` | Stream LLM tokens, sentence-chunk into streaming TTS |
| `v4_stream_stt` | Streaming STT via Deepgram websocket |
| `v5_full` | Server-side endpointing at 300 ms |

`v1` vs `v5` is the before/after. The intermediate steps are the ablation.

Providers: **Groq** (LLM, `openai/gpt-oss-20b`), **Deepgram** (streaming STT
nova-3 + Aura TTS), **OpenAI** (baseline Whisper + GPT-4o + tts-1).

## Measurement design — do not break this

Each turn records **one monotonic timeline** (`time.perf_counter`, never
`time.time`) in `agent/metrics.py`. All metrics are derived by subtraction so
they are internally consistent. First write to a mark wins, so
`llm_first_token` stays first.

```
eot_detection = eot_detected    - speech_end_true
stt           = stt_final       - eot_detected
llm_ttft      = llm_first_token - llm_request_sent
llm_total     = llm_last_token  - llm_request_sent
tts_ttfa      = tts_first_audio - tts_request_sent
e2e           = playback_start  - speech_end_true    <- headline number
```

**Critical distinction:** in streaming profiles the stages overlap, so
per-stage numbers do NOT sum to e2e. `Turn.critical_path()` computes seven
contiguous non-overlapping segments that DO sum to e2e. Use critical path for
"where is time going", per-stage for "how fast is each component". This
distinction is a key interview talking point — preserve it.

`speech_end_true` ground truth comes from `WavSource.speech_end_ts`, the last
audio block containing real signal.

## Reproducibility design — do not break this

`WavSource` replays 20 fixed WAVs into the pipeline in real time. Identical
audio goes through every profile, so differences are the pipeline, not the
speaker. Never switch the benchmark to live mic input.

`benchmark.py` resets `agent.history = []` between turns so each turn is
independent and comparable.

## Environment (already resolved — do not regress)

Windows. Python 3.14.

1. **Unpinned requirements.** Python 3.13/3.14 only have wheels for recent
   package versions. Pinning exact versions makes pip compile numpy from source
   and fail. Keep `requirements.txt` unpinned.

2. **No `audioop`.** Removed in Python 3.13 (PEP 594). All PCM maths is numpy.
   Never reintroduce `import audioop`.

3. **Sample rate.** The user's Realtek mic is only exposed via WDM-KS, which
   does no rate conversion and refuses 16 kHz. `audio_io.py` captures at
   `MIC_NATIVE_RATE` (48000) and decimates 3:1 to the 16 kHz that STT
   providers require. The windowed-sinc anti-aliasing filter before decimation
   is **not optional** — without it, content above 8 kHz aliases into the
   speech band. Filter state carries across blocks. Prefer 48000 over 44100:
   48000/16000 is exactly 3, 44100 needs fractional interpolation.

4. **Channel count.** WDM-KS refuses any channel count except the device's
   native one. `mic_channels()` queries it; `to_mono()` downmixes.

5. **Groq model drift.** `llama-3.3-70b-versatile` was deprecated and 404s.
   Now using `openai/gpt-oss-20b` — chosen over 120b because TTFT dominates
   perceived latency and replies are ~80 tokens of casual conversation. If it
   404s again, list available models via the Groq `/models` endpoint and update
   `agent/config.py`.

6. **matplotlib 3.11** renamed `boxplot(labels=)` to `tick_labels=`.
   `analyze.py` already uses the new name.

7. **High RTT.** User is in India, providers are US-hosted. Measured RTTs were
   1000–1900 ms on first contact. Expect e2e well above typical published
   figures. This is not a bug — it is good report material for the "worse
   network conditions" question.

## Current state

Setup verified end to end via `check_setup.py` — all checks passing on the
previous laptop. The user has since moved to a **new laptop** and is repeating
setup from scratch, so mic device index and native rate will need
re-discovering via `fix_mic.py`.

Not yet done: recording the 20 prompts, running the benchmark, filling in
`REPORT.md`.

## Files

```
agent/config.py      profiles — every optimization is a flag
agent/metrics.py     Turn timeline, metric derivation, JSONL logging
agent/audio_io.py    MicSource, WavSource, EnergyVAD, Downsampler, Player
agent/stt.py         Whisper batch, Deepgram batch, Deepgram websocket
agent/llm.py         OpenAI + Groq, streaming and non-streaming
agent/tts.py         OpenAI + Deepgram Aura, sentence chunker
agent/pipeline.py    orchestrator — runs every profile
check_setup.py       verify keys, audio, providers
fix_mic.py           microphone diagnostic
make_prompts.py      build the 20 fixed test utterances
import_recordings.py import phone recordings as prompts
demo.py              live microphone demo
benchmark.py         replay harness
analyze.py           stats + charts
REPORT.md            the writeup (template, needs filling in)
```

## Commands

```powershell
.\.venv\Scripts\Activate.ps1        # every new terminal
python check_setup.py
python fix_mic.py                   # if the mic check fails
python make_prompts.py --mode record
python demo.py --profile v5_full
python benchmark.py --profiles all
python analyze.py
```

## Conventions

- Python 3.10+ syntax; type hints on function signatures.
- Raw `httpx` and `websockets` rather than vendor SDKs, so the exact moment
  bytes arrive on the wire can be timestamped. Do not swap in SDKs.
- Never use `time.time()` for latency. Only `metrics.now()`.
- Comments explain *why*, not *what*.
- Secrets live in `.env`, which is gitignored. Never hardcode a key.

## What to be careful about

- Do not "simplify" the five profiles into two. The ablation is the point.
- Do not remove the anti-aliasing filter as an optimization. It is correctness.
- Do not add latency instrumentation that itself costs measurable time inside
  the hot path.
- If a benchmark turn fails, diagnose from `logs/bench.jsonl` — the full event
  timeline is preserved per turn, so metrics can be re-derived without re-running.
