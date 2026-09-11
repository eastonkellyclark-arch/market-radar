"""The proxy reader: locate deterministically, extract with provenance.

No network and no model: the LLM is a fake client returning scripted JSON, which
is the only way to test the part that matters -- what happens to an answer once
it comes back. The interesting behaviour is all in the checking.

Every fixture here is a cut-down proxy with the real phrasing, because the
locators key on market convention and a fixture written in my own words would be
testing my words.
"""

from __future__ import annotations

import json

import httpx
import pytest

from marketradar.llm import router
from marketradar.signals import proxy

# --- documents ----------------------------------------------------------

#: The figures are what the locator scores on, so a fixture section needs them.
FIGURES = (" The closing price was $41.00 on March 2, 2026, a premium of 23.4%. "
           "Analysts had modelled $38.50 and $44.25 per share, or 1.8x and "
           "2.05x revenue. ")

CASH_DEAL = (
    "TABLE OF CONTENTS The Merger Consideration 42 Opinion of the Financial "
    "Advisor 58 Premiums Paid Analysis 61 "
    + "filler. " * 200
    # A decoy: the same operative language in the tax discussion, with no
    # figures near it. This is the shape that makes position rules fail, so the
    # fixture has to contain one or the selection rule is untested.
    + "Material U.S. Federal Income Tax Consequences. The exchange in which "
      "each issued and outstanding share of Company common stock will be "
      "converted into the right to receive cash is expected to be a taxable "
      "transaction for U.S. holders."
    + "filler. " * 200
    + "The Merger Consideration At the effective time, each issued and "
      "outstanding share of Company common stock will be converted into the "
      "right to receive $52.00 in cash, without interest."
    + FIGURES
    + "filler. " * 400
    + "Premiums Paid Analysis The Financial Advisor reviewed the consideration "
      "paid in 24 selected transactions. The $52.00 per share consideration "
      "represents a premium of 31.7% over the closing price on the last "
      "trading day before announcement."
    + FIGURES
)

STOCK_DEAL = (
    "The Merger Consideration Each issued and outstanding share of Company "
    "common stock will be converted into the right to receive 0.6303 shares of "
    "Parent common stock."
    + FIGURES
    + "filler. " * 300
    + "Premiums Paid Analysis The Advisor reviewed premiums paid in 30 selected "
      "healthcare transactions, which ranged from 12.0% to 88.0% with a median "
      "of 41.0%."
    + FIGURES
)

#: An acquirer's own proxy, asking its holders to approve a share issuance. Not a
#: takeout, and the first real batch read five of six documents that looked like
#: this one.
ISSUANCE_PROXY = (
    "Consideration to be Received Director Shares Held William Dillard, II "
    "9,997 960,246 Alex Dillard 10,097 969,864 "
    + "filler. " * 300
    + "the issuance of (i) up to 41,496 shares of Class A common stock, par "
      "value $0.01 per share, and (ii) up to 3,985,776 shares of Class B "
      "common stock, par value $0.01 per share, of the Company"
    + FIGURES
)

NO_SECTIONS = "Annual Meeting of Stockholders. " + "nothing relevant. " * 300


# --- a fake model -------------------------------------------------------


class Reply:
    def __init__(self, payload: dict, headers: dict | None = None) -> None:
        self.status_code = 200
        self._payload = payload
        # The router reads rate-limit headers to pace itself, so a fake response
        # without them is not a response.
        self.headers = headers or {}

    def json(self) -> dict:
        return {"choices": [{"message":
                             {"content": json.dumps(self._payload)}}]}

    def raise_for_status(self) -> None:
        return None


class FakeModel:
    """Returns a scripted payload per call, and records the prompts it saw."""

    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    def post(self, url: str, *, headers=None, json=None):  # noqa: A002
        self.prompts.append(json["messages"][1]["content"])
        payload = self.payloads.pop(0) if len(self.payloads) > 1 \
            else self.payloads[0]
        return Reply(payload)

    def close(self) -> None:
        return None


ONLY_GROQ = (router.Provider(name="groq", key_env="MR_GROQ_API_KEY",
                             url="https://groq.test/v1", model="test-model"),)


@pytest.fixture(autouse=True)
def one_provider(monkeypatch):
    monkeypatch.setenv("MR_GROQ_API_KEY", "k")
    monkeypatch.setattr(router, "_struck_off", set())
    monkeypatch.setattr(router, "MIN_INTERVAL", {})


# --- locating -----------------------------------------------------------


def test_the_window_with_the_numbers_wins_not_the_first_or_the_last() -> None:
    """Measured on a real proxy: the same heading matches in the table of
    contents, the body, the tax discussion and the appended merger agreement, so
    "first" lands in the contents and "last" lands in the annex.
    """
    section = proxy.section_window(CASH_DEAL, "merger_consideration")
    assert section is not None
    assert "$52.00 in cash" in section.text
    assert section.start > CASH_DEAL.index("TABLE OF CONTENTS")
    assert section.figures >= proxy.MIN_FIGURES


def test_a_heading_only_in_prose_is_no_section() -> None:
    """``no_section`` is a more useful answer than a prompt aimed at the wrong
    paragraph, and its remedy is a locator rather than a prompt."""
    assert proxy.section_window(NO_SECTIONS, "premiums_paid") is None


def test_the_window_is_capped_so_the_document_is_never_sent() -> None:
    huge = "Premiums Paid Analysis " + FIGURES + ("x" * 500_000)
    section = proxy.section_window(huge, "premiums_paid")
    assert section is not None
    assert section.chars <= proxy.SECTION_CHARS
    assert section.chars < len(huge) / 10


def test_the_section_records_why_it_was_chosen() -> None:
    """A reader has to be able to see the selection, not trust it."""
    section = proxy.section_window(CASH_DEAL, "merger_consideration")
    assert section is not None
    assert section.candidates >= 2, "the fixture has no decoy to choose against"
    assert section.figures > 0
    assert section.heading
    # The decoy comes first in the document and carries no figures, so a
    # position rule would have taken it.
    decoy = CASH_DEAL.index("Material U.S. Federal Income Tax")
    assert section.start > decoy


# --- the population gate ------------------------------------------------


def test_an_issuance_proxy_is_not_a_takeout_and_costs_no_prompt() -> None:
    """The finding that the first batch produced. ``DEFM14A`` is a proxy
    *relating to* a merger, which includes an acquirer asking its own holders to
    approve a share issuance -- Dillard's filed one for 41,496 Class A shares and
    is not being acquired. Five of the first six documents read were this.

    A prompt aimed at one returns a perfectly correct "no cash price stated",
    which reads like a coverage problem and is not one.
    """
    assert not proxy.is_takeout_proxy(ISSUANCE_PROXY)
    model = FakeModel({"present": True, "value": 1.0, "quote": "x"})
    figures = proxy.read("acc-1", ISSUANCE_PROXY, client=model,
                         providers=ONLY_GROQ)
    assert {f.reason for f in figures} == {proxy.NOT_A_TAKEOUT}
    assert model.prompts == [], "a prompt was sent for a non-takeout document"
    assert len(figures) == len(proxy.FIELDS), "every field still gets a row"


def test_a_real_takeout_passes_the_gate() -> None:
    assert proxy.is_takeout_proxy(CASH_DEAL)
    assert proxy.is_takeout_proxy(STOCK_DEAL)


# --- extraction, and the checking -------------------------------------


def test_a_stated_figure_carries_its_provenance() -> None:
    """An LLM number with no provenance is unauditable. Section, offsets, quote,
    provider, model and prompt version, on every row."""
    model = FakeModel({"present": True, "value": 52.0,
                       "quote": "converted into the right to receive $52.00 in "
                                "cash"})
    figure = proxy.extract_field("acc-2", CASH_DEAL, "consideration_per_share",
                                 client=model, providers=ONLY_GROQ)
    assert figure.reason == proxy.STATED
    assert figure.value == 52.0
    assert figure.unit == "usd_per_share"
    assert figure.section == "merger_consideration"
    assert figure.section_start is not None and figure.section_end is not None
    assert figure.section_end > figure.section_start
    assert figure.quote and "$52.00" in figure.quote
    assert figure.provider == "groq" and figure.model == "test-model"
    assert figure.prompt_version == proxy.PROMPT_VERSION
    assert figure.usable


def test_a_figure_whose_quote_is_not_in_the_text_is_rejected() -> None:
    """The failure mode this tier actually has. Rejected rather than stored, and
    counted -- the rate is how trust in a provider is earned, and it is the
    reason a small local model is an acceptable fallback at all.
    """
    model = FakeModel({"present": True, "value": 99.0,
                       "quote": "the right to receive $99.00 in cash"})
    figure = proxy.extract_field("acc-3", CASH_DEAL, "consideration_per_share",
                                 client=model, providers=ONLY_GROQ)
    assert figure.reason == proxy.UNCITED
    assert figure.value is None, "an unverifiable number was stored anyway"
    assert "not in the section" in (figure.note or "")


def test_a_quote_reflowed_by_the_model_still_verifies() -> None:
    """Models reflow whitespace when they copy. Rejecting a figure over a double
    space would make the check useless while looking strict."""
    model = FakeModel({"present": True, "value": 52.0,
                       "quote": "converted   into the\n right to receive "
                                "$52.00 in cash"})
    figure = proxy.extract_field("acc-4", CASH_DEAL, "consideration_per_share",
                                 client=model, providers=ONLY_GROQ)
    assert figure.reason == proxy.STATED


def test_not_stated_and_not_parsed_stay_distinct() -> None:
    """A stock-for-stock merger genuinely has no cash price per share, and
    recording that as a parse failure sends the next reader hunting for a number
    nobody wrote down."""
    absent = FakeModel({"present": False, "value": None, "quote": None,
                        "why_absent": "consideration is shares, not cash"})
    figure = proxy.extract_field("acc-5", STOCK_DEAL,
                                 "consideration_per_share",
                                 client=absent, providers=ONLY_GROQ)
    assert figure.reason == proxy.NOT_STATED
    assert figure.value is None
    assert "shares" in (figure.note or "")

    garbled = FakeModel({"present": True, "value": "about fifty dollars",
                         "quote": "x"})
    figure = proxy.extract_field("acc-6", CASH_DEAL,
                                 "consideration_per_share",
                                 client=garbled, providers=ONLY_GROQ)
    assert figure.reason == proxy.NOT_PARSED
    assert figure.reason != proxy.NOT_STATED


def test_a_missing_quote_is_uncited_rather_than_stated() -> None:
    model = FakeModel({"present": True, "value": 31.7, "quote": None})
    figure = proxy.extract_field("acc-7", CASH_DEAL, "premium_pct",
                                 client=model, providers=ONLY_GROQ)
    assert figure.reason == proxy.UNCITED
    assert figure.value is None


def test_no_provider_is_not_parsed_rather_than_a_crash() -> None:
    """Provider failure degrades. An unread field is a fact about this run, not
    about the document."""
    class Dead:
        def post(self, *a, **kw):
            raise httpx.HTTPError("down")

        def close(self) -> None:
            return None

    figure = proxy.extract_field("acc-8", CASH_DEAL, "premium_pct",
                                 client=Dead(), providers=ONLY_GROQ)
    assert figure.reason == proxy.NOT_PARSED
    assert "no provider" in (figure.note or "")


def test_the_prompt_gets_the_section_and_not_the_document() -> None:
    """The whole point of locating first. A 1.26-million-character document does
    not fit a prompt, and sending five pages where a paragraph would do was the
    binding constraint on throughput."""
    model = FakeModel({"present": False, "value": None, "quote": None})
    proxy.extract_field("acc-9", CASH_DEAL, "premium_pct", client=model,
                        providers=ONLY_GROQ)
    sent = model.prompts[0]
    assert len(sent) < len(CASH_DEAL) + 2_000
    assert "Premiums Paid Analysis" in sent
    assert "TABLE OF CONTENTS" not in sent


def test_the_prompt_says_when_absent_is_the_right_answer() -> None:
    """Without it a model asked for a cash price in a stock deal will produce
    one, and the citation check would pass because some dollar figure is always
    nearby."""
    model = FakeModel({"present": False, "value": None, "quote": None})
    proxy.extract_field("acc-10", STOCK_DEAL, "consideration_per_share",
                        client=model, providers=ONLY_GROQ)
    assert "report absent" in model.prompts[0]


# --- scope --------------------------------------------------------------


def test_v1_reads_consideration_and_premium_only() -> None:
    """Comps, projections and DCF ranges come after these are shown to work."""
    assert set(proxy.FIELDS) == {
        "consideration_per_share", "exchange_ratio", "premium_pct"}
    with pytest.raises(ValueError, match="not in v1"):
        proxy.extract_field("a", CASH_DEAL, "dcf_discount_rate")


def test_every_field_always_gets_a_row() -> None:
    model = FakeModel({"present": False, "value": None, "quote": None})
    figures = proxy.read("acc-11", CASH_DEAL, client=model,
                         providers=ONLY_GROQ)
    assert len(figures) == len(proxy.FIELDS)
    assert {f.field for f in figures} == set(proxy.FIELDS)
    assert all(f.reason in proxy.REASONS for f in figures)


def test_the_report_counts_every_reason() -> None:
    figures = [
        proxy.Figure(accession="a", field="premium_pct", reason=proxy.STATED,
                     value=31.7, quote="q"),
        proxy.Figure(accession="b", field="premium_pct", reason=proxy.UNCITED),
        proxy.Figure(accession="c", field="premium_pct",
                     reason=proxy.NO_SECTION),
    ]
    report = proxy.ReadReport(documents=3, figures=figures)
    counts = report.by_reason("premium_pct")
    assert counts[proxy.STATED] == 1
    assert counts[proxy.UNCITED] == 1
    assert counts[proxy.NO_SECTION] == 1
    text = "\n".join(report.lines())
    assert "1/3" in text
    assert "citation check earning its keep" in text
