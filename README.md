# Low-Latency Voice Agent — Task 1

A real-time spoken conversational agent, fully instrumented, with switchable
optimization profiles so every latency improvement can be attributed to a
specific change.

```
Mic ──► VAD / endpointing ──► STT ──► LLM ──► TTS ──► Speaker
        │                     │       │       │       │
        └── every arrow timestamped on one monotonic timeline ──┘
```

---

## What you are building

**One** codebase, **five** profiles. Each turns on one more optimization:

| Profile | What changes |
|---|---|
| `v1_baseline` | Nothing streams. Whisper → GPT-4o → OpenAI TTS, strictly sequential. |
| `v2_fast_llm` | Swap the LLM for Groq (still non-streaming) |
| `v3_stream_llm_tts` | Stream LLM tokens, sentence-chunk into streaming TTS |
| `v4_stream_stt` | Streaming STT — transcript ready the moment speech ends |
| `v5_full` | Server-side endpointing (300 ms) instead of a 900 ms silence timer |

`v1` vs `v5` is your before/after. The steps between are your ablation, which
is what makes the reasoning defensible in the interview.

Expect roughly a **3–5× reduction** in median perceived latency. Absolute
numbers depend heavily on your distance from the API providers — from India
expect ~3.5–5 s baseline and ~1.2–2 s optimized. That is fine, and the gap
matters far more than the absolute figure.

---

## Quick reference

```powershell
.\.venv\Scripts\Activate.ps1        # every new terminal
python check_setup.py               # verify keys, audio, providers
python fix_mic.py                   # only if the mic check fails
python make_prompts.py --mode record
python demo.py --profile v5_full    # the live demo
python benchmark.py --profiles all  # 100 instrumented turns
python analyze.py                   # tables + charts
```

---

## Step 1 — API keys (~15 min)

| Provider | Where | Used for | Cost |
|---|---|---|---|
| **Groq** | console.groq.com | Fast LLM | Free, no card |
| **Deepgram** | console.deepgram.com | Streaming STT + Aura TTS | $200 free credit, no card |
| **OpenAI** | platform.openai.com | Baseline Whisper + GPT-4o + TTS | Needs ~$5 on a card |

For each: sign up → find "API Keys" → create → **copy immediately** (shown once).

In OpenAI, do **Settings → Billing → add $5** first, or your key returns 401.
Set a $5 usage limit while you are there. The full benchmark costs well under
$2, nearly all of it the baseline profile.

---

## Step 2 — Python (~10 min)

```powershell
python --version
```

Anything **3.10 or newer** is fine, including 3.14. If it errors or opens the
Microsoft Store, install from python.org and **tick "Add python.exe to PATH"**
on the first installer screen. Close and reopen PowerShell afterwards.

Extract this project to your Desktop (right-click the zip → **Extract All**;
do not work inside the zip preview). Then open a terminal there: in File
Explorer, click the address bar, type `powershell`, press Enter.

```powershell
dir
```

You should see `agent`, `demo.py`, `check_setup.py`, `README.md`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`. That prefix disappears whenever
you close the terminal — re-run the activate line each session.

> Blocked by execution policy? Run once, answer `Y`:
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Versions in `requirements.txt` are deliberately **unpinned**. Newer Pythons
only have prebuilt wheels for recent package versions; pinning exact versions
forces pip to compile numpy from source, which fails without a C toolchain.

---

## Step 3 — Keys and mic config

```powershell
copy .env.example .env
notepad .env
```

Paste your three keys. Leave `MIC_DEVICE` blank for now.

---

## Step 4 — Verify everything (~3 min)

```powershell
python check_setup.py
```

It plays a beep, records you, plays it back, then pings all three providers.
Every line should read `[PASS]`.

### If the microphone check fails

```powershell
python fix_mic.py
```

This probes every input device at every sample rate, records from each, and
ends by telling you exactly what to put in `.env`:

```
      MIC_DEVICE=8
      MIC_NATIVE_RATE=48000
```

Put those in `.env` and re-run `check_setup.py`.

**Always prefer 48000 over 44100.** 48000 divides evenly by 16000, so
downsampling is a clean 3:1 decimation. 44100 needs fractional interpolation,
which introduces small timing errors — bad when you are measuring milliseconds.

Common mic problems:

| Symptom | Fix |
|---|---|
| Level under ~400 | Settings → System → Sound → Input → volume to 100 |
| `Invalid device` | `MIC_DEVICE` not set, or set to a nonexistent index |
| No devices at all | Settings → Privacy → Microphone → enable **"Let desktop apps access your microphone"** |
| Everything fails, and the mic also fails in Zoom/Teams | Driver problem. Device Manager → Audio inputs → right-click → Uninstall device → reboot. Or skip it: record on your phone (Step 5B). |

Avoid any device named **Stereo Mix** or **loopback** — those record your
speakers, so the agent will transcribe itself and loop forever.

---

## Step 5 — Build the 20 test prompts

**This is what separates a good submission from an average one.** If you speak
your prompts live on every run, each run has different audio, and your
before/after comparison partly measures *you*. Instead we fix 20 recordings and
replay them into the pipeline byte-for-byte identically for every profile.

### 5A — Record on this laptop

```powershell
python make_prompts.py --mode record
```

Press Enter → say the line → press Enter. Twenty times. Leave half a second of
silence at each end; it gets trimmed automatically. Watch the `level=` figure —
under 400 gets flagged, and you can delete that file and re-run to redo just it.

### 5B — Record on your phone

If the laptop mic is broken, this is the better path.

First install ffmpeg, then **close and reopen PowerShell** and reactivate:

```powershell
winget install --id Gyan.FFmpeg -e
```

Print the sentences to read:

```powershell
python -c "from make_prompts import PROMPTS; [print(f'{i:02d}. {p}') for i,p in enumerate(PROMPTS,1)]"
```

Record 20 separate voice memos, renaming each to `01`, `02` … `20`. Copy them
to a folder on the laptop (AirDrop, email, Drive, USB), then:

```powershell
python import_recordings.py --dir "C:\Users\you\Desktop\memos" --order name
```

Or record one long memo with ~2 s pauses between lines:

```powershell
python import_recordings.py --split "C:\Users\you\Desktop\all.m4a"
```

If the splitter finds the wrong number of segments it tells you which flag to
adjust (`--gap`, `--thresh`).

### 5C — Synthetic voices (30 seconds, no mic)

```powershell
python make_prompts.py --mode tts
```

Legitimate fallback. Say so in your report — synthetic speech is cleaner than
real speech, so your STT numbers will be slightly optimistic.

### Verify, whichever route you took

```powershell
python make_prompts.py --mode verify
```

Want `All 20 good.` Each clip 1.5–4 seconds.

---

## Step 6 — The live demo

**Wear headphones**, or the mic hears the agent and it talks to itself.

```powershell
python demo.py --profile v5_full
```

Then feel the difference:

```powershell
python demo.py --profile v1_baseline
```

**Screen-record both back to back.** That contrast is your demo deliverable and
is far more convincing than a table. Ctrl+C to quit.

---

## Step 7 — Benchmark

```powershell
python benchmark.py --profiles all
```

20 prompts × 5 profiles = 100 turns, roughly 25 minutes. Watch the
`-> N ok, M failed` line per profile; more than 2–3 failures means something is
wrong. Everything lands in `logs/bench.jsonl`.

Cheaper first pass — just before and after, ~10 min:

```powershell
python benchmark.py --profiles v1_baseline v5_full
```

---

## Step 8 — Results

```powershell
python analyze.py
```

Writes to `reports/`:

- **summary.md** — median/p95 tables, per-stage breakdown, before/after. Paste
  straight into your writeup.
- **per_turn.csv** — raw data
- **breakdown.png** — stacked critical-path bars
- **distribution.png** — box plots

Then fill in `REPORT.md`, which already has the interview answers structured.

---

## How the measurement works

Every turn records **one monotonic timeline** (`time.perf_counter`, never
`time.time`). All metrics are derived by subtraction, so they are internally
consistent by construction.

| Mark | Meaning |
|---|---|
| `speech_end_true` | Ground truth: last audio block containing real signal |
| `eot_detected` | When our system *decided* the turn ended |
| `stt_final` | Final transcript in hand |
| `llm_request_sent` | HTTP request dispatched |
| `llm_first_token` / `llm_last_token` | First / last token off the wire |
| `tts_request_sent` / `tts_first_audio` | TTS dispatched / first audio byte |
| `playback_start` | **First sample written to the sound card** |

The six required metrics:

```
end-of-speech detection = eot_detected      - speech_end_true
speech-to-text          = stt_final         - eot_detected
LLM time-to-first-token = llm_first_token   - llm_request_sent
LLM total response      = llm_last_token    - llm_request_sent
TTS time-to-first-audio = tts_first_audio   - tts_request_sent
END-TO-END (perceived)  = playback_start    - speech_end_true
```

### The subtlety that will come up in the interview

In a **streaming** pipeline the stages overlap: the LLM is still generating
while TTS is synthesising while the speaker is already playing. So the
per-stage numbers **do not sum to end-to-end** — adding them double-counts.

`metrics.py` therefore also computes a **critical path**: seven contiguous,
non-overlapping segments from `speech_end_true` to `playback_start` that *do*
sum exactly to e2e. Use the critical path for "where is the time going", and
per-stage numbers for "how fast is each component". Being able to explain that
distinction is worth a lot.

---

## Project layout

```
voice-agent/
├── agent/
│   ├── config.py       profiles — every optimization is a flag here
│   ├── metrics.py      Turn timeline, metric derivation, JSONL logging
│   ├── audio_io.py     MicSource, WavSource, VAD, downsampler, Player
│   ├── stt.py          Whisper batch, Deepgram batch, Deepgram websocket
│   ├── llm.py          OpenAI + Groq, streaming and non-streaming
│   ├── tts.py          OpenAI + Deepgram Aura, plus the sentence chunker
│   └── pipeline.py     orchestrator — runs every profile
├── check_setup.py      verify keys, audio, providers
├── fix_mic.py          microphone diagnostic
├── make_prompts.py     build the 20 fixed test utterances
├── import_recordings.py  import phone recordings
├── demo.py             live microphone demo
├── benchmark.py        replay harness
├── analyze.py          stats + charts
└── REPORT.md           your writeup
```

---

## Notes on environment quirks handled here

Worth mentioning in your report — they show you understood your own stack.

**No `audioop`.** Python 3.13 removed it (PEP 594). All PCM maths is numpy.

**Sample rate conversion.** Many Windows mics (Realtek via WDM-KS) refuse
16 kHz, which is what the STT providers need. `audio_io.py` captures at the
device's native rate and decimates to 16 kHz with a windowed-sinc
anti-aliasing filter whose state carries across block boundaries. Skipping the
filter would fold everything above 8 kHz back into the speech band as aliasing
and quietly degrade transcripts.

**Channel count.** WDM-KS devices refuse to open at any channel count other
than their native one, so we open at the hardware count and downmix to mono
ourselves.

**Model availability drifts.** Groq deprecated its Llama chat models; the
profiles use `openai/gpt-oss-20b`. If that 404s, list what your key can reach:

```powershell
python -c "import httpx,os;from dotenv import load_dotenv;load_dotenv();r=httpx.get('https://api.groq.com/openai/v1/models',headers={'Authorization':'Bearer '+os.getenv('GROQ_API_KEY')});[print(m['id']) for m in sorted(r.json()['data'],key=lambda x:x['id'])]"
```

then update `llm_model` in `agent/config.py`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError` | venv not active — `.\.venv\Scripts\Activate.ps1` |
| pip fails building numpy | Unpin versions (already done here) and upgrade pip |
| `Invalid number of channels` | Old `audio_io.py` — this version handles it |
| `no module named audioop` | Old file version; this one uses numpy |
| Groq 404 | Model deprecated — list models, update `config.py` |
| OpenAI 401 | No billing set up |
| Agent replies to itself | Use headphones, and never pick a Stereo Mix device |
| Turn 1 slower than the rest | TLS handshake. `_warm()` reduces it; note it or discard turn 1 |
| Deepgram websocket closes | Free-tier concurrency — lower `--repeat` |
