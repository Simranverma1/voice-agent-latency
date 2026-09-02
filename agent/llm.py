"""LLM backends.

Both OpenAI and Groq expose the same /chat/completions shape, so one httpx
implementation covers both. We use raw HTTP rather than the SDKs so that the
exact moment the first token arrives on the wire is ours to timestamp.
"""
import asyncio
import json
from typing import AsyncIterator

import httpx

from .config import GROQ_API_KEY, OPENAI_API_KEY, SYSTEM_PROMPT
from .metrics import now
from .net import TRANSIENT_STATUS, with_retry

# See tts.py: a per-read timeout doesn't bound total time if a connection
# dribbles data slowly. Cap wall-clock time across the whole stream.
OVERALL_DEADLINE_S = 20.0

ENDPOINTS = {
    "openai": ("https://api.openai.com/v1/chat/completions", lambda: OPENAI_API_KEY),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", lambda: GROQ_API_KEY),
}


def _headers(provider: str) -> dict:
    _, keyfn = ENDPOINTS[provider]
    return {"Authorization": f"Bearer {keyfn()}", "Content-Type": "application/json"}


def _body(model: str, history: list[dict], stream: bool, max_tokens: int, provider: str) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": 0.6,
    }
    if provider == "groq":
        # gpt-oss is a reasoning model: hidden reasoning tokens count against
        # max_tokens, and without a cap they can consume the whole budget and
        # leave nothing for the visible reply. "low" keeps replies short since
        # SYSTEM_PROMPT already asks for one or two spoken sentences.
        body["reasoning_effort"] = "low"
    return body


async def complete(provider: str, model: str, history: list[dict],
                   client: httpx.AsyncClient, max_tokens: int = 80) -> str:
    """Non-streaming. Nothing is usable until the whole reply is generated."""
    url, _ = ENDPOINTS[provider]

    async def _call():
        r = await client.post(url, headers=_headers(provider),
                              json=_body(model, history, False, max_tokens, provider), timeout=15.0)
        r.raise_for_status()
        return r

    r = await with_retry(_call)
    return r.json()["choices"][0]["message"]["content"].strip()


async def stream(provider: str, model: str, history: list[dict],
                 client: httpx.AsyncClient, max_tokens: int = 80) -> AsyncIterator[str]:
    """Yields token deltas as they arrive over SSE."""
    url, _ = ENDPOINTS[provider]
    attempts = 2
    for attempt in range(attempts):
        yielded_any = False
        start = now()
        try:
            async with client.stream("POST", url, headers=_headers(provider),
                                     json=_body(model, history, True, max_tokens, provider),
                                     timeout=15.0) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if now() - start > OVERALL_DEADLINE_S:
                        raise TimeoutError(f"LLM stream exceeded {OVERALL_DEADLINE_S}s overall")
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    piece = delta.get("content")
                    if piece:
                        yielded_any = True
                        yield piece
            return
        except httpx.HTTPStatusError as e:
            if yielded_any or attempt == attempts - 1 or e.response.status_code not in TRANSIENT_STATUS:
                raise
            await asyncio.sleep(0.5)
        except (httpx.TimeoutException, httpx.ConnectError, TimeoutError):
            if yielded_any or attempt == attempts - 1:
                raise
            await asyncio.sleep(0.5)
