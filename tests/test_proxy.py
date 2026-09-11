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
    + "No fractional shares will be issued. Each holder who would otherwise be "
      "entitled to receive a fraction of a share will instead be entitled to "
      "receive cash of $0.01 per share in lieu of such fraction."
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

#: A Canadian plan of arrangement, in SunOpta's own phrasing. The verb is not
#: "converted into" and the subject is the holder rather than the share, which is
#: why the US-only gate missed a real $6.50 cash takeout.
ARRANGEMENT = (
    "The Arrangement Pursuant to the Plan of Arrangement, on the effective date: "
    "each issued and outstanding Common Share, excluding Common Shares held by "
    "Dissenting Shareholders, will be transferred to Purchaser for the "
    "Consideration of $6.50 in cash, less any applicable withholdings."
    + FIGURES
    + "filler. " * 200
    + "Q: What will I receive in the Arrangement? A: If the Arrangement is "
      "completed, you will be entitled to receive the Consideration in respect "
      "of each Common Share, which is equal to $6.50 in cash. This represents a "
      "premium of 44.0% to the 20-trading-day volume weighted average price."
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
    assert proxy.section_window(NO_SECTIONS, "premium_statement") is None


def test_the_window_is_capped_so_the_document_is_never_sent() -> None:
    huge = "a premium of 23.4% was paid " + FIGURES + ("x" * 500_000)
    section = proxy.section_window(huge, "premium_statement")
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
    # The decoy comes first and carries one figure against the real clause's
    # eleven, so a position rule would have taken it and density does not.
    decoy = CASH_DEAL.index("No fractional shares will be issued")
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
    assert len(figures) == len(proxy.V1_FIELDS), "every field still gets a row"


def test_a_real_takeout_passes_the_gate() -> None:
    assert proxy.is_takeout_proxy(CASH_DEAL)
    assert proxy.is_takeout_proxy(STOCK_DEAL)


def test_a_canadian_plan_of_arrangement_passes_the_gate() -> None:
    """SunOpta was the measured miss: a real $6.50 cash takeout by KKR that the
    US-only pattern gated out. Canada does not write "converted into" -- a share
    is **transferred to** the purchaser for the consideration, and the holder is
    named rather than the share.

    A gated document costs nothing and says nothing, which makes a false negative
    the expensive direction of error.
    """
    assert proxy.is_takeout_proxy(ARRANGEMENT)


def test_the_gate_is_not_relaxed_to_any_plan_of_arrangement() -> None:
    """The wrong test, and it would have been the easy one. Coeur Mining's proxy
    and Royal Gold's both describe a plan of arrangement in which the *other*
    company's shares are acquired, and both are filings where this company is the
    buyer -- so "plan of arrangement" appears in a document that must stay gated
    out. The gate anchors on the transfer-for-consideration clause instead.
    """
    buying = ("Proposal 1: to approve the issuance of shares of Company common "
              "stock to the shareholders of Target Inc. pursuant to the Plan of "
              "Arrangement under the Business Corporations Act." + FIGURES)
    assert not proxy.is_takeout_proxy(buying)


# --- extraction, and the checking -------------------------------------


#: One cash class, correctly attributed. The shape the model is asked for.
def cash_said(low: float = 52.0, high: float | None = None,
              quote: str = "right to receive $52.00 in cash",
              attributed: str | None = "Company, Inc.",
              share_class: str = "common") -> dict:
    return {"present": True, "classes": [
        {"share_class": share_class,
         "cash": {"low": low, "high": low if high is None else high,
                  "currency": "USD"},
         "shares": None, "quote": quote, "attributed_to": attributed}]}


def test_a_stated_figure_carries_its_provenance() -> None:
    """An LLM number with no provenance is unauditable. Section, offsets, quote,
    provider, model and prompt version, on every row."""
    model = FakeModel(cash_said())
    rows = proxy.extract_consideration("acc-2", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert len(rows) == 1
    figure = rows[0]
    assert figure.reason == proxy.STATED
    assert figure.value == 52.0
    assert (figure.low, figure.high) == (52.0, 52.0)
    assert figure.component == proxy.CASH
    assert figure.currency == "USD"
    assert figure.unit == "usd_per_share"
    assert figure.section == "merger_consideration"
    assert figure.section_start is not None and figure.section_end is not None
    assert figure.section_end > figure.section_start
    assert figure.quote and "$52.00" in figure.quote
    assert figure.provider == "groq" and figure.model == "test-model"
    assert figure.prompt_version == proxy.PROMPT_VERSION
    assert figure.attributed_to == "Company, Inc."
    assert figure.attribution_ok is True
    assert figure.usable


def test_a_figure_whose_quote_is_not_in_the_text_is_rejected() -> None:
    """The failure mode this tier actually has. Rejected rather than stored, and
    counted -- the rate is how trust in a provider is earned, and it is the
    reason a small local model is an acceptable fallback at all.
    """
    model = FakeModel(cash_said(
        99.0, quote="the right to receive $99.00 in cash"))
    rows = proxy.extract_consideration("acc-3", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert [r.reason for r in rows] == [proxy.UNCITED]
    assert rows[0].value is None, "an unverifiable number was stored anyway"
    assert rows[0].low is None
    assert "not in the section" in (rows[0].note or "")


def test_a_quote_reflowed_by_the_model_still_verifies() -> None:
    """Models reflow whitespace when they copy. Rejecting a figure over a double
    space would make the check useless while looking strict."""
    model = FakeModel(cash_said(
        quote="right  to\n receive   $52.00 in cash"))
    rows = proxy.extract_consideration("acc-4", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert [r.reason for r in rows] == [proxy.STATED]


def test_not_stated_and_not_parsed_stay_distinct() -> None:
    """A stock-for-stock merger genuinely has no cash price per share, and
    recording that as a parse failure sends the next reader hunting for a number
    nobody wrote down."""
    absent = FakeModel({"present": False, "classes": [],
                        "why_absent": "the holder receives nothing in cash"})
    rows = proxy.extract_consideration("acc-5", STOCK_DEAL, client=absent,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert [r.reason for r in rows] == [proxy.NOT_STATED]
    assert rows[0].value is None and rows[0].low is None
    assert "cash" in (rows[0].note or "")

    garbled = FakeModel({"present": True, "classes": [
        {"share_class": "common",
         "cash": {"low": "about fifty dollars", "currency": "USD"},
         "shares": None, "quote": "x", "attributed_to": "Company"}]})
    rows = proxy.extract_consideration("acc-6", CASH_DEAL, client=garbled,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert [r.reason for r in rows] == [proxy.NOT_PARSED]
    assert rows[0].reason != proxy.NOT_STATED


# --- consideration is a structure, not a scalar -------------------------


def test_a_collar_is_a_range_and_has_no_scalar() -> None:
    """Enviri: "not to be less than $14.50 per share and not to exceed $16.50".

    **A collar recorded as its upper bound is a wrong number that looks right**,
    which is why the scalar is derived rather than stored. A midpoint would be
    worse still -- an invented figure no document states, the same mistake as
    inferring a split ratio from a price jump.
    """
    model = FakeModel(cash_said(14.50, 16.50))
    rows = proxy.extract_consideration("acc-collar", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert [r.reason for r in rows] == [proxy.STATED]
    assert (rows[0].low, rows[0].high) == (14.50, 16.50)
    assert rows[0].value is None, "one end of a collar was stored as the price"

    value, shape = proxy.scalar_consideration(proxy.considerations_from(rows))
    assert value is None
    assert shape == proxy.COLLAR
    # A range still has the two ends, which is what a reader needs.
    assert rows[0].amount is not None and rows[0].amount.is_range


def test_a_mixed_deal_is_two_rows_for_one_class() -> None:
    """Veeco: 0.265 Axcelis shares **and** $10.15. Neither half is the
    consideration, and v1's two scalar fields had no way to say so."""
    model = FakeModel({"present": True, "classes": [
        {"share_class": "common",
         "cash": {"low": 10.15, "high": 10.15, "currency": "USD"},
         "shares": {"low": 0.265, "high": 0.265},
         "quote": "right to receive $52.00 in cash",
         "attributed_to": "Company, Inc."}]})
    rows = proxy.extract_consideration("acc-mix", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert {r.component for r in rows} == {proxy.CASH, proxy.ACQUIRER_SHARES}
    assert all(r.reason == proxy.STATED for r in rows)
    # A share ratio has no currency; cash must name one.
    by = {r.component: r for r in rows}
    assert by[proxy.CASH].currency == "USD"
    assert by[proxy.ACQUIRER_SHARES].currency is None

    structs = proxy.considerations_from(rows)
    assert len(structs) == 1 and structs[0].is_mixed
    assert proxy.scalar_consideration(structs) == (None, proxy.MIXED)


def test_two_share_classes_are_two_prices() -> None:
    """FONAR: $19.00 for Common and Class B, $6.34 for Class C. One number for
    that filing is one of the two, which is a wrong answer either way."""
    model = FakeModel({"present": True, "classes": [
        {"share_class": "common", "cash": {"low": 19.0, "high": 19.0,
                                           "currency": "USD"},
         "shares": None, "quote": "right to receive $52.00 in cash",
         "attributed_to": "Company, Inc."},
        {"share_class": "Class C", "cash": {"low": 6.34, "high": 6.34,
                                            "currency": "USD"},
         "shares": None, "quote": "a premium of 23.4%",
         "attributed_to": "Company, Inc."},
    ]})
    rows = proxy.extract_consideration("acc-class", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert {r.share_class for r in rows} == {"common", "Class C"}
    structs = proxy.considerations_from(rows)
    assert len(structs) == 2
    assert proxy.scalar_consideration(structs) == (None, proxy.PER_CLASS)


def test_a_stock_deal_has_a_ratio_and_no_usd_scalar() -> None:
    model = FakeModel({"present": True, "classes": [
        {"share_class": "common", "cash": None,
         "shares": {"low": 0.6303, "high": 0.6303},
         "quote": "right to receive 0.6303 shares of Parent common stock",
         "attributed_to": "Company, Inc."}]})
    rows = proxy.extract_consideration("acc-stock", STOCK_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert [r.component for r in rows] == [proxy.ACQUIRER_SHARES]
    assert rows[0].value == 0.6303
    assert proxy.scalar_consideration(proxy.considerations_from(rows)) == (
        None, proxy.SHARES_ONLY)


def test_the_scalar_exists_only_when_the_deal_has_one() -> None:
    """Four of the fifteen takeouts measured have no single per-share price. The
    shape says *which* absence, so three different facts are not three identical
    NULLs."""
    usd = lambda lo, hi=None: proxy.Amount(lo, lo if hi is None else hi, "USD")
    assert proxy.scalar_consideration(
        [proxy.Consideration(cash=usd(29.0))]) == (29.0, proxy.SCALAR)
    assert proxy.scalar_consideration([]) == (None, proxy.NOT_READ)
    assert proxy.scalar_consideration(
        [proxy.Consideration(cash=usd(14.5, 16.5))]) == (None, proxy.COLLAR)


def test_a_range_the_wrong_way_round_is_refused() -> None:
    """The type refuses an incoherent range rather than storing it, the same way
    the table's check constraint does."""
    with pytest.raises(ValueError, match="wrong way round"):
        proxy.Amount(16.5, 14.5)
    # The model writing them swapped is a model error, not a data error, so the
    # reader normalises rather than failing the document.
    model = FakeModel(cash_said(16.5, 14.5))
    rows = proxy.extract_consideration("acc-swap", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert (rows[0].low, rows[0].high) == (14.5, 16.5)


# --- attribution: what the figure is *of* ------------------------------


def test_a_cited_merger_sub_conversion_is_rejected() -> None:
    """**The error the citation check is blind to, and the dominant one.**

    Measured over 20 real proxies: every wrong figure was genuinely in the text
    and quoted correctly. Farmer Brothers' all-cash deal came back with an
    exchange ratio of 1.0, quoting "each share of common stock of **Merger Sub**
    ... shall automatically be converted" -- boilerplate merger mechanics, read as
    what target holders receive.

    A quote proves the number was read. The attribution is what says it answers
    the question asked.
    """
    model = FakeModel({"present": True, "classes": [
        {"share_class": "common", "cash": None,
         "shares": {"low": 1.0, "high": 1.0},
         "quote": "right to receive $52.00 in cash",
         "attributed_to": "Merger Sub, Inc."}]})
    rows = proxy.extract_consideration("acc-mis", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ,
                                       filer="Farmer Brothers Co")
    assert [r.reason for r in rows] == [proxy.MISATTRIBUTED]
    assert rows[0].low is None and rows[0].value is None
    assert rows[0].attribution_ok is False
    # Kept distinct from `uncited`: they say opposite things about the provider,
    # and the remedy is a prompt in one case and a locator in the other.
    assert rows[0].reason != proxy.UNCITED
    assert "Merger Sub" in (rows[0].note or "")


def test_another_deal_inside_the_same_document_is_rejected() -> None:
    """Royal Gold's proxy carries "C$2.00 in cash per common share" -- Sandstorm
    buying Horizon, in Canadian dollars, in a filing where Royal Gold is the
    buyer. Present, quotable, and not this company's consideration.

    The two names share "gold", which is why the check agrees on a **head word**
    rather than on any shared word: a sector word is not an identity.
    """
    model = FakeModel({"present": True, "classes": [
        {"share_class": "common",
         "cash": {"low": 2.0, "high": 2.0, "currency": "CAD"},
         "shares": None, "quote": "right to receive $52.00 in cash",
         "attributed_to": "Sandstorm Gold Ltd."}]})
    rows = proxy.extract_consideration("acc-other", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ,
                                       filer="Royal Gold, Inc.")
    assert [r.reason for r in rows] == [proxy.MISATTRIBUTED]
    assert "does not name the filer" in (rows[0].note or "")


def test_a_figure_with_no_attribution_cannot_be_placed() -> None:
    model = FakeModel(cash_said(attributed=None))
    rows = proxy.extract_consideration("acc-none", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ, filer="Company Inc")
    assert [r.reason for r in rows] == [proxy.MISATTRIBUTED]
    assert "no attribution" in (rows[0].note or "")


def test_an_abbreviated_name_still_matches() -> None:
    """The cost of a false mismatch is a good figure thrown away, so the check
    has to survive how filings actually write names."""
    for attributed, filer in (("Farmer Bros. Co.", "FARMER BROTHERS CO"),
                              ("Electro-Sensors, Inc.", "ELECTRO SENSORS INC"),
                              ("the Company", "AstroNova, Inc."),
                              ("Leggett & Platt, Incorporated",
                               "LEGGETT & PLATT INC")):
        ok, why = proxy.check_attribution(attributed, filer)
        assert ok, f"{attributed!r} vs {filer!r} was rejected: {why}"


def test_an_unchecked_attribution_is_not_a_passed_one() -> None:
    """With no filer name there is nothing to compare against. Recorded as
    unchecked -- ``attribution_ok`` unset -- because "we did not look" and "we
    looked and it was fine" are different facts."""
    model = FakeModel(cash_said())
    rows = proxy.extract_consideration("acc-unchecked", CASH_DEAL, client=model,
                                       providers=ONLY_GROQ)
    assert rows[0].reason == proxy.STATED
    assert rows[0].attributed_to == "Company, Inc."


def test_a_comparables_percentile_is_not_this_deals_premium() -> None:
    """Comerica came back with 7%, quoting "premium of 7.0% and 75th percentile
    premium of 22" -- a quartile from a table of other transactions."""
    model = FakeModel({"present": True, "value": 7.0,
                       "quote": "a premium of 23.4%",
                       "attributed_to": "75th percentile of selected "
                                        "transactions",
                       "reference": "closing price"})
    figure = proxy.extract_field("acc-pct", CASH_DEAL, "premium_pct",
                                 client=model, providers=ONLY_GROQ,
                                 filer="Comerica Incorporated")
    assert figure.reason == proxy.MISATTRIBUTED
    assert figure.value is None


def test_a_premium_records_what_it_is_measured_against() -> None:
    """One filing quotes 208.5%, 231% and 84.9% for the same deal against three
    reference prices. Without the reference they are three numbers rather than
    one comparable figure."""
    model = FakeModel({"present": True, "value": 23.4,
                       "quote": "a premium of 23.4%",
                       "attributed_to": "Company, Inc.",
                       "reference": "closing price on March 2, 2026"})
    figure = proxy.extract_field("acc-ref", CASH_DEAL, "premium_pct",
                                 client=model, providers=ONLY_GROQ,
                                 filer="Company Inc")
    assert figure.reason == proxy.STATED
    assert figure.reference == "closing price on March 2, 2026"
    assert figure.attribution_ok is True


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
    assert "premium" in sent.lower()
    assert "TABLE OF CONTENTS" not in sent


def test_the_prompt_says_when_absent_is_the_right_answer() -> None:
    """Without it a model asked for a premium in a document that only tabulates
    other deals' premiums will produce one, and the citation check would pass
    because some percentage is always nearby."""
    model = FakeModel({"present": False, "value": None, "quote": None})
    proxy.extract_field("acc-10", CASH_DEAL, "premium_pct",
                        client=model, providers=ONLY_GROQ)
    assert "Report absent when" in model.prompts[0]


def test_the_consideration_is_one_call_not_two() -> None:
    """Cash and shares are not independent questions: a deal pays one, the other,
    or both. Asking separately is what made Veeco's cash read ``not_stated``
    while its ratio was extracted from the same sentence -- and it doubled the
    tokens, which on an 8,000-per-minute free tier was the binding constraint."""
    model = FakeModel(cash_said())
    proxy.extract_consideration("acc-one", CASH_DEAL, client=model,
                                providers=ONLY_GROQ, filer="Company Inc")
    assert len(model.prompts) == 1
    assert "attributed_to" in model.prompts[0]
    assert "midpoint" in model.prompts[0], (
        "nothing told the model not to average a collar")


# --- scope --------------------------------------------------------------


def test_v1_reads_consideration_and_premium_only() -> None:
    """Comps, projections and DCF ranges come after these are shown to work."""
    assert proxy.V1_FIELDS == ("consideration", "premium_pct")
    assert set(proxy.FIELDS) == {"premium_pct"}, (
        "the consideration is structured, not a scalar field")
    with pytest.raises(ValueError, match="not a scalar v1 field"):
        proxy.extract_field("a", CASH_DEAL, "dcf_discount_rate")


def test_every_field_always_gets_a_row() -> None:
    model = FakeModel({"present": False, "classes": [], "value": None,
                       "quote": None})
    figures = proxy.read("acc-11", CASH_DEAL, client=model,
                         providers=ONLY_GROQ)
    assert {f.field for f in figures} == set(proxy.V1_FIELDS)
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


def test_the_report_separates_a_bad_quote_from_a_wrong_question() -> None:
    """Two rejections that say opposite things about the provider. An uncited
    figure means the model produced text that is not in the document; a
    misattributed one means it read the document correctly and answered a
    different question. Collapsing them would hide which of the two is happening,
    and the remedies are a prompt and a locator respectively."""
    report = proxy.ReadReport(documents=2, figures=[
        proxy.Figure(accession="a", field="premium_pct", reason=proxy.UNCITED),
        proxy.Figure(accession="b", field="premium_pct",
                     reason=proxy.MISATTRIBUTED),
    ])
    text = "\n".join(report.lines())
    assert "citation check earning its keep" in text
    assert "wrong question" in text
    counts = report.by_reason("premium_pct")
    assert counts[proxy.UNCITED] == 1 and counts[proxy.MISATTRIBUTED] == 1
