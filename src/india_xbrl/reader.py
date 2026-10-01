"""Read XBRL instance documents: taxonomy detection and context extraction.

This is deliberately *not* a financial-statement parser. v1 reads just enough
of the instance to (a) identify the taxonomy family and (b) index facts per
context, keeping the discrete quarter (``OneD``) and the cumulative
year-to-date (``FourD``) contexts distinct. Conflating them silently doubles
or halves every flow value, so they are separate keys everywhere here.

Two taxonomies exist and the new one is NOT backwards compatible:

======================  ==========================  =============
                        legacy "Financial Results"  "Integrated"
======================  ==========================  =============
taxonomy                in-bse-fin (BSE)            in-capmkt (SEBI)
facts / filing          ~112                        ~931 (~144 contexts)
coverage                FY2015-16 onward            2025+ only
======================  ==========================  =============

Both are plain XBRL 2.1 (not iXBRL). The ``in-bse-fin`` prefix **contains
hyphens** -- a naive ``[A-Za-z0-9_]+`` pattern silently matches nothing, so
every prefix pattern here allows ``-``.

``in-ind-as`` is claimed by a third-party library to exist but was seen in no
actual file; we treat it as unconfirmed and do not build on it.

Taxonomy resolution is broken upstream: ``schemaRef`` is relative and neither
nsearchives.nseindia.com nor sebi.gov.in serves the .xsd, so a strict
DTS-resolving parser cannot resolve the taxonomy from the instance alone. See
:mod:`india_xbrl.taxonomy` for the side-loaded SEBI package (primary) and
namespace-agnostic local-name matching (fallback, used here).
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from india_xbrl.models import TaxonomyFamily

#: Namespace prefixes seen in real filings. Hyphens are legal in NCNames and
#: appear in the wild (in-bse-fin); XML NCNameChar excludes ':' only.
NCNAME_RE = re.compile(r"^[A-Za-z_][\w.\-]*$")

BSE_FIN_NS = "http://www.bseindia.com/xbrl/fin"
CAPMKT_NS = "http://www.sebi.gov.in/xbrl/2025-01-31"

_XBRLI_NS = "http://www.xbrl.org/2003/instance"
_XBRLI = f"{{{_XBRLI_NS}}}"
_LINK = "{http://www.xbrl.org/2003/linkbase}"
_XLINK = "{http://www.w3.org/1999/xlink}"
_XSI = "{http://www.w3.org/2001/XMLSchema-instance}"

#: Known taxonomy prefixes, hyphen-aware. Matched case-insensitively.
_KNOWN_PREFIXES: dict[str, TaxonomyFamily] = {
    "in-bse-fin": TaxonomyFamily.BSE_FIN,
    "in-capmkt": TaxonomyFamily.CAPMKT,
}


@dataclass(frozen=True)
class ContextRef:
    """One xbrli:context, with its period and dimensions."""

    context_id: str
    start: str | None
    end: str | None
    instant: str | None
    dimensions: tuple[tuple[str, str], ...] = ()

    @property
    def is_instant(self) -> bool:
        return self.instant is not None


@dataclass(frozen=True)
class Fact:
    """One fact, keyed by (namespace URI, local name, context id)."""

    namespace: str | None
    local_name: str
    context_ref: str | None
    unit_ref: str | None
    value: str
    decimals: str | None = None


@dataclass
class InstanceSummary:
    """What v1 reads out of an instance document."""

    taxonomy_family: TaxonomyFamily
    detected_prefixes: tuple[str, ...] = ()
    schema_ref: str | None = None
    contexts: dict[str, ContextRef] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    dropped_namespaces: tuple[str, ...] = ()

    def facts_for_context(self, context_id: str) -> list[Fact]:
        return [f for f in self.facts if f.context_ref == context_id]

    def fact_count_by_local_name(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for fact in self.facts:
            counts[fact.local_name] = counts.get(fact.local_name, 0) + 1
        return counts


class ReaderError(ValueError):
    """Raised when the document is not a readable XBRL instance."""


def read_instance(xml_bytes: bytes) -> InstanceSummary:
    """Summarize an XBRL 2.1 instance document (never an HTML rendering)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ReaderError(f"not well-formed XML: {exc}") from exc
    if root.tag != f"{_XBRLI}xbrl":
        raise ReaderError(
            f"root element is not xbrli:xbrl (got {root.tag!r}); "
            "refusing to treat a non-instance document as XBRL"
        )

    ns_map, dropped = _collect_namespaces(xml_bytes)
    family, prefixes = _detect_taxonomy(ns_map, root)
    contexts = _read_contexts(root)
    facts = _read_facts(root, dropped)

    return InstanceSummary(
        taxonomy_family=family,
        detected_prefixes=tuple(sorted(prefixes)),
        schema_ref=_read_schema_ref(root),
        contexts=contexts,
        facts=facts,
        dropped_namespaces=tuple(sorted(dropped)),
    )


# ---------------------------------------------------------------------- #
# Namespace handling
# ---------------------------------------------------------------------- #


def _collect_namespaces(xml_bytes: bytes) -> tuple[dict[str, str], set[str]]:
    """Map declared prefixes -> namespace URIs, plus a set of dropped items.

    Uses ``iterparse`` ``start-ns`` events so we see the *original* prefixes
    as written in the document (ElementTree rewrites them to ns0/ns1/... when
    serializing, which would lose the hyphenated ``in-bse-fin`` prefix we
    specifically need to detect).
    """
    ns_map: dict[str, str] = {}
    dropped: set[str] = set()
    try:
        for _event, ns_pair in ET.iterparse(
            io.BytesIO(xml_bytes), events=("start-ns",)
        ):
            prefix, uri = ns_pair
            prefix, uri = prefix.strip(), uri.strip()
            if not prefix or not uri:
                continue
            if not NCNAME_RE.match(prefix):
                dropped.add(f"prefix:{prefix}")
                continue
            ns_map[prefix] = uri
    except ET.ParseError:
        pass  # the strict parse above already reported malformed XML
    return ns_map, dropped


def _detect_taxonomy(
    ns_map: dict[str, str], root: ET.Element
) -> tuple[TaxonomyFamily, set[str]]:
    """Identify the taxonomy family from declared namespaces and fact URIs.

    Prefixes may be rebound arbitrarily, so match on namespace URI first and
    fall back to the known hyphenated prefixes.
    """
    matched: set[str] = set()
    for prefix, uri in ns_map.items():
        low = prefix.lower()
        if low in _KNOWN_PREFIXES:
            matched.add(prefix)
        if "sebi.gov.in/xbrl" in uri or "in-capmkt" in low:
            matched.add(prefix)
        if "bseindia.com/xbrl" in uri or "in-bse-fin" in low:
            matched.add(prefix)

    # Fact element namespaces are authoritative even when the declaration
    # scan misses (e.g. exotic serializations).
    fact_uris = set()
    for el in root:
        tag = el.tag
        if isinstance(tag, str) and tag.startswith("{"):
            uri, _, _ = tag[1:].partition("}")
            fact_uris.add(uri)

    all_uris = set(ns_map.values()) | fact_uris
    if any("sebi.gov.in/xbrl" in u for u in all_uris):
        return TaxonomyFamily.CAPMKT, matched
    if any("bseindia.com/xbrl" in u for u in all_uris):
        return TaxonomyFamily.BSE_FIN, matched
    if matched:
        for prefix in matched:
            fam = _KNOWN_PREFIXES.get(prefix.lower())
            if fam is not None:
                return fam, matched
        return TaxonomyFamily.UNKNOWN, matched
    return TaxonomyFamily.UNKNOWN, matched


def _read_schema_ref(root: ET.Element) -> str | None:
    """Read the schemaRef: ``link:schemaRef/@xlink:href`` is the XBRL 2.1
    convention (and what real filings carry, e.g. a *relative*
    ``in-capmkt-ent-2026-01-31.xsd``); fall back to ``xsi:schemaLocation``."""
    sr = root.find(f"{_LINK}schemaRef")
    if sr is not None:
        href = sr.attrib.get(_XLINK + "href") or sr.attrib.get("href")
        if href:
            return href.strip()
    loc = root.attrib.get(_XSI + "schemaLocation")
    if loc:
        # schemaLocation is "ns-1 uri-1 ns-2 uri-2 ..."; take the first URI.
        parts = loc.split()
        return parts[1] if len(parts) >= 2 else parts[0]
    return None


# ---------------------------------------------------------------------- #
# Contexts and facts
# ---------------------------------------------------------------------- #


def _read_contexts(root: ET.Element) -> dict[str, ContextRef]:
    contexts: dict[str, ContextRef] = {}
    for ctx in root.findall(f"{_XBRLI}context"):
        ctx_id = ctx.attrib.get("id")
        if not ctx_id:
            continue
        start = end = instant = None
        period = ctx.find(f"{_XBRLI}period")
        if period is not None:
            el = period.find(f"{_XBRLI}startDate")
            if el is not None and el.text:
                start = el.text.strip()
            el = period.find(f"{_XBRLI}endDate")
            if el is not None and el.text:
                end = el.text.strip()
            el = period.find(f"{_XBRLI}instant")
            if el is not None and el.text:
                instant = el.text.strip()
        dims: list[tuple[str, str]] = []
        entity = ctx.find(f"{_XBRLI}entity")
        if entity is not None:
            segment = entity.find(f"{_XBRLI}segment")
            if segment is not None:
                for member in segment:
                    dim = member.attrib.get("dimension")
                    if dim:
                        dims.append((dim, (member.text or "").strip()))
        contexts[ctx_id] = ContextRef(
            context_id=ctx_id, start=start, end=end, instant=instant,
            dimensions=tuple(dims),
        )
    return contexts


def _read_facts(root: ET.Element, dropped: set[str]) -> list[Fact]:
    """Read all top-level facts (children of xbrli:xbrl other than contexts,
    units, and linkbase elements)."""
    facts: list[Fact] = []
    for el in root:
        tag = el.tag
        if tag.startswith(_XBRLI) and tag[len(_XBRLI):] in {
            "context", "unit"
        }:
            continue
        if tag.startswith(_LINK):
            continue
        ns, local = _split_tag(tag)
        if ns is None:
            dropped.add("fact:no-namespace")
        facts.append(
            Fact(
                namespace=ns,
                local_name=local,
                context_ref=el.attrib.get("contextRef"),
                unit_ref=el.attrib.get("unitRef"),
                value=(el.text or "").strip(),
                decimals=el.attrib.get("decimals"),
            )
        )
    return facts


def _split_tag(tag: str) -> tuple[str | None, str]:
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        return uri, local
    return None, tag


def quarter_context_ids(summary: InstanceSummary) -> list[str]:
    """Context ids that look like the discrete quarter (``OneD`` convention).

    In real filings the quarter context id is ``OneD`` and the cumulative
    year-to-date context is ``FourD``; both appear in the same filing and mean
    different things. We match the convention; a later normalization layer
    should verify by period length so a renamed context still classifies.
    """
    return _duration_context_ids(summary, "ONED")


def ytd_context_ids(summary: InstanceSummary) -> list[str]:
    """Context ids that look like cumulative year-to-date (``FourD``)."""
    return _duration_context_ids(summary, "FOURD")


def _duration_context_ids(summary: InstanceSummary, marker: str) -> list[str]:
    out: list[str] = []
    for ctx in summary.contexts.values():
        if ctx.is_instant or ctx.start is None or ctx.end is None:
            continue
        if ctx.context_id.upper() == marker:
            out.append(ctx.context_id)
    return out
