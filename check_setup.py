"""Setup checker. Run this before anything else.

    python check_setup.py
"""
import asyncio
import sys
import time

PASS, FAIL = "[PASS]", "[FAIL]"
results = []


def report(ok, label, detail=""):
    results.append(ok)
    print(f"{PASS if ok else FAIL} {label}" + (f"  -> {detail}" if detail else ""))
    return ok


def check_python():
    v = sys.version_info
    ok = v.major == 3 and v.minor >= 9
    return report(ok, f"Python {v.major}.{v.minor}.{v.micro}",
                  "" if ok else "install Python 3.11+ from python.org")


def check_packages():
    missing = []
    for m in ["httpx", "websockets", "sounddevice", "numpy", "pandas", "matplotlib", "dotenv"]:
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    return report(not missing, "Packages installed",
                  "" if not missing else f"missing {missing} -> pip install -r requirements.txt")


def check_keys():
    from agent.config import DEEPGRAM_API_KEY, GROQ_API_KEY
    ok_all = True
    for name, val in [("GROQ_API_KEY", GROQ_API_KEY), ("DEEPGRAM_API_KEY", DEEPGRAM_API_KEY)]:
        ok = bool(val) and len(val) > 15 and not val.endswith("...")
        ok_all &= report(ok, f"{name} present",
                         "" if ok else "add it to .env (copy .env.example to .env)")
    return ok_all


def check_audio():
    import numpy as np
    import sounddevice as sd
    from agent.audio_io import MIC_DEVICE, MIC_NATIVE_RATE, DECIMATION, resample_to_16k

    try:
        devs = sd.query_devices()
    except Exception as e:
        return report(False, "Audio subsystem", str(e))

    ins = [d for d in devs if d["max_input_channels"] > 0]
    outs = [d for d in devs if d["max_output_channels"] > 0]
    report(bool(ins), f"Microphone found ({len(ins)} input device(s))")
    report(bool(outs), f"Speaker found ({len(outs)} output device(s))")
    print(f"       using MIC_DEVICE={MIC_DEVICE} at {MIC_NATIVE_RATE} Hz "
          f"-> /{DECIMATION} -> 16000 Hz")

    if outs:
        print("       playing a short beep...")
        try:
            t = np.linspace(0, 0.4, int(24000 * 0.4), endpoint=False)
            tone = (np.sin(2 * np.pi * 440 * t) * 0.25 * 32767).astype(np.int16)
            sd.play(tone, 24000); sd.wait()
            report(True, "Speaker playback")
        except Exception as e:
            report(False, "Speaker playback", str(e))

    print("       recording 1.5s - SAY SOMETHING NOW...")
    try:
        rec = sd.rec(int(MIC_NATIVE_RATE * 1.5), samplerate=MIC_NATIVE_RATE,
                     channels=1, dtype="int16", device=MIC_DEVICE)
        sd.wait()
        pcm16k = resample_to_16k(rec.tobytes(), MIC_NATIVE_RATE)
        a = np.frombuffer(pcm16k, dtype=np.int16).astype(np.float64)
        level = int(np.sqrt(np.mean(a * a)))
        ok = level > 200
        report(ok, f"Microphone capture + downsample (level {level})",
               "" if ok else "too quiet: raise mic volume in Windows Sound settings")
        if ok:
            print("       playing it back to you...")
            sd.play(np.frombuffer(pcm16k, dtype=np.int16), 16000); sd.wait()
    except Exception as e:
        report(False, "Microphone capture", f"{e}  -> run: python fix_mic.py")


async def check_apis():
    import httpx
    from agent.config import DEEPGRAM_API_KEY, GROQ_API_KEY, PROFILES

    groq_model = PROFILES["v5_full"].llm_model

    async with httpx.AsyncClient(timeout=30.0) as c:
        try:
            t0 = time.perf_counter()
            r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                             json={"model": groq_model, "max_tokens": 5,
                                   "messages": [{"role": "user", "content": "say hi"}]})
            ok = r.status_code == 200
            detail = (r.json()["choices"][0]["message"]["content"][:30] if ok
                      else f"HTTP {r.status_code} {r.text[:150]}")
            report(ok, f"Groq LLM '{groq_model}' ({(time.perf_counter()-t0)*1000:.0f} ms)", detail)
        except Exception as e:
            report(False, "Groq LLM", str(e))

        try:
            t0 = time.perf_counter()
            r = await c.post("https://api.deepgram.com/v1/speak",
                             params={"model": "aura-2-thalia-en", "encoding": "linear16",
                                     "sample_rate": "24000"},
                             headers={"Authorization": f"Token {DEEPGRAM_API_KEY}",
                                      "Content-Type": "application/json"},
                             json={"text": "Setup check complete."})
            ok = r.status_code == 200 and len(r.content) > 1000
            report(ok, f"Deepgram TTS ({(time.perf_counter()-t0)*1000:.0f} ms, {len(r.content)} bytes)",
                   "" if ok else f"HTTP {r.status_code} {r.text[:150]}")
            if ok:
                try:
                    import numpy as np, sounddevice as sd
                    print("       playing synthesised speech...")
                    sd.play(np.frombuffer(r.content, dtype=np.int16), 24000); sd.wait()
                except Exception:
                    pass
        except Exception as e:
            report(False, "Deepgram TTS", str(e))

    try:
        from agent.stt import DeepgramStream
        dg = DeepgramStream()
        t0 = time.perf_counter()
        await dg.connect()
        ms = (time.perf_counter() - t0) * 1000
        await dg.close()
        report(True, f"Deepgram streaming websocket ({ms:.0f} ms handshake)")
    except Exception as e:
        report(False, "Deepgram streaming websocket", str(e))


async def main():
    print("\n" + "=" * 62)
    print("  VOICE AGENT - SETUP CHECK")
    print("=" * 62 + "\n")
    print("-- environment --")
    if not check_python() or not check_packages():
        return
    check_keys()
    print("\n-- audio --")
    check_audio()
    print("\n-- providers --")
    await check_apis()
    print("\n" + "=" * 62)
    failed = results.count(False)
    if failed == 0:
        print("  ALL CHECKS PASSED. Next: python make_prompts.py --mode record")
    else:
        print(f"  {failed} CHECK(S) FAILED. Fix the [FAIL] lines above, then re-run.")
    print("=" * 62 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
