"""The cheap-tier router. No network: every provider is a fake client.

The two properties worth pinning are the ones CLAUDE.md states as rules --
provider failure degrades rather than crashing, and which provider answered is
part of the answer -- plus the two that were learned by running it: a permanent
failure must not be retried on every call, and a free tier must be paced.
"""

from __future__ import annotations

import json

import httpx
import pytest

from marketradar.llm import router


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None,
                 headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None)  # type: ignore[arg-type]


def openai_reply(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


class FakeClient:
    """Answers per-URL from a script, and records what it was asked."""

    def __init__(self, script: dict[str, list[FakeResponse]]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, headers=None, json=None):  # noqa: A002
        self.calls.append((url, json or {}))
        for fragment, responses in self.script.items():
            if fragment in url:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"no scripted response for {url}")

    def close(self) -> None:
        return None


GROQ = router.Provider(name="groq", key_env="MR_GROQ_API_KEY",
                       url="https://groq.test/v1/chat", model="good-model")
LOCAL = router.Provider(name="ollama", key_env="",
                        url="{base}/api/chat", model="small-model",
                        dialect="ollama")


@pytest.fixture(autouse=True)
def clean_router(monkeypatch):
    """Each test starts with no provider struck off and no pacing debt."""
    monkeypatch.setattr(router, "_struck_off", set())
    monkeypatch.setattr(router, "_last_call", {})
    monkeypatch.setattr(router, "_deferred", {})
    monkeypatch.setattr(router, "MIN_INTERVAL", {})
    monkeypatch.setenv("MR_GROQ_API_KEY", "k")
    monkeypatch.setenv("MR_OLLAMA_HOST", "http://localhost:11434")


# --- degrading ----------------------------------------------------------


def test_a_failing_provider_falls_through_to_the_next() -> None:
    """Six free tiers means six things that can change without notice."""
    client = FakeClient({
        "groq.test": [FakeResponse(503)],
        "11434": [FakeResponse(200, {"message": {"content": '{"n": 1}'}})],
    })
    answer = router.ask("s", "u", prompt_version="v1",
                        providers=(GROQ, LOCAL), client=client)
    assert answer.data == {"n": 1}
    assert answer.provider == "ollama"


def test_every_provider_failing_raises_with_all_the_reasons() -> None:
    """The caller is expected to record the field as unread and move on, which
    it can only do if the error says what happened."""
    client = FakeClient({
        "groq.test": [FakeResponse(503)],
        "11434": [FakeResponse(500)],
    })
    with pytest.raises(router.LlmError, match="every provider failed"):
        router.ask("s", "u", prompt_version="v1", providers=(GROQ, LOCAL),
                   client=client)


def test_no_configured_provider_says_which_variables_to_set(monkeypatch) -> None:
    monkeypatch.delenv("MR_GROQ_API_KEY", raising=False)
    monkeypatch.delenv("MR_OLLAMA_HOST", raising=False)
    with pytest.raises(router.LlmError, match="MR_GROQ_API_KEY"):
        router.ask("s", "u", prompt_version="v1", providers=(GROQ, LOCAL))


# --- permanent failures -------------------------------------------------


def test_a_permanent_failure_is_struck_off_and_not_retried() -> None:
    """A configured key on an unpaid account answered 402 to every call, which
    cost one wasted request per extraction and showed up only as log noise. A key
    being set is not the same as a provider working.
    """
    client = FakeClient({
        "groq.test": [FakeResponse(402)],
        "11434": [FakeResponse(200, {"message": {"content": '{"n": 2}'}})],
    })
    for _ in range(3):
        answer = router.ask("s", "u", prompt_version="v1",
                            providers=(GROQ, LOCAL), client=client)
        assert answer.provider == "ollama"
    assert router.struck_off() == frozenset({"groq"})
    groq_calls = [u for u, _ in client.calls if "groq.test" in u]
    assert len(groq_calls) == 1, (
        f"the struck-off provider was called {len(groq_calls)} times"
    )


def test_a_rate_limit_is_not_permanent() -> None:
    """429 is a queue, not a broken provider. Striking it off would send every
    later call to a weaker model for the rest of the process."""
    client = FakeClient({
        "groq.test": [FakeResponse(429, headers={"retry-after": "0"}),
                      FakeResponse(200, openai_reply({"n": 3}))],
        "11434": [FakeResponse(200, {"message": {"content": '{"n": 99}'}})],
    })
    answer = router.ask("s", "u", prompt_version="v1",
                        providers=(GROQ, LOCAL), client=client)
    assert answer.data == {"n": 3}
    assert answer.provider == "groq"
    assert router.struck_off() == frozenset()


def test_retry_after_is_read_and_a_missing_one_still_waits() -> None:
    """A header that cannot be read is not an excuse to hammer."""
    assert router._retry_after(FakeResponse(429, headers={"retry-after": "7"})) == 7.0
    assert router._retry_after(
        FakeResponse(429, headers={"retry-after": "Tue, 9 Sep 2026 12:00:00 GMT"})
    ) == router.DEFAULT_BACKOFF
    assert router._retry_after(FakeResponse(429)) == router.DEFAULT_BACKOFF


def test_pacing_leaves_a_gap_between_calls_to_one_provider(monkeypatch) -> None:
    """A free tier meters tokens per minute, so an unpaced loop spends its whole
    budget in the first seconds and then 429s for the rest of it."""
    slept: list[float] = []
    monkeypatch.setattr(router.time, "sleep", slept.append)
    monkeypatch.setattr(router, "MIN_INTERVAL", {"groq": 4.0})
    client = FakeClient({"groq.test": [FakeResponse(200, openai_reply({"n": 1}))]})
    router.ask("s", "u", prompt_version="v1", providers=(GROQ,), client=client)
    router.ask("s", "u", prompt_version="v1", providers=(GROQ,), client=client)
    assert slept, "the second call was not paced"
    assert 0 < slept[0] <= 4.0


# --- the answer ---------------------------------------------------------


def test_the_answer_records_who_said_it() -> None:
    """A number from a 120B model and the same number from a 3B model on a laptop
    are not equally trustworthy, and a table recording only the number cannot
    tell them apart later."""
    client = FakeClient({"groq.test": [FakeResponse(200, openai_reply({"n": 4}))]})
    answer = router.ask("s", "u", prompt_version="proxy-v1",
                        providers=(GROQ,), client=client)
    assert answer.provider == "groq"
    assert answer.model == "good-model"
    assert answer.prompt_version == "proxy-v1"
    assert answer.elapsed_ms >= 0


def test_a_fenced_json_block_is_tolerated() -> None:
    fenced = "```json\n{\"n\": 5}\n```"
    client = FakeClient({
        "groq.test": [FakeResponse(200, {"choices": [{"message":
                                                     {"content": fenced}}]})],
    })
    answer = router.ask("s", "u", prompt_version="v1", providers=(GROQ,),
                        client=client)
    assert answer.data == {"n": 5}


def test_a_json_list_is_not_an_answer() -> None:
    """The router promises an object. Wrapping a list would let a consumer read
    a field that was never there."""
    client = FakeClient({
        "groq.test": [FakeResponse(200, {"choices": [{"message":
                                                     {"content": "[1, 2]"}}]})],
        "11434": [FakeResponse(500)],
    })
    with pytest.raises(router.LlmError):
        router.ask("s", "u", prompt_version="v1", providers=(GROQ, LOCAL),
                   client=client)


def test_extraction_asks_for_temperature_zero() -> None:
    """Two runs over the same document disagreeing is a defect here, not
    variety."""
    client = FakeClient({"groq.test": [FakeResponse(200, openai_reply({"n": 6}))]})
    router.ask("s", "u", prompt_version="v1", providers=(GROQ,), client=client)
    _, body = client.calls[0]
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}


def test_ollama_gets_its_own_request_shape() -> None:
    """Ollama is not OpenAI-compatible on this endpoint, and sending the wrong
    shape fails in a way that looks like the model refusing."""
    client = FakeClient({
        "11434": [FakeResponse(200, {"message": {"content": '{"n": 7}'}})],
    })
    router.ask("s", "u", prompt_version="v1", providers=(LOCAL,), client=client)
    url, body = client.calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert body["format"] == "json"
    assert body["stream"] is False
    assert "response_format" not in body


# --- pacing from what the provider says ---------------------------------


def test_the_reset_header_is_parsed_in_every_shape_groq_sends() -> None:
    assert router._parse_reset("18.39s") == pytest.approx(18.39)
    assert router._parse_reset("50m24s") == pytest.approx(3024.0)
    assert router._parse_reset("1m") == pytest.approx(60.0)
    assert router._parse_reset("2h3m") == pytest.approx(7380.0)
    # A bare number is seconds, which is what most providers send.
    assert router._parse_reset("7") == pytest.approx(7.0)


def test_a_nearly_empty_token_budget_is_waited_out(monkeypatch) -> None:
    """Measured rather than guessed: Groq's free tier allows 8,000 tokens a
    minute for this model, which is about four extraction-sized prompts, so a
    hand-picked four-second gap still got refused. The headers had been saying
    exactly how long to wait the whole time -- the same lesson as the Tiingo
    pacer, where a fixed sleep either wastes the quota or blows through it.
    """
    slept: list[float] = []
    monkeypatch.setattr(router.time, "sleep", slept.append)
    monkeypatch.setattr(router, "_budget", {})
    low = FakeResponse(200, openai_reply({"n": 1}), headers={
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-tokens": "400",
        "x-ratelimit-reset-tokens": "12s",
    })
    client = FakeClient({"groq.test": [low]})
    router.ask("s", "u", prompt_version="v1", providers=(GROQ,), client=client)
    assert slept == [] or slept[0] < 2, "the first call should not wait"
    router.ask("s", "u", prompt_version="v1", providers=(GROQ,), client=client)
    assert slept, "the second call ignored a nearly empty budget"
    assert slept[-1] == pytest.approx(12.5, abs=1.0)


def test_a_full_budget_does_not_wait(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(router.time, "sleep", slept.append)
    monkeypatch.setattr(router, "_budget", {})
    monkeypatch.setattr(router, "MIN_INTERVAL", {})
    full = FakeResponse(200, openai_reply({"n": 1}), headers={
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-remaining-tokens": "7900",
        "x-ratelimit-reset-tokens": "3s",
    })
    client = FakeClient({"groq.test": [full]})
    for _ in range(3):
        router.ask("s", "u", prompt_version="v1", providers=(GROQ,),
                   client=client)
    assert slept == [], f"waited when the budget was full: {slept}"


def test_a_wait_longer_than_the_backoff_cap_falls_through_instead() -> None:
    """A provider asking for a minute is telling you to go somewhere else."""
    assert router.MAX_BACKOFF <= 30.0


# --- a day cap is not a minute bucket -----------------------------------


def test_a_long_rate_limit_defers_the_provider_for_the_rest_of_the_run() -> None:
    """**Measured 2026-09-11, and the headers cannot see it.** Groq's free tier
    caps tokens per *day* as well as per minute -- 200,000 per model -- and with
    the day bucket spent the per-minute headers read a healthy
    ``remaining-tokens: 8000`` while every call came back 429 with
    ``retry-after: 723``.

    A 429 asking for twelve minutes is not a queue, it is a closed door. Asking
    again costs one request to relearn what the provider just said, which is the
    Cerebras-402 lesson one notch less permanent: the provider is skipped until
    its deadline rather than struck off forever.
    """
    client = FakeClient({
        "groq.test": [FakeResponse(429, headers={
            "retry-after": "723",
            # The healthy minute bucket that hid the real limit.
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "8000",
            "x-ratelimit-reset-tokens": "1ms",
        })],
        "11434": [FakeResponse(200, {"message": {"content": '{"n": 99}'}})],
    })
    answer = router.ask("s", "u", prompt_version="v1",
                        providers=(GROQ, LOCAL), client=client)
    assert answer.provider == "ollama"
    groq_calls = [u for u, _ in client.calls if "groq.test" in u]
    assert len(groq_calls) == 1, (
        f"a provider that said 'not for 12 minutes' was asked "
        f"{len(groq_calls)} times")

    # And the next call skips it without a request at all.
    again = router.ask("s", "u", prompt_version="v1",
                       providers=(GROQ, LOCAL), client=client)
    assert again.provider == "ollama"
    assert len([u for u, _ in client.calls if "groq.test" in u]) == 1

    # Deferred, not struck off: it will work again today.
    assert router.struck_off() == frozenset()
    assert "groq" in router.deferred()
    assert router.deferred()["groq"] > 600


def test_a_busy_minute_does_not_cost_the_good_model_for_the_whole_run() -> None:
    """The middle case, and the reason ``DEFER_AFTER`` is its own constant.

    A drained *minute* bucket asks for something like 42 seconds -- measured on
    ``gpt-oss-20b``. That is too long to sit on and nowhere near a day cap, so the
    call falls through to the next provider and the provider stays in the ladder
    for the next one. Deferring on 42 seconds would throw away the only model that
    can do the job over one busy minute.
    """
    client = FakeClient({
        "groq.test": [FakeResponse(429, headers={"retry-after": "42"}),
                      FakeResponse(200, openai_reply({"n": 7}))],
        "11434": [FakeResponse(200, {"message": {"content": '{"n": 99}'}})],
    })
    first = router.ask("s", "u", prompt_version="v1",
                       providers=(GROQ, LOCAL), client=client)
    assert first.provider == "ollama", "a 42s wait was sat on rather than routed"
    assert router.deferred() == {}, "a minute bucket was read as a day cap"

    second = router.ask("s", "u", prompt_version="v1",
                        providers=(GROQ, LOCAL), client=client)
    assert second.provider == "groq", "the good model was dropped for the run"


def test_a_deferred_provider_is_reported_rather_than_hidden() -> None:
    """A run that produced half its figures from the fallback model did so for a
    reason, and "the good model's daily cap was spent at document 23" is the
    reason a consumer needs in order to read the numbers."""
    router._defer("groq", 300.0)
    assert "groq" in router.deferred()
    client = FakeClient({"groq.test": [FakeResponse(200, openai_reply({}))]})
    with pytest.raises(router.LlmError, match="out of day budget"):
        router.ask("s", "u", prompt_version="v1", providers=(GROQ,),
                   client=client)
    assert client.calls == [], "a deferred provider was still asked"
