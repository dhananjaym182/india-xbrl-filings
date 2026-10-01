"""Side-loaded SEBI taxonomy package support.

**This is the single most important design decision in the project.**

The ``schemaRef`` in every instance is *relative* (e.g.
``in-capmkt-ent-2026-01-31.xsd``) and **neither host serves the .xsd**:

- ``nsearchives.nseindia.com/corporate/xbrl/*.xsd`` -> 404
- ``sebi.gov.in/xbrl/...`` -> 530

So a strict DTS-resolving parser (plain Arelle) **cannot** resolve the
taxonomy from the instance alone. Two options exist:

1. **Side-load the official SEBI taxonomy package** (primary path, implemented
   here and vendored in ``vendor/taxonomies/``). The SEBI "Integrated Filing
   Finance (IndAS)" package is a valid XBRL Taxonomy Package with
   ``META-INF/taxonomyPackage.xml``, ``META-INF/catalog.xml``, entry point
   ``in-capmkt-ent-2025-01-31.xsd``, publisher SEBI, plus lab/cal/def/pre/ref
   linkbases and a tag->label XLSX. Siblings exist for NBFC, Banking, General
   Insurance, Life Insurance, and "Other than banks".
2. **Parse namespace-agnostically** (fallback, implemented in
   :mod:`india_xbrl.reader`): strip prefixes, match local names. Simpler and
   robust to taxonomy drift, but loses DTS-validated semantics.

Packages are versioned by their taxonomy publication date so a future SEBI
revision can be added beside this one without conflict.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

_TP_NS = "{http://xbrl.org/2016/taxonomy-package}"
_CATALOG_NS = "{urn:oasis:names:tc:entity:xmlns:xml:catalog}"


@dataclass(frozen=True)
class TaxonomyPackage:
    """A validated XBRL taxonomy package on disk."""

    name: str
    version: str
    publisher: str
    publication_date: str
    identifier: str
    entry_points: tuple[str, ...]
    path: Path
    rewrite_prefix: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def resolve_schema(self, schema_ref: str) -> Path | None:
        """Resolve an instance's relative schemaRef against this package.

        Uses the package catalog's rewriteURI rule (catalog.xml maps the
        official namespace URI prefix onto files inside the package).
        """
        ref = schema_ref.strip()
        if self.rewrite_prefix and ref.startswith(self.rewrite_prefix):
            rel = ref[len(self.rewrite_prefix):].lstrip("/")
            candidate = self.path / rel
            if candidate.is_file():
                return candidate
        # Relative refs (the common broken-upstream case).
        if not ref.startswith(("http://", "https://")):
            resolved = self._resolve_relative(ref)
            if resolved is not None:
                return resolved
            # Real instances often reference a *newer* schema date than the
            # vendored package (e.g. in-capmkt-ent-2026-01-31.xsd against the
            # 2025-01-31 package). Substitute the ref's date-stamp with the
            # package entry point's and retry -- same taxonomy, older
            # publication.
            return self._resolve_date_substituted(ref)
        return None

    def _entry_dirs(self) -> list[str]:
        """Local directories (relative to the package root) holding the
        entry points, derived from the entry-point hrefs and the catalog
        rewrite rule."""
        dirs: list[str] = []
        for href in self.entry_points:
            local = href
            if self.rewrite_prefix and local.startswith(self.rewrite_prefix):
                local = local[len(self.rewrite_prefix):]
            else:
                local = re.sub(r"^[a-z][a-z0-9+.-]*://", "", local)
                if "/" in local:
                    local = local.split("/", 1)[1]  # drop the host
            parent = local.rsplit("/", 1)[0] if "/" in local else ""
            if parent and parent not in dirs:
                dirs.append(parent)
        return dirs

    def _resolve_relative(self, ref: str) -> Path | None:
        candidate = self.path / ref
        if candidate.is_file():
            return candidate
        for entry_dir in self._entry_dirs():
            candidate = self.path / entry_dir / ref
            if candidate.is_file():
                return candidate
        return None

    def _resolve_date_substituted(self, ref: str) -> Path | None:
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})", ref)
        target = self.publication_date or None
        if not dates or not target:
            return None
        substituted = ref.replace(dates[0], target)
        return self._resolve_relative(substituted)


class TaxonomyRegistry:
    """Discovers and loads taxonomy packages from one or more roots."""

    def __init__(self, roots: list[Path] | None = None) -> None:
        if roots is None:
            roots = default_roots()
        self._roots = roots
        self._cache: dict[str, TaxonomyPackage] = {}

    def packages(self) -> list[TaxonomyPackage]:
        out: list[TaxonomyPackage] = []
        for root in self._roots:
            if not root.is_dir():
                continue
            for tp_path in sorted(root.glob("**/META-INF/taxonomyPackage.xml")):
                pkg_path = tp_path.parent.parent
                try:
                    out.append(self._load(pkg_path))
                except (OSError, ET.ParseError) as exc:
                    # A broken package is recorded, never silently skipped.
                    out.append(
                        TaxonomyPackage(
                            name=f"<unparsable: {exc}>",
                            version="",
                            publisher="",
                            publication_date="",
                            identifier="",
                            entry_points=(),
                            path=pkg_path,
                            warnings=(f"taxonomyPackage.xml parse error: {exc}",),
                        )
                    )
        return out

    def _load(self, pkg_path: Path) -> TaxonomyPackage:
        tp_xml = pkg_path / "META-INF" / "taxonomyPackage.xml"
        root = ET.parse(tp_xml).getroot()
        name = _text(root, f"{_TP_NS}name")
        version = _text(root, f"{_TP_NS}version")
        publisher = _text(root, f"{_TP_NS}publisher")
        pub_date = _text(root, f"{_TP_NS}publicationDate")
        identifier = _text(root, f"{_TP_NS}identifier")

        entry_points: list[str] = []
        for ep in root.findall(f"{_TP_NS}entryPoints/{_TP_NS}entryPoint"):
            doc = ep.find(f"{_TP_NS}entryPointDocument")
            if doc is not None:
                href = doc.attrib.get("href", "").strip()
                if href:
                    entry_points.append(href)

        rewrite: str | None = None
        catalog = pkg_path / "META-INF" / "catalog.xml"
        if catalog.is_file():
            croot = ET.parse(catalog).getroot()
            for rule in croot.findall(f"{_CATALOG_NS}rewriteURI"):
                start = rule.attrib.get("uriStartString", "")
                if start:
                    rewrite = start

        return TaxonomyPackage(
            name=name,
            version=version,
            publisher=publisher,
            publication_date=pub_date,
            identifier=identifier,
            entry_points=tuple(entry_points),
            path=pkg_path,
            rewrite_prefix=rewrite,
        )

    def resolve(self, schema_ref: str) -> TaxonomyPackage | None:
        """Find the package that can resolve this schemaRef, cached."""
        cached = self._cache.get(schema_ref)
        if cached is not None:
            return cached if cached.entry_points else None
        best: TaxonomyPackage | None = None
        for pkg in self.packages():
            if pkg.warnings:
                continue
            if pkg.resolve_schema(schema_ref) is not None:
                best = pkg
                break
        self._cache[schema_ref] = best  # type: ignore[assignment]
        return best


def default_roots() -> list[Path]:
    """Where taxonomy packages are looked up, in order.

    1. ``$INDIA_XBRL_TAXONOMY_DIR`` (user override)
    2. The vendored directory inside a source checkout / wheel.
    """
    import os

    roots: list[Path] = []
    env = os.environ.get("INDIA_XBRL_TAXONOMY_DIR")
    if env:
        roots.append(Path(env))
    roots.extend(_vendored_roots())
    return roots


def _vendored_roots() -> list[Path]:
    roots: list[Path] = []
    # Source checkout: <repo>/vendor/taxonomies
    repo_candidate = Path(__file__).resolve().parents[2] / "vendor" / "taxonomies"
    if repo_candidate.is_dir():
        roots.append(repo_candidate)
        return roots
    # Installed wheel: vendored data shipped under india_xbrl/_vendor
    try:
        traversable = resources.files("india_xbrl") / "_vendor"
        with resources.as_file(traversable) as p:
            if p.is_dir():
                roots.append(p)
    except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError):
        pass
    return roots


def validate_packages() -> list[str]:
    """Human-readable validation report for ``india-xbrl --audit``."""
    lines: list[str] = []
    registry = TaxonomyRegistry()
    packages = registry.packages()
    if not packages:
        lines.append(
            "taxonomy: NO packages found. Set INDIA_XBRL_TAXONOMY_DIR or vendor "
            "a SEBI package under vendor/taxonomies/ (see README)."
        )
        return lines
    for pkg in packages:
        if pkg.warnings:
            lines.append(f"taxonomy: {pkg.name} ({pkg.path})")
            for warn in pkg.warnings:
                lines.append(f"  WARNING: {warn}")
            continue
        lines.append(
            f"taxonomy: {pkg.name} v{pkg.version} by {pkg.publisher} "
            f"(published {pkg.publication_date})"
        )
        lines.append(f"  identifier: {pkg.identifier}")
        for ep in pkg.entry_points:
            lines.append(f"  entry point: {ep}")
        xsd_count = sum(1 for _ in pkg.path.rglob("*.xsd"))
        linkbase_count = sum(
            1 for pat in ("*.xml",) for _ in pkg.path.rglob(pat)
        )
        lines.append(
            f"  files: {xsd_count} xsd, {linkbase_count} linkbase xml under "
            f"{pkg.path.name}/"
        )
    return lines


def _text(root: ET.Element, tag: str) -> str:
    el = root.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    return ""
