"""Acceptance test 3: both taxonomies parse, OneD/FourD kept distinct."""

from __future__ import annotations

import pytest

from india_xbrl.models import TaxonomyFamily
from india_xbrl.reader import ReaderError, quarter_context_ids, read_instance, ytd_context_ids


def test_bse_fin_fixture_parses(fixture_bse_fin: bytes) -> None:
    summary = read_instance(fixture_bse_fin)
    assert summary.taxonomy_family is TaxonomyFamily.BSE_FIN
    assert "in-bse-fin" in summary.detected_prefixes
    assert summary.schema_ref == "in-bse-fin-2021-03-31.xsd"
    # ~112 facts in real files; fixture carries 7.
    assert len(summary.facts) == 7


def test_capmkt_fixture_parses(fixture_capmkt: bytes) -> None:
    summary = read_instance(fixture_capmkt)
    assert summary.taxonomy_family is TaxonomyFamily.CAPMKT
    assert "in-capmkt" in summary.detected_prefixes
    assert summary.schema_ref == "in-capmkt-ent-2026-01-31.xsd"


def test_oned_and_fourd_kept_distinct(fixture_bse_fin: bytes, fixture_capmkt: bytes) -> None:
    for raw in (fixture_bse_fin, fixture_capmkt):
        summary = read_instance(raw)
        quarter_ids = quarter_context_ids(summary)
        ytd_ids = ytd_context_ids(summary)
        assert quarter_ids == ["OneD"]
        assert ytd_ids == ["FourD"]

        # The same concept must appear under both contexts with *different*
        # values -- conflating them would double/halve flow values.
        rev = {
            f.context_ref: f.value
            for f in summary.facts
            if f.local_name == "RevenueFromOperations"
        }
        assert set(rev) == {"OneD", "FourD"}
        assert rev["OneD"] != rev["FourD"]

        # facts_for_context returns the quarter's facts; the YTD context
        # carries its own (superset in real filings).
        one_facts = {f.local_name for f in summary.facts_for_context("OneD")}
        assert "RevenueFromOperations" in one_facts
        four_facts = {f.local_name for f in summary.facts_for_context("FourD")}
        assert "RevenueFromOperations" in four_facts


def test_instant_context_is_marked(fixture_bse_fin: bytes) -> None:
    summary = read_instance(fixture_bse_fin)
    n1d = summary.contexts["N1D"]
    assert n1d.is_instant
    assert n1d.instant == "2021-03-31"


def test_hyphenated_prefix_is_required_in_patterns() -> None:
    """The in-bse-fin prefix contains hyphens; a [A-Za-z0-9_]+ regex finds
    nothing. Our NCName pattern must accept hyphens."""
    from india_xbrl.reader import NCNAME_RE

    assert NCNAME_RE.match("in-bse-fin")
    assert NCNAME_RE.match("in-capmkt")
    assert not NCNAME_RE.match("0invalid")
    assert not NCNAME_RE.match("bad:prefix")


def test_not_xml_raises_reader_error() -> None:
    with pytest.raises(ReaderError):
        read_instance(b"<html><body>not xbrl</body></html>".lstrip())


def test_namespaces_are_recorded_not_silently_dropped(fixture_capmkt: bytes) -> None:
    summary = read_instance(fixture_capmkt)
    # Standard namespaces resolved; nothing unparseable was dropped silently.
    assert summary.dropped_namespaces == ()
