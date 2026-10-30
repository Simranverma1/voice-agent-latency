"""Hardening tests: access code, session cap, idle timeout, misconfiguration,
bad provider keys, utterance cap and turn limit.

    python -m web.test_limits            # free checks only
    python -m web.test_limits --live     # + 2 live turns (Deepgram, Groq credit)

Each scenario starts its own uvicorn on a spare port with lowered limits
(MAX_SESSIONS=2, MAX_IDLE_S=3, MAX_UTTERANCE_S=2, MAX_TURNS=1), so every
limit is exercised without spending 20 turns of API credit.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

from agent.config import MIC_BLOCK_BYTES, MIC_BLOCK_MS

from .test_client import DATA, load_blocks

CODE = "test-code"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


class Server:
    def __init__(self, port: int, **env: str):
        self.port = port
        self.env = {**os.environ, "ACCESS_CODE": CODE, "PYTHONUNBUFFERED": "1", **env}

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "web.server:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                self.health()
                return self
            except OSError:
                time.sleep(0.25)
        raise RuntimeError(f"server on {self.port} did not start")

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=10)

    def health(self) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
            return json.loads(r.read())

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"


async def open_session(url: str, code: str | None = CODE):
    ws = await websockets.connect(url, max_size=None)
    if code is not None:
        await ws.send(json.dumps({"type": "hello", "code": code}))
    return ws


async def next_event(ws, timeout: float = 15.0) -> dict:
    """Next JSON event, skipping audio frames."""
    deadline = time.monotonic() + timeout
    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
        if isinstance(msg, str):
            return json.loads(msg)


async def expect_refusal(url: str, code, want: str, name: str, send_binary_first: bool = False):
    ws = await websockets.connect(url, max_size=None)
    if send_binary_first:
        await ws.send(b"\x00" * 640)
    elif code is not None:
        await ws.send(json.dumps({"type": "hello", "code": code}))
    ev = await next_event(ws)
    try:
        await asyncio.wait_for(ws.recv(), timeout=5)
        closed = False
    except websockets.ConnectionClosed as e:
        closed = True
        close_code = e.rcvd.code if e.rcvd else None
    check(name, ev.get("code") == want and closed,
          f"(got {ev.get('code')!r}: {ev.get('message')!r}; closed={closed}"
          f"{', close code ' + str(close_code) if closed else ''})")


async def stream_prompt(ws, prompt: int, events: list[dict], until: set[str], timeout: float = 45.0):
    """Wait for 'listening', stream a prompt in real time, then keep sending
    silence while collecting events until one of `until` arrives."""
    while True:
        ev = await next_event(ws, timeout)
        events.append(ev)
        if ev["type"] == "listening":
            break
    blocks = load_blocks(DATA / f"prompt_{prompt:02d}.wav")
    silence = b"\x00" * MIC_BLOCK_BYTES
    stream = [silence] * (600 // MIC_BLOCK_MS) + blocks
    done = asyncio.Event()

    async def collect():
        try:
            while not done.is_set():
                msg = await ws.recv()
                if isinstance(msg, str):
                    ev = json.loads(msg)
                    events.append(ev)
                    if ev["type"] in until or ev.get("code") in until:
                        done.set()
        except websockets.ConnectionClosed:
            done.set()

    rx = asyncio.create_task(collect())
    t_next = time.monotonic()
    deadline = t_next + timeout
    i = 0
    while not done.is_set() and time.monotonic() < deadline:
        with_block = stream[i] if i < len(stream) else silence
        try:
            await ws.send(with_block)
        except websockets.ConnectionClosed:
            break
        i += 1
        t_next += MIC_BLOCK_MS / 1000
        await asyncio.sleep(max(0.0, t_next - time.monotonic()))
    done.set()
    rx.cancel()


async def free_checks():
    with Server(8101, MAX_SESSIONS="2", MAX_IDLE_S="3") as srv:
        h = srv.health()
        check("/health answers", h.get("ok") is True and h.get("sessions") == 0, f"({h})")
        await expect_refusal(srv.url, "wrong-code", "bad_code", "wrong access code is refused")
        await expect_refusal(srv.url, None, "bad_hello", "audio before hello is refused", send_binary_first=True)

        a = await open_session(srv.url)
        b = await open_session(srv.url)
        ra, rb = await next_event(a), await next_event(b)
        check("two sessions are accepted", ra["type"] == "ready" and rb["type"] == "ready",
              f"({ra['type']}, {rb['type']}; health {srv.health()})")
        await expect_refusal(srv.url, CODE, "busy", "a third session is refused while two are open")
        await a.close()
        await asyncio.sleep(1.0)
        h1 = srv.health()["sessions"]
        await b.close()
        await asyncio.sleep(1.0)
        h0 = srv.health()["sessions"]
        check("closing sessions frees their slots", h1 == 1 and h0 == 0, f"(after 1 close: {h1}, after 2: {h0})")

        c = await open_session(srv.url)
        t0 = time.monotonic()
        events = []
        try:
            while True:
                events.append(await next_event(c, timeout=20))
        except websockets.ConnectionClosed:
            pass
        idle = [e for e in events if e.get("code") == "idle"]
        await asyncio.sleep(1.0)
        check("a silent session is closed after MAX_IDLE_S",
              bool(idle) and srv.health()["sessions"] == 0,
              f"(closed after {time.monotonic() - t0:.1f} s with MAX_IDLE_S=3; message {idle[0]['message']!r})"
              if idle else f"(events: {events})")

    with Server(8102, ACCESS_CODE="") as srv:
        await expect_refusal(srv.url, "", "server_misconfigured", "no ACCESS_CODE on the server = closed to everyone")

    with Server(8103, DEEPGRAM_API_KEY="invalid-key") as srv:
        ws = await open_session(srv.url)
        events = []
        try:
            while True:
                events.append(await next_event(ws, timeout=20))
        except websockets.ConnectionClosed:
            pass
        err = [e for e in events if e["type"] == "error"]
        check("invalid Deepgram key gives a friendly, final error",
              bool(err) and err[0]["code"] == "server_misconfigured" and "speech service" in err[0]["message"],
              f"({err[0]['message']!r})" if err else f"(events: {events})")
        await asyncio.sleep(1.0)
        check("...and the slot is freed", srv.health()["sessions"] == 0, f"({srv.health()})")


async def live_checks():
    # One live turn exercises both the utterance cap and the turn limit.
    with Server(8104, MAX_UTTERANCE_S="2", MAX_TURNS="1") as srv:
        ws = await open_session(srv.url)
        events: list[dict] = []
        await stream_prompt(ws, 2, events, until={"turn_limit"})
        capped = [e for e in events if e.get("code") == "utterance_capped"]
        transcript = [e["text"] for e in events if e["type"] == "transcript"]
        replies = [e["text"] for e in events if e["type"] == "reply"]
        limit = [e for e in events if e.get("code") == "turn_limit"]
        check("utterance longer than MAX_UTTERANCE_S is cut off with a notice",
              bool(capped) and bool(transcript),
              f"(heard only: {transcript[0]!r})" if transcript else f"(events: {[e['type'] for e in events]})")
        check("the capped turn still gets a reply", bool(replies), f"({replies[0][:60]!r}…)" if replies else "")
        check("session ends after MAX_TURNS turns", bool(limit), f"({limit[0]['message']!r})" if limit else "")
        await ws.close()

    with Server(8105, GROQ_API_KEY="invalid-key") as srv:
        ws = await open_session(srv.url)
        events = []
        await stream_prompt(ws, 1, events, until={"server_misconfigured", "turn_failed"})
        err = [e for e in events if e["type"] == "error"]
        transcript = [e["text"] for e in events if e["type"] == "transcript"]
        check("invalid Groq key: user is heard, then a friendly final error",
              bool(transcript) and bool(err) and err[0]["code"] == "server_misconfigured"
              and "language model" in err[0]["message"],
              f"(heard {transcript[0]!r}; {err[0]['message']!r})" if err and transcript else f"(events: {events})")
        await ws.close()


async def main(live: bool) -> int:
    await free_checks()
    if live:
        await live_checks()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="also run the 2 checks that spend API credit")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main(ap.parse_args().live)))
