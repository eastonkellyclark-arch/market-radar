"""Form 4 parsing. No network.

The transaction code carries almost the whole signal, so most of what is
asserted here is that a code survives the trip out of the XML intact and lands
on the right side of the derivative/non-derivative split.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from marketradar.signals import form4


def owner_xml(name="Liu Chang", cik="0001843196", director="true",
              officer="true", ten="false", other="false", title="CEO") -> str:
    return f"""
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>{cik}</rptOwnerCik>
            <rptOwnerName>{name}</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>{director}</isDirector>
            <isOfficer>{officer}</isOfficer>
            <isTenPercentOwner>{ten}</isTenPercentOwner>
            <isOther>{other}</isOther>
            <officerTitle>{title}</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>"""


def txn_xml(code="P", shares="10000", price="2.3788", ad="A",
            when="2026-09-03", derivative=False) -> str:
    tag = "derivativeTransaction" if derivative else "nonDerivativeTransaction"
    return f"""
        <{tag}>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>{when}</value></transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>{code}</transactionCode>
                <equitySwapInvolved>false</equitySwapInvolved>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </{tag}>"""


def doc(document_type="4", owners=None, nd=None, deriv=None,
        period="2026-09-03", issuer_cik="0001821468") -> str:
    owners = owners if owners is not None else [owner_xml()]
    nd = nd if nd is not None else [txn_xml()]
    deriv = deriv or []
    return f"""<ownershipDocument>
    <documentType>{document_type}</documentType>
    <periodOfReport>{period}</periodOfReport>
    <issuer>
        <issuerCik>{issuer_cik}</issuerCik>
        <issuerName>17 Education &amp; Technology Group Inc.</issuerName>
        <issuerTradingSymbol>YQ</issuerTradingSymbol>
    </issuer>
    {''.join(owners)}
    <nonDerivativeTable>{''.join(nd)}</nonDerivativeTable>
    <derivativeTable>{''.join(deriv)}</derivativeTable>
</ownershipDocument>"""


# --- the document -------------------------------------------------------


def test_issuer_and_period_are_read() -> None:
    f = form4.parse(doc())
    assert f.issuer_cik == "0001821468"
    assert f.issuer_symbol == "YQ"
    assert f.period_of_report == date(2026, 9, 3)
    assert "17 Education" in f.issuer_name


def test_the_ownership_xml_is_sliced_out_of_a_full_submission() -> None:
    """Submissions wrap several documents in SGML that is not XML."""
    submission = (
        "-----BEGIN PRIVACY-ENHANCED MESSAGE-----\n<SEC-HEADER>junk</SEC-HEADER>\n"
        + doc()
        + "\n</SUBMISSION>"
    )
    assert form4.parse(submission).issuer_symbol == "YQ"


def test_a_submission_with_no_ownership_document_raises() -> None:
    with pytest.raises(form4.Form4Error, match="ownershipDocument"):
        form4.parse("<SEC-HEADER>nothing here</SEC-HEADER>")


def test_malformed_xml_raises_rather_than_returning_empty() -> None:
    with pytest.raises(form4.Form4Error):
        form4.parse("<ownershipDocument><issuer></ownershipDocument>")


# --- transaction codes --------------------------------------------------


def test_an_open_market_purchase_is_recognised() -> None:
    f = form4.parse(doc(nd=[txn_xml(code="P")]))
    t = f.transactions[0]
    assert t.code == "P"
    assert t.is_open_market_purchase
    assert f.has_open_market_purchase
    assert t.shares == Decimal("10000")
    assert t.price == Decimal("2.3788")
    assert t.value == Decimal("23788.0000")


@pytest.mark.parametrize("code", sorted(form4.COMPENSATION_CODES))
def test_compensation_codes_are_not_purchases(code: str) -> None:
    """A, M and F are the high-volume codes and none of them is a decision.

    Every vesting date fires a burst of them from several insiders at once;
    counting those as a cluster turns the payroll calendar into a buy signal.
    """
    f = form4.parse(doc(nd=[txn_xml(code=code)]))
    assert not f.transactions[0].is_open_market_purchase
    assert not f.has_open_market_purchase


def test_a_sale_is_not_a_purchase() -> None:
    f = form4.parse(doc(nd=[txn_xml(code="S", ad="D")]))
    assert not f.has_open_market_purchase


def test_a_derivative_P_does_not_count() -> None:
    """The cluster rule is code P in the non-derivative table specifically."""
    f = form4.parse(doc(nd=[], deriv=[txn_xml(code="P", derivative=True)]))
    assert f.transactions[0].code == "P"
    assert f.transactions[0].is_derivative
    assert not f.transactions[0].is_open_market_purchase
    assert not f.has_open_market_purchase


def test_both_tables_are_read_and_kept_apart() -> None:
    f = form4.parse(doc(nd=[txn_xml(code="P")],
                        deriv=[txn_xml(code="M", derivative=True)]))
    assert len(f.transactions) == 2
    assert [t.is_derivative for t in f.transactions] == [False, True]
    assert len(f.purchases) == 1


def test_a_lowercase_code_is_normalised() -> None:
    assert form4.parse(doc(nd=[txn_xml(code="p")])).has_open_market_purchase


def test_a_transaction_with_no_price_has_no_value() -> None:
    f = form4.parse(doc(nd=[txn_xml(code="P", price="")]))
    assert f.transactions[0].price is None
    assert f.transactions[0].value is None


# --- roles --------------------------------------------------------------


def test_officer_and_director_are_insiders() -> None:
    f = form4.parse(doc(owners=[owner_xml(officer="true", director="false",
                                          ten="false")]))
    assert f.owners[0].is_insider
    assert f.owners[0].roles == ("officer",)
    assert f.owners[0].officer_title == "CEO"


def test_a_ten_percent_owner_alone_is_not_an_insider() -> None:
    """A fund adjusting an allocation is a different signal from a CFO buying."""
    f = form4.parse(doc(owners=[owner_xml(officer="false", director="false",
                                          ten="true")]))
    assert not f.owners[0].is_insider
    assert f.owners[0].is_ten_percent
    assert f.owners[0].roles == ("ten_percent",)


def test_multiple_roles_are_all_kept() -> None:
    """A CEO who also holds 12% is an insider who is large, not a fund."""
    f = form4.parse(doc(owners=[owner_xml(officer="true", director="true",
                                          ten="true")]))
    o = f.owners[0]
    assert o.is_insider and o.is_ten_percent
    assert o.roles == ("officer", "director", "ten_percent")


def test_a_joint_filing_keeps_every_owner() -> None:
    f = form4.parse(doc(owners=[owner_xml(name="A", cik="0000000001"),
                                owner_xml(name="B", cik="0000000002")]))
    assert [o.name for o in f.owners] == ["A", "B"]


# --- amendments ---------------------------------------------------------


def test_an_amendment_is_flagged() -> None:
    assert form4.parse(doc(document_type="4/A")).is_amendment
    assert not form4.parse(doc(document_type="4")).is_amendment


def test_an_amendment_supersedes_the_original() -> None:
    """Both carry their own accession, so a naive dedupe keeps both and the
    transaction is counted twice."""
    original = form4.parse(doc(document_type="4"), accession="0000000000-26-000001")
    amended = form4.parse(doc(document_type="4/A"), accession="0000000000-26-000002")

    kept = form4.supersede([original, amended])
    assert len(kept) == 1
    assert kept[0].is_amendment


def test_an_amendment_for_a_different_period_supersedes_nothing() -> None:
    original = form4.parse(doc(document_type="4", period="2026-09-03"))
    amended = form4.parse(doc(document_type="4/A", period="2026-08-03"))
    assert len(form4.supersede([original, amended])) == 2


def test_an_amendment_for_a_different_owner_supersedes_nothing() -> None:
    original = form4.parse(doc(owners=[owner_xml(name="A", cik="1")]))
    amended = form4.parse(doc(document_type="4/A",
                              owners=[owner_xml(name="B", cik="2")]))
    assert len(form4.supersede([original, amended])) == 2


def test_an_orphan_amendment_is_kept() -> None:
    """The original is simply outside the batch. Dropping the correction too
    would lose the filing entirely."""
    amended = form4.parse(doc(document_type="4/A"))
    assert form4.supersede([amended]) == [amended]


def test_supersede_leaves_unamended_filings_alone() -> None:
    a = form4.parse(doc(owners=[owner_xml(name="A", cik="1")]))
    b = form4.parse(doc(owners=[owner_xml(name="B", cik="2")]))
    assert len(form4.supersede([a, b])) == 2


# --- Rule 10b5-1 --------------------------------------------------------


def doc_with_plan(flag: str) -> str:
    return doc().replace(
        "<periodOfReport>", f"<aff10b5One>{flag}</aff10b5One><periodOfReport>"
    )


@pytest.mark.parametrize("flag", ["true", "1", "TRUE", "Y"])
def test_the_plan_box_is_read_in_every_spelling(flag: str) -> None:
    """Real filings use all of these. Sampled 60 and saw false, 0, true and 1."""
    f = form4.parse(doc_with_plan(flag))
    assert f.aff10b5_one and f.is_planned


@pytest.mark.parametrize("flag", ["false", "0", "", "no"])
def test_an_unset_plan_box_is_not_planned(flag: str) -> None:
    assert not form4.parse(doc_with_plan(flag)).is_planned


def test_a_filing_with_no_plan_element_is_not_planned() -> None:
    assert not form4.parse(doc()).is_planned


def test_a_footnote_mention_is_recorded_but_is_not_the_flag() -> None:
    """Weaker evidence and unattributed: the footnote may be about a sale.

    Kept separate so the headline number stays the filer's own tick-box.
    """
    xml = doc().replace(
        "</issuer>",
        "</issuer><footnotes><footnote id='F1'>Sold under a Rule 10b5-1 "
        "trading plan adopted 2026-01-05.</footnote></footnotes>",
    )
    f = form4.parse(xml)
    assert f.mentions_10b5_1
    assert not f.aff10b5_one
    assert not f.is_planned


def test_a_planned_purchase_is_still_an_open_market_purchase() -> None:
    """is_planned annotates; it does not reclassify the transaction code."""
    f = form4.parse(doc_with_plan("1"))
    assert f.has_open_market_purchase
    assert f.is_planned


# --- form types ---------------------------------------------------------


def test_amendments_are_in_the_default_form_type_set() -> None:
    """A filter of exactly "4" excludes every 4/A. That is how a week's
    distribution came back reporting zero amendments when there were 106."""
    assert set(form4.FORM4_TYPES) == {"4", "4/A"}


def test_the_daily_index_reads_both_form_types(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(form4, "user_agent", lambda: "x you@example.org")
    index = (
        "Form Type   Company Name       CIK  Date Filed  File Name\n"
        "-------------------------------------------------------\n"
        "4           ACME CORP          1    2026-09-03  edgar/data/1/a.txt\n"
        "4/A         ACME CORP          1    2026-09-03  edgar/data/1/b.txt\n"
        "8-K         ACME CORP          1    2026-09-03  edgar/data/1/c.txt\n"
        "3           ACME CORP          1    2026-09-03  edgar/data/1/d.txt\n"
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=index))
    )
    got = form4.daily_index_paths(date(2026, 9, 3), client=client)
    assert got == ["edgar/data/1/a.txt", "edgar/data/1/b.txt"]

    only_4 = form4.daily_index_paths(date(2026, 9, 3), "4", client=client)
    assert only_4 == ["edgar/data/1/a.txt"]


def test_a_missing_daily_index_is_a_weekend_not_an_error(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(form4, "user_agent", lambda: "x you@example.org")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )
    assert form4.daily_index_paths(date(2026, 9, 5), client=client) == []


# --- clusters -----------------------------------------------------------


def owner(cik, officer=False, ten=False, name=None):
    return form4.Owner(cik=cik, name=name or f"Person {cik}",
                       is_officer=officer, is_director=False,
                       is_ten_percent=ten)


def buy_txn(value, when, code="P", derivative=False):
    return form4.Transaction(
        code=code, shares=Decimal(str(value)), price=Decimal(1),
        acquired_disposed="A", security_title="CS",
        transaction_date=when, is_derivative=derivative)


def filing(owners, txns, cik="0000000001", symbol="ACME",
           period=date(2020, 1, 1), name="Acme Corp", planned=False):
    return form4.Form4(
        accession="a", document_type="4", period_of_report=period,
        issuer_cik=cik, issuer_name=name, issuer_symbol=symbol,
        owners=owners, transactions=txns, aff10b5_one=planned)


D1, D3, D9 = date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 9)


def test_two_insiders_inside_the_window_cluster() -> None:
    found = form4.clusters([
        filing([owner("1", officer=True)], [buy_txn(30_000, D1)]),
        filing([owner("2", officer=True)], [buy_txn(30_000, D3)]),
    ])
    assert len(found[form4.INSIDER]) == 1
    c = found[form4.INSIDER][0]
    assert c.value == Decimal(60_000)
    assert c.buyers == {"1", "2"}


def test_buys_outside_the_window_do_not_cluster() -> None:
    found = form4.clusters([
        filing([owner("1", officer=True)], [buy_txn(30_000, D1)]),
        filing([owner("2", officer=True)], [buy_txn(30_000, D9)]),
    ])
    assert found[form4.INSIDER] == []


def test_the_window_keys_on_transaction_date_not_the_period() -> None:
    """A late-filed Form 4 reports an old trade.

    Grouping on periodOfReport pulled a 2025-10-24 transaction into a
    September window in a sample week.
    """
    buys = form4.purchases([
        filing([owner("1", officer=True)], [buy_txn(1, date(2025, 10, 24))],
               period=date(2026, 9, 1))
    ])
    assert buys[0].on == date(2025, 10, 24)


def test_a_cluster_below_its_floor_is_dropped() -> None:
    fs = [filing([owner("1", officer=True)], [buy_txn(10, D1)]),
          filing([owner("2", officer=True)], [buy_txn(10, D3)])]
    assert form4.clusters(fs)[form4.INSIDER] == []
    assert len(form4.clusters(fs, insider_floor=Decimal(1))[form4.INSIDER]) == 1


def test_the_two_floors_are_independent() -> None:
    """Sample-week medians were $339k and $26.4M -- 78x apart."""
    assert form4.DEFAULT_TENPCT_FLOOR > form4.DEFAULT_INSIDER_FLOOR * 10


def test_someone_who_is_both_counts_as_an_insider() -> None:
    buys = form4.purchases([
        filing([owner("9", officer=True, ten=True)], [buy_txn(1, D1)])])
    assert buys[0].role == form4.INSIDER


def test_a_joint_filing_divides_the_value_rather_than_double_counting() -> None:
    """The same shares reported by several persons are one purchase."""
    buys = form4.purchases([
        filing([owner("1", officer=True), owner("2", officer=True)],
               [buy_txn(100, D1)])])
    assert sum(b.value for b in buys) == Decimal(100)


def test_only_purchases_reach_a_cluster() -> None:
    fs = [filing([owner("1", officer=True)], [buy_txn(1e9, D1, code="A")]),
          filing([owner("2", officer=True)], [buy_txn(1e9, D3, code="M")])]
    assert form4.clusters(fs)[form4.INSIDER] == []


def test_placeholder_symbols_are_not_tickers() -> None:
    """A company with no listed equity files N/A literally."""
    for raw in ("N/A", "NONE", "-", "", "na"):
        assert form4.display_symbol(raw) is None
    assert form4.display_symbol("rsg") == "RSG"


# --- the fund heuristic -------------------------------------------------


def fund_cluster(names, symbol, issuer="Some Fund LP"):
    owners = [owner(str(i), ten=True, name=n) for i, n in enumerate(names)]
    return form4.clusters(
        [filing(owners, [buy_txn(10_000_000, D1)], symbol=symbol, name=issuer)],
        tenpct_floor=Decimal(1),
    )[form4.TEN_PERCENT][0]


def test_funds_buying_each_other_are_marked() -> None:
    c = fund_cluster(["BlueArc Capital Management, LLC",
                      "Pantheon Infrastructure Fund"], "PBLSX")
    flagged, why = c.fund_flag
    assert flagged
    assert "all buyers are entities" in why


def test_a_fund_alongside_a_person_is_not_marked() -> None:
    """Cascade Investment buying beside Bill Gates is an ordinary cluster."""
    c = fund_cluster(["CASCADE INVESTMENT, L.L.C.", "GATES WILLIAM H III"],
                     "RSG", issuer="Republic Services Inc")
    assert c.fund_flag[0] is False


def test_an_issuer_with_no_listed_equity_is_a_signal_not_a_ticker() -> None:
    c = fund_cluster(["Corbin Capital Partners, L.P.",
                      "CCP Investment Accelerator, LLC"], "NONE")
    flagged, why = c.fund_flag
    assert flagged
    assert "no listed equity" in why


def test_executives_buying_are_never_marked_as_funds() -> None:
    """The expensive mistake is a real insider cluster labelled as noise."""
    owners = [owner("1", officer=True, name="HIGGINBOTHAM RICHARD A"),
              owner("2", officer=True, name="Slotkin Judy S")]
    c = form4.clusters(
        [filing(owners, [buy_txn(500_000, D1)], symbol=None)],
    )[form4.INSIDER][0]
    assert c.fund_flag[0] is False


def test_flagged_clusters_are_kept_not_dropped() -> None:
    """Marked, never dropped -- excluding them means never learning whether
    they are noise."""
    c = fund_cluster(["A Capital LLC", "B Management LP"], "PBLSX")
    assert c.fund_flag[0]
    assert c.value > 0 and len(c.buys) == 2


def test_a_weekend_is_skipped_without_a_request(monkeypatch) -> None:
    """SEC answers a weekend index with 403, not 404.

    A --days window that spans a Saturday aborted the whole run. Skipping
    without asking also saves the request.
    """
    import httpx

    monkeypatch.setattr(form4, "user_agent", lambda: "x you@example.org")
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, text="")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert form4.daily_index_paths(date(2026, 9, 5), client=client) == []  # Sat
    assert form4.daily_index_paths(date(2026, 9, 6), client=client) == []  # Sun
    assert asked == [], "no request should be made for a weekend"


@pytest.mark.parametrize("status", [403, 404])
def test_a_missing_weekday_index_is_a_holiday_not_a_failure(
    monkeypatch, status
) -> None:
    """A real UA block fails every request, not one date -- and the UA is
    validated before any of them."""
    import httpx

    monkeypatch.setattr(form4, "user_agent", lambda: "x you@example.org")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status))
    )
    assert form4.daily_index_paths(date(2026, 9, 7), client=client) == []
