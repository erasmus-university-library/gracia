"""Docs: the JSON object a record is known by to what follows the data.

A doc is made by XSLT from the RDF/XML of a record and of everything it is made of (what the graph cache
keeps). The base stylesheet is a file of the type `type` (`doc/doc-base-01.xsl`): it makes the fields every
doc has, and a type adds fields with a stylesheet of its own in its `doc/` directory, that imports the base,
named by the type's `gracia:docTemplate` (`doc-full-01.xsl`: a profile and a revision, like a schema file).
A type that names none has the docs the base makes.

The stylesheets of all the types are made one: `rollup` is a stylesheet that imports the base and the
`docTemplate` of every type of the repository. That works because a type's template matches the records of
that type only (the uri `rec:<slug>:`), so a batch of records of many types is one run of one stylesheet.
`compile` compiles it (`gracia.utils.xslt.Compiled`: imports are resolved when it is compiled, so the cache
keeps it for no longer than the repository stays as it was: see `Cache.compiled`), and `make` runs it.

The docs are kept in LMDB (`Cache.doc`, `gracia.cache.DocStore`) stamped with the etag of the graph they
were made from and the fingerprint of the stylesheets, so a doc made by other stylesheets is not used.
"""

import json
import logging
from xml.sax.saxutils import quoteattr

from gracia.utils import assets, xslt

log = logging.getLogger("gracia.docs")

XSL = "http://www.w3.org/1999/XSL/Transform"
BUNDLED_BASE = "gracia.types:type/doc/doc-base-01.xsl"


def hrefs(repository) -> list[str]:
    """The `type:` addresses of the doc stylesheets of a repository: the base, then the `docTemplate` of
    each type that has one, by slug. A type whose file is not there, or is not named right, is left out
    (and logged): its records get the doc of the base, and the other types go on."""
    found = [assets.DOC_BASE]
    for slug, res_type in sorted(repository.res_types.items()):
        if not res_type.doc_template:
            continue
        try:
            href = assets.doc_href(slug, res_type.doc_template)
            resolver(repository)(href)
        except (ValueError, LookupError, FileNotFoundError) as err:
            log.warning("%s: the doc template of %s is not used: %s", repository.__name__, slug, err)
            continue
        found.append(href)
    return found


def rollup(addresses: list[str]) -> str:
    """The stylesheet that imports the doc stylesheets at `addresses` (the base first, so what a type
    says is of higher precedence)."""
    imports = "".join(f'<xsl:import href="{href}"/>' for href in addresses)
    return f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSL}">{imports}</xsl:stylesheet>'


def resolver(repository):
    """`repository.resolve_type`, except that the base is the bundled one when the repository has no type
    `type` (a bare one, made by a script)."""

    def resolve(href: str):
        try:
            return repository.resolve_type(href)
        except LookupError:
            if href == assets.DOC_BASE:
                return assets.resolve(BUNDLED_BASE)
            raise

    return resolve


def compile(repository) -> xslt.Compiled:
    """The doc stylesheet of a repository, compiled. Needs the registry of the application to be the
    current one, for the files of the types."""
    return xslt.Compiled(rollup(hrefs(repository)), resolver(repository))


def make(compiled: xslt.Compiled, ids: dict[str, int], rdfxml: str) -> dict[str, dict]:
    """The docs of the records `ids` (uri -> record id) from RDF/XML that has them and what they are made
    of, by record uri. One run for all of them. A doc that could not be made is
    `{"record": uri, "error": why}`."""
    if not ids:
        return {}
    found = json.loads(compiled.transform(rdfxml, {"records": json.dumps(ids)}))
    return {doc["record"]: doc for doc in found}


def schema(compiled: xslt.Compiled, slugs: list[str]) -> list[dict]:
    """The fields the docs of the types `slugs` have (the schema mode of the doc stylesheets): one
    dict for each, in the form of `gracia.search.Field.as_dict()` and `types`, the slugs of the types
    that have it (and `conflict` when two of them say it differently)."""
    types = "".join(f"<type slug={quoteattr(slug)}/>" for slug in slugs)
    return json.loads(compiled.transform(f"<types>{types}</types>", {"schema": "true"}))
