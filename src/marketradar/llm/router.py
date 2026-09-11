"""Route a prompt to the cheapest provider that can answer it.

Two tiers, per CLAUDE.md. Classification and extraction go to the fast cheap
models; the good model is for decks and narrative analysis and is never called
inside a loop over the market. This module is the cheap tier.

**Provider failure degrades, never crashes.** Six free tiers means six things
that can change without notice, so the router walks its list and the last
provider is local Ollama -- slower and weaker, but it has no quota and no
upstream to go down. Only an empty list or every provider failing raises.

**Which provider answered is part of the answer.** A number extracted by
``llama-3.3-70b`` on Groq and the same number extracted by ``qwen2.5:1.5b`` on a
laptop are not equally trustworthy, and a table that records only the number
cannot tell them apart later. :class:`Answer` carries the provider, the model and
the prompt version, and every consumer is expected to store them.

No new dependency: these are all OpenAI-compatible HTTP APIs except Ollama's
chat endpoint, and ``httpx`` is already here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """No provider could answer."""


@dataclass(frozen=True, slots=True)
class Provider:
    """One place a prompt can go."""

    name: str
    #: Environment variable holding the key. Empty for a local provider.
    key_env: str
    #: ``{base}`` is read from the env for Ollama and is otherwise fixed.
    url: str
    model: str
    #: Ollama speaks its own chat shape; the rest are OpenAI-compatible.
    dialect: str = "openai"
    #: Whether the provider honours a JSON response format. Where it does not,
    #: the prompt asks for JSON and the parser tolerates fencing.
    json_mode: bool = True

    def available(self) -> bool:
        if not self.key_env:
            return bool(os.environ.get("MR_OLLAMA_HOST", "").strip())
        return bool(os.environ.get(self.key_env, "").strip())


#: In order of preference: fast and good, fast and good, then local.
#:
#: Ollama is last on purpose rather than first. It has no quota and no upstream,
#: which makes it the right *fallback*; the models small enough to run on a
#: laptop are also the ones most likely to invent a number, and this module's
#: consumers check citations for exactly that reason.
#:
#: **The model names were read from each provider's /models endpoint on
#: 2026-09-11, not recalled.** The first attempt used `llama-3.3-70b-versatile`
#: on Groq, which no longer exists and returned 404 -- the router degraded to
#: Ollama and answered correctly, which is the design working and is also how the
#: stale name went unnoticed for a minute. A 404 from a hosted provider means
#: re-query the endpoint rather than guess again; these lists turn over.
PROVIDERS: Final[tuple[Provider, ...]] = (
    Provider(
        name="groq",
        key_env="MR_GROQ_API_KEY",
        url="https://api.groq.com/openai/v1/chat/completions",
        model="openai/gpt-oss-120b",
    ),
    Provider(
        name="cerebras",
        key_env="MR_CEREBRAS_API_KEY",
        url="https://api.cerebras.ai/v1/chat/completions",
        model="gpt-oss-120b",
    ),
    Provider(
        name="ollama",
        key_env="",
        url="{base}/api/chat",
        model="qwen2.5:3b-instruct",
        dialect="ollama",
        json_mode=True,
    ),
)

REQUEST_TIMEOUT: Final[float] = 120.0

#: Retries are for the transport, not for the answer. A model that returned
#: unusable JSON will not fix itself and the next provider is a better bet; a
#: rate limit is a queue and waiting it out is the whole point.
#:
#: Two rather than one because a drained free tier refuses the first call of a
#: run, and one retry spends itself discovering that.
TRANSPORT_RETRIES: Final[int] = 2

#: Longest a 429 is worth waiting out before moving to the next provider. A free
#: tier that wants sixty seconds is telling you to go somewhere else.
MAX_BACKOFF: Final[float] = 20.0

#: Waited when a 429 carries no Retry-After.
DEFAULT_BACKOFF: Final[float] = 3.0


#: Status codes that mean "this provider will not work today": a bad key, an
#: unpaid account, a blocked region. Unlike a 429 or a 5xx they will not clear on
#: a retry, so the provider is struck off for the rest of the process.
#:
#: Measured the hard way: a configured Cerebras key on an account with no credit
#: answered 402 to every call, which cost one wasted request *per extraction* and
#: showed up only as noise in the log. A key being set is not the same as a
#: provider working, and `available()` can only see the former.
PERMANENT_FAILURES: Final[frozenset[int]] = frozenset({401, 402, 403})

#: Providers struck off this process. Deliberately process-local and not
#: persisted: an unpaid account gets paid, a blocked region gets unblocked, and a
#: cached "never try this again" on disk would outlive the reason for it.
_struck_off: set[str] = set()

#: Floor between calls to one provider, used until it tells us better.
#:
#: A free tier meters tokens per minute, so an unpaced loop spends its budget in
#: the first few seconds and then 429s for the rest of the minute -- which reads
#: as a broken provider rather than as a queue, and sends every later extraction
#: to a weaker model.
MIN_INTERVAL: Final[dict[str, float]] = {"groq": 1.0, "cerebras": 1.0}

#: Wait for the token bucket to refill when fewer than this fraction of it is
#: left. Below about a fifth there is not room for another extraction-sized
#: prompt, and discovering that by being refused costs a round trip.
LOW_WATER: Final[float] = 0.2

_last_call: dict[str, float] = {}

#: ``{provider: (seconds until the token bucket resets, remaining fraction)}``,
#: read from the response headers of the previous call.
_budget: dict[str, tuple[float, float]] = {}


def _parse_reset(raw: str) -> float:
    """Groq's ``18.39s`` / ``50m24s`` / ``1m`` as seconds."""
    total, number = 0.0, ""
    for char in raw.strip():
        if char.isdigit() or char == ".":
            number += char
            continue
        if not number:
            continue
        value = float(number)
        total += value * {"h": 3600.0, "m": 60.0, "s": 1.0}.get(char, 0.0)
        number = ""
    if number:
        total += float(number)
    return total


def _note_budget(provider: str, resp: httpx.Response) -> None:
    """Record what the provider says is left, so the next call can pace itself.

    **Measured rather than guessed.** Groq's free tier allows 8,000 tokens a
    minute for gpt-oss-120b, which is about four extraction-sized prompts; a
    hand-picked four-second gap therefore still 429'd, while the response headers
    had been saying exactly how long to wait the whole time. Same lesson as the
    Tiingo pacer: a fixed sleep either wastes the quota or blows through it.
    """
    limit = resp.headers.get("x-ratelimit-limit-tokens")
    remaining = resp.headers.get("x-ratelimit-remaining-tokens")
    reset = resp.headers.get("x-ratelimit-reset-tokens")
    if not (limit and remaining and reset):
        return
    try:
        share = float(remaining) / float(limit) if float(limit) else 1.0
    except (TypeError, ValueError, ZeroDivisionError):
        return
    _budget[provider] = (_parse_reset(reset), share)


def _pace(provider: str) -> None:
    wait = MIN_INTERVAL.get(provider, 0.0) - (
        time.monotonic() - _last_call.get(provider, 0.0))
    reset, share = _budget.get(provider, (0.0, 1.0))
    if share < LOW_WATER:
        # The provider has told us there is not room for another prompt. Waiting
        # out the window is cheaper than a refusal plus a fall-through to a
        # weaker model.
        wait = max(wait, min(reset + 0.5, MAX_BACKOFF))
        log.info("%s has %.0f%% of its token budget left; waiting %.1fs",
                 provider, share * 100, wait)
    if wait > 0:
        time.sleep(wait)
    _last_call[provider] = time.monotonic()


def struck_off() -> frozenset[str]:
    """Providers this process has given up on, and why they are worth knowing."""
    return frozenset(_struck_off)


def _retry_after(resp: httpx.Response) -> float:
    """Seconds the provider asked for, or a default.

    Groq sends ``retry-after`` in seconds; some providers send a date. A value
    that cannot be read is not an excuse to hammer, so it falls back to a wait
    rather than to zero.
    """
    raw = (resp.headers.get("retry-after") or "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_BACKOFF


@dataclass(frozen=True, slots=True)
class Answer:
    """What came back, and who said it."""

    data: dict[str, Any]
    provider: str
    model: str
    #: Bumped by hand whenever a prompt changes. Stored with every extracted
    #: figure, because a number from prompt v1 and one from v3 are different
    #: measurements and a re-run that mixes them is not comparable.
    prompt_version: str
    elapsed_ms: int


def _payload(provider: Provider, system: str, user: str,
             temperature: float) -> dict[str, Any]:
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    if provider.dialect == "ollama":
        body: dict[str, Any] = {
            "model": provider.model, "messages": messages, "stream": False,
            "options": {"temperature": temperature},
        }
        if provider.json_mode:
            body["format"] = "json"
        return body
    body = {"model": provider.model, "messages": messages,
            "temperature": temperature}
    if provider.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def _content(provider: Provider, payload: dict[str, Any]) -> str:
    if provider.dialect == "ollama":
        return (payload.get("message") or {}).get("content") or ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content") or ""


def _parse_json(raw: str) -> dict[str, Any]:
    """The model's text as an object, tolerating a fenced block.

    Deliberately strict about the *shape*: a list or a bare string is not an
    answer this router promises, and silently wrapping one would let a consumer
    read a field that was never there.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def ask(
    system: str,
    user: str,
    *,
    prompt_version: str,
    temperature: float = 0.0,
    providers: tuple[Provider, ...] = PROVIDERS,
    client: httpx.Client | None = None,
) -> Answer:
    """Ask the first provider that works, and say which one it was.

    ``temperature`` defaults to zero: this tier does extraction, where two runs
    over the same document disagreeing is a defect rather than variety.
    """
    usable = [p for p in providers
              if p.available() and p.name not in _struck_off]
    if not usable:
        struck = f" (struck off this run: {sorted(_struck_off)})" \
            if _struck_off else ""
        raise LlmError(
            "no LLM provider is configured. Set MR_GROQ_API_KEY, "
            "MR_CEREBRAS_API_KEY, or run Ollama and set MR_OLLAMA_HOST."
            + struck
        )
    close = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT)
    failures: list[str] = []
    try:
        for provider in usable:
            url = provider.url.format(
                base=os.environ.get("MR_OLLAMA_HOST", "").rstrip("/"))
            headers = {"Content-Type": "application/json"}
            if provider.key_env:
                headers["Authorization"] = (
                    f"Bearer {os.environ[provider.key_env].strip()}")
            body = _payload(provider, system, user, temperature)
            for attempt in range(TRANSPORT_RETRIES + 1):
                _pace(provider.name)
                started = time.monotonic()
                try:
                    resp = client.post(url, headers=headers, json=body)
                    if resp.status_code in PERMANENT_FAILURES:
                        _struck_off.add(provider.name)
                        log.warning(
                            "%s returned HTTP %s, which will not clear on a "
                            "retry; struck off for this process",
                            provider.name, resp.status_code)
                        failures.append(
                            f"{provider.name}: HTTP {resp.status_code} "
                            "(struck off)")
                        break
                    if resp.status_code in (429, 500, 502, 503, 504):
                        # A free tier answers 429 constantly and it is not a
                        # failure, it is a queue. Honour Retry-After when the
                        # provider sends one and otherwise back off a little:
                        # retrying instantly, which is what this did, turns one
                        # rate limit into two and then falls through to a weaker
                        # provider for no reason.
                        if resp.status_code == 429:
                            _note_budget(provider.name, resp)
                            # The refusal itself says when the bucket refills, and
                            # that beats a default: a drained free tier answers
                            # 429 to the *first* call of a run, where there is no
                            # recorded budget yet and a 3-second guess is not
                            # enough to clear it.
                            reset, _ = _budget.get(provider.name, (0.0, 1.0))
                            wait = max(_retry_after(resp), reset + 0.5)
                            if attempt < TRANSPORT_RETRIES and wait <= MAX_BACKOFF:
                                log.info("%s rate-limited; waiting %.1fs",
                                         provider.name, wait)
                                time.sleep(wait)
                        raise httpx.HTTPError(f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    _note_budget(provider.name, resp)
                    data = _parse_json(_content(provider, resp.json()))
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    if attempt < TRANSPORT_RETRIES and isinstance(
                            exc, httpx.HTTPError):
                        continue
                    failures.append(f"{provider.name}: {exc}")
                    log.warning("%s failed (%s); trying the next provider",
                                provider.name, exc)
                    break
                return Answer(
                    data=data, provider=provider.name, model=provider.model,
                    prompt_version=prompt_version,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
    finally:
        if close:
            client.close()
    raise LlmError(
        "every provider failed: " + "; ".join(failures)
        + ". This degrades rather than crashing a pipeline -- the caller is "
        "expected to record the field as not_parsed and move on."
    )
