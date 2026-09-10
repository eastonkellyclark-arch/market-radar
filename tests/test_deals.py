"""8-K deal extraction. No network.

Item 1.01 is mostly not M&A -- roughly 16% of it, measured over a full week
of filings -- so most of what is asserted here is that the *non*-deals stay
out: credit facilities, private placements, leases, securitisations, and
employment agreements all use contract vocabulary that overlaps with M&A.

The cases with a company name in them are shapes taken from real filings in
the 2026-08-31 week, kept because each one broke an earlier version.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from marketradar.signals import deals


# --- the header ---------------------------------------------------------

HEADER = """
<html><body><pre>
ACCESSION NUMBER:		0000006845-26-000087
CONFORMED SUBMISSION TYPE:	8-K
CONFORMED PERIOD OF REPORT:	20260902
ITEM INFORMATION:		Entry into a Material Definitive Agreement
ITEM INFORMATION:		Regulation FD Disclosure
ITEM INFORMATION:		Financial Statements and Exhibits
FILED AS OF DATE:		20260903
		STANDARD INDUSTRIAL CLASSIFICATION:	PLASTICS [2821]
&lt;DOCUMENT&gt;
&lt;TYPE&gt;8-K
&lt;FILENAME&gt;apog-20260902.htm
&lt;DOCUMENT&gt;
&lt;TYPE&gt;EX-2.1
&lt;FILENAME&gt;ex21.htm
&lt;DOCUMENT&gt;
&lt;TYPE&gt;EX-99.1
&lt;FILENAME&gt;ex991.htm
</pre></body></html>
"""


def filing(**over):
    base = dict(
        accession="0000006845-26-000087", cik="0000006845", company="APOGEE",
        form="8-K", filed=date(2026, 9, 3), base="https://x/",
    )
    base.update(over)
    return deals.parse_header(HEADER, **base)


def test_header_yields_items_types_and_sic() -> None:
    f = filing()
    assert f.items == ("1.01", "7.01", "9.01")
    assert f.doc_types == ("8-K", "EX-2.1", "EX-99.1")
    assert f.primary == "apog-20260902.htm"
    assert f.sic == "2821"
    assert f.event_date == date(2026, 9, 2)
    assert f.is_deal_item


def test_exhibit_signal_is_the_filers_own_classification() -> None:
    """EX-2.x is Reg S-K 601(b)(2)'s slot for a plan of acquisition."""
    assert filing().exhibit_signal is True

    no_ex2 = HEADER.replace("EX-2.1", "EX-10.1")
    f = deals.parse_header(
        no_ex2, accession="a", cik="1", company="X", form="8-K",
        filed=date(2026, 9, 3), base="https://x/",
    )
    assert f.exhibit_signal is False


def test_ex_21_subsidiary_list_is_not_an_ex_2_exhibit() -> None:
    """EX-21 is the subsidiary list. A prefix match on "EX-2" would take it."""
    f = deals.parse_header(
        HEADER.replace("EX-2.1", "EX-21"), accession="a", cik="1", company="X",
        form="8-K", filed=date(2026, 9, 3), base="https://x/",
    )
    assert f.exhibit_signal is False


def test_unmapped_item_names_are_reported_not_dropped() -> None:
    html = HEADER.replace(
        "Regulation FD Disclosure", "Something EDGAR Renamed Last Year"
    )
    numbers, unknown = deals.parse_items(html)
    assert numbers == ("1.01", "9.01")
    assert unknown == ("Something EDGAR Renamed Last Year",)


def test_html_escaped_item_names_still_map() -> None:
    """index-headers.html escapes the header; &#39; broke 4.01 on first run."""
    html = "ITEM INFORMATION:  Changes in Registrant&#39;s Certifying Accountant"
    numbers, unknown = deals.parse_items(html)
    assert numbers == ("4.01",)
    assert unknown == ()


# --- the section finder -------------------------------------------------


def test_section_prefers_the_caption_over_a_cross_reference() -> None:
    """8-Ks say "the disclosure under Item 1.01 is incorporated by reference"
    from later items. Taking the last mention picked up that boilerplate."""
    text = (
        "Item 1.01 Entry into a Material Definitive Agreement. On August 30, "
        "2026, the Company entered into an Agreement and Plan of Merger with "
        "Foo Holdings, Inc., a Delaware corporation. " + "x" * 200 +
        " Item 7.01 Regulation FD Disclosure. The information in Item 1.01 is "
        "incorporated herein by reference."
    )
    body = deals.section(text, "1.01")
    assert "Agreement and Plan of Merger" in body
    assert "incorporated herein by reference" not in body


def test_section_survives_nbsp_between_number_and_caption() -> None:
    """Entity spellings, not characters: &#160; hid 28 sections in 171."""
    html = (
        "<p>Item 1.01&#160;&#160;Entry into a Material Definitive Agreement</p>"
        "<p>The Company entered into an Asset Purchase Agreement with "
        "Bar LLC for an aggregate purchase price of $12.5 million.</p>"
        + "<p>" + "y" * 200 + "</p>"
    )
    body = deals.section(deals.visible(html), "1.01")
    assert "Asset Purchase Agreement" in body


def test_visible_strips_tags_before_decoding_entities() -> None:
    """Unescaping first would turn a literal &lt; into a tag and eat prose."""
    assert "a < b and the deal closed" in deals.visible(
        "<p>a &lt; b and the deal closed</p>"
    )


# --- the text classifier ------------------------------------------------


@pytest.mark.parametrize("prose,expected", [
    ("entered into an Agreement and Plan of Merger", "m_and_a"),
    ("entered into a Membership Interest Purchase Agreement", "m_and_a"),
    ("entered into a Business Combination Agreement", "m_and_a"),
    ("entered into a Tenth Amendment to Credit Agreement", "debt"),
    ("entered into a Securities Purchase Agreement with the investors",
     "equity_raise"),
    ("entered into an At-The-Market sales agreement", "equity_raise"),
    ("issued the Commercial Mortgage Pass-Through Certificates pursuant to a "
     "Pooling and Servicing Agreement", "securitization"),
    ("entered into an Employment Agreement with its chief executive",
     "employment"),
    ("agreed to lease the entirety of four buildings under the Leases",
     "real_estate"),
    ("adopted a stockholder rights plan", "governance"),
    ("entered into an Aircraft Management Services Agreement", "commercial"),
])
def test_text_signal_buckets(prose: str, expected: str) -> None:
    assert deals.text_signal(prose)[0] == expected


def test_a_tie_is_ambiguous_rather_than_arbitrary() -> None:
    """An unweighted pass let ten non-deals in forty tie their way to M&A."""
    prose = "a Merger Agreement and a Credit Agreement"
    bucket, scores = deals.text_signal(prose)
    assert bucket is None
    assert scores["m_and_a"] == scores["debt"]


def test_no_match_is_none_not_a_default_bucket() -> None:
    assert deals.text_signal("The board met on Tuesday.") == (None, {})


# --- value, and the reason code -----------------------------------------


def test_value_requires_price_context() -> None:
    """A bare largest-figure rule takes escrows, break fees and par values."""
    assert deals.value("a termination fee of $4,000,000 applies") == (None, None)
    amount, phrase = deals.value(
        "for an aggregate purchase price of $12.5 million in cash"
    )
    assert amount == Decimal("12500000")
    assert phrase == "$12.5 million"


def test_value_rejects_market_sizing_copy() -> None:
    """A nano-cap's press release announced a $500 billion "deal" this way."""
    assert deals.value(
        "total consideration in a global market expected to reach $500 billion",
        strict=True,
    ) == (None, None)


def test_exhibit_text_needs_a_deal_cue() -> None:
    """The body is contractual; a press release talks about many numbers."""
    promo = "the total consideration paid to shareholders was $50 million"
    assert deals.value(promo, strict=True)[0] == Decimal("50000000")
    vague = "total revenue guidance of $50 million"
    assert deals.value(vague, strict=True) == (None, None)


def test_value_band_rejects_implausible_parses() -> None:
    assert deals.value("aggregate consideration of $1,200")[0] is None
    assert deals.value("purchase price of $900 billion")[0] is None


# --- what the schema promises -------------------------------------------


def body(prose: str) -> str:
    return f"<p>Item 1.01 Entry into a Material Definitive Agreement</p><p>{prose}</p>"


def test_absent_value_is_not_stated_never_a_bare_null() -> None:
    """The reason code is what stops a NULL being read as zero."""
    deal = deals.extract(filing(), body(
        "entered into an Agreement and Plan of Merger with Foo Holdings, Inc."
        + " " * 1 + "z" * 200
    ))
    assert deal.value_usd is None
    assert deal.value_basis == "not_stated"


def test_there_is_no_undisclosed_basis() -> None:
    """Measured: "terms were not disclosed" occurred 0 times in 31 M&A
    filings and 0 times in their 19 press releases. A basis the extractor
    can never emit would be a permanently empty column."""
    sql = (deals.__file__.rsplit("marketradar", 1)[0]
           + "../sql/005_deals.sql")
    import pathlib
    text = pathlib.Path(sql).resolve().read_text(encoding="utf-8")
    assert "'undisclosed'" not in text
    assert "not_stated" in text and "not_parsed" in text


def test_value_from_an_exhibit_is_labelled_as_such() -> None:
    deal = deals.extract(
        filing(),
        body("entered into an Agreement and Plan of Merger with Foo Holdings, "
             "Inc. " + "z" * 200),
        exhibit_texts=[
            "the acquisition values the company at a total consideration of "
            "$250 million"
        ],
    )
    assert deal.value_usd == Decimal("250000000")
    assert deal.value_basis == "stated_exhibit"


def test_both_classifiers_agreeing_is_recorded() -> None:
    deal = deals.extract(filing(), body(
        "entered into an Agreement and Plan of Merger with Foo Holdings, Inc."
        + " " + "z" * 200))
    assert deal.exhibit_signal is True
    assert deal.text_signal == "m_and_a"
    assert deal.classifiers_agree is True


def test_disagreement_is_a_candidate_not_a_rejection() -> None:
    """Both single-signal cases are real, so neither is dropped."""
    text_only = deals.extract(
        deals.parse_header(
            HEADER.replace("EX-2.1", "EX-10.1"), accession="a", cik="1",
            company="X", form="8-K", filed=date(2026, 9, 3), base="https://x/",
        ),
        body("entered into an Agreement and Plan of Merger with Foo Holdings, "
             "Inc. " + "z" * 200),
    )
    assert text_only.is_candidate
    assert text_only.classifiers_agree is False

    exhibit_only = deals.extract(filing(), body(
        "entered into a Tenth Amendment to Credit Agreement " + "z" * 200))
    assert exhibit_only.is_candidate
    assert exhibit_only.classifiers_agree is False


def test_a_credit_facility_with_no_exhibit_is_not_a_candidate() -> None:
    """31% of Item 1.01 is debt. It must not reach the table."""
    deal = deals.extract(
        deals.parse_header(
            HEADER.replace("EX-2.1", "EX-10.1"), accession="a", cik="1",
            company="X", form="8-K", filed=date(2026, 9, 3), base="https://x/",
        ),
        body("entered into a Tenth Amendment to Credit Agreement providing a "
             "revolving credit facility " + "z" * 200),
    )
    assert deal.text_signal == "debt"
    assert not deal.is_candidate


# --- deal_type ----------------------------------------------------------


def test_sic_6770_marks_a_spac_without_reading_prose() -> None:
    html = HEADER.replace("PLASTICS [2821]", "BLANK CHECKS [6770]")
    f = deals.parse_header(html, accession="a", cik="1", company="X",
                           form="8-K", filed=date(2026, 9, 3), base="https://x/")
    deal = deals.extract(f, body(
        "entered into a Business Combination Agreement " + "z" * 200))
    assert deal.deal_type == "spac"


def test_operating_acquisition_is_not_a_spac() -> None:
    deal = deals.extract(filing(), body(
        "entered into an Agreement and Plan of Merger with Foo Holdings, Inc. "
        + "z" * 200))
    assert deal.deal_type == "operating"


def test_securitization_is_separated_from_operating_deals() -> None:
    f = deals.parse_header(
        HEADER.replace("EX-2.1", "EX-10.1"), accession="a", cik="1",
        company="X", form="8-K", filed=date(2026, 9, 3), base="https://x/")
    deal = deals.extract(f, body(
        "sold the Class A-1 Asset Backed Notes pursuant to a Pooling and "
        "Servicing Agreement " + "z" * 200))
    assert deal.deal_type == "securitization"
    assert not deal.is_candidate


# --- parties ------------------------------------------------------------


def test_merger_sub_is_never_the_counterparty() -> None:
    """Vertiv's buyer read as "Vultra Merger Sub, Inc." on the first run."""
    role, other, *_ = deals.parties(
        "Buyer and Vultra Merger Sub, Inc., a Delaware corporation, and "
        "Vertiv Corporation entered into an Agreement and Plan of Merger with "
        "Waylay Holdings, Inc., a Delaware corporation",
        "SOMEONE ELSE",
    )
    assert other is not None
    assert "Merger Sub" not in other


def test_unclear_role_leaves_acquirer_and_target_null() -> None:
    """A wrong acquirer is worse than a missing one."""
    role, other, acquirer, target, basis = deals.parties(
        "entered into an agreement with Foo Holdings, Inc., a Delaware "
        "corporation", "BAR CORP")
    assert role == "party"
    assert acquirer is None and target is None
    assert basis == "counterparty_only"


def test_seller_role_puts_the_filer_on_the_target_side() -> None:
    role, other, acquirer, target, basis = deals.parties(
        "the Company, as seller, agreed to sell the business to Foo Holdings, "
        "Inc., a Delaware corporation", "BAR CORP")
    assert role == "seller"
    assert target == "BAR CORP"
    assert acquirer == other
    assert basis == "derived_from_role"


# --- target financials --------------------------------------------------


def test_rule_305_is_found_under_item_901_not_item_101() -> None:
    """Scoping the search to the deal section found it 0 times in 39."""
    html = (
        "<p>Item 1.01 Entry into a Material Definitive Agreement</p>"
        "<p>entered into an Agreement and Plan of Merger with Foo Holdings, "
        "Inc. " + "z" * 200 + "</p>"
        "<p>Item 9.01 Financial Statements and Exhibits</p>"
        "<p>The financial statements required by Item 9.01(a) will be filed "
        "by amendment not later than 71 calendar days after the date of "
        "this report.</p>"
    )
    deal = deals.extract(filing(), html)
    assert deal.target_financials == "rule_305_promised"


def test_no_target_financials_is_the_common_case() -> None:
    """Measured at 9.7%: the multiples usually cannot be computed at all."""
    deal = deals.extract(filing(), body(
        "entered into an Agreement and Plan of Merger with Foo Holdings, Inc. "
        + "z" * 200))
    assert deal.target_financials == "none_disclosed"


def test_load_deduplicates_on_accession() -> None:
    """One 8-K filed by a parent and a subsidiary is listed under both CIKs.
    Both copies in one batch made Postgres reject the whole statement:
    "ON CONFLICT DO UPDATE command cannot affect row a second time"."""
    captured: list[str] = []

    class FakeCon:
        def execute(self, sql, params=None):
            if params and isinstance(params[0], str):
                captured.append(params[0])
            return self

        def fetchall(self):
            return [(0,)]

        def sql(self, _):
            return None

    import marketradar.storage as _storage

    real = _storage.postgres_attached
    _storage.postgres_attached = lambda con: True
    try:
        one = deals.extract(filing(), body(
            "entered into an Agreement and Plan of Merger with Foo Holdings, "
            "Inc. " + "z" * 200))
        with pytest.raises(Exception):
            # assert_fresh will object to the fake relation; what matters is
            # the SQL built before it, captured above.
            deals.load([one, one], con=FakeCon())
    finally:
        _storage.postgres_attached = real

    inserts = [s for s in captured if s.startswith("insert into deals")]
    assert len(inserts) == 1
    assert inserts[0].count("'0000006845-26-000087'") == 1
