"""The index view of a record: the record and the records its page is made of, as RDF/XML.

`GET /<repo>/<type>/<n>` (the view of the record itself; `Accept: application/json` still asks for
the plain values, see `views.type`):

1. the record, and every record that its resource points at, and those, and so on
   (`Store.dependencies`, from the `records` table and its references);
2. the types of those records are known from their uris (`rec:<type>:<n>`), and the shapes of
   those types go into the page: the shape that every record has, and the shape of the content of
   each type (`sh:name`, `sh:order`, `dash:propertyRole`, ...: what a stylesheet needs to title
   and order a property, or to find the label, in the language it is asked for). A page has the
   shapes of the types it is made of, and not those of the others (the shape of a person is in the
   page of a book that points at persons);
3. the etag of the page is made of the etag stored for the record (`etags`, see `Store.refresh_etags`:
   the hash of the ids and modification dates of the record and everything it is made of, made
   when something is written) and of the versions of those shapes: it changes when the record,
   anything it is made of or the schema of a type on the page changes, and not when anything else
   does. A request reads that one row; it does not walk the references. A repository that has no
   etags yet (none made, or one from before them) walks them as before (`Store.dependencies`);
4. what the browser has is answered with 304 before anything is built;
5. otherwise the sources of those records and the shapes are one graph, written as RDF/XML and
   nested by `rdfxml-to-nested.xsl` (blank nodes and collections inline), which is given the uri
   of the record as the parameter `record_uri`: the element of the record comes first.

A rendered file looked up by the etag is for later. The credentials of a user are never in the graph.
"""

import hashlib

import pyoxigraph as ox
from pyramid.httpexceptions import HTTPNotModified
from pyramid.response import Response

from gracia import uri
from gracia.graph import Graph
from gracia.utils import assets

# never shown, whoever asks and whatever the record says (as in `views.type`)
HIDDEN = frozenset({uri.CREDENTIALS})
MAX_DEPTH = 10  # how far out from the record the records of a page are looked for


SHAPES = {"sh": "http://www.w3.org/ns/shacl#", "dash": "http://datashapes.org/dash#"}


def digest(lines: list[str]) -> str:
    """16 hex digits of the lines, in the order given (not a secret: a version for caches)."""
    return hashlib.blake2b("\n".join(lines).encode("utf-8"), digest_size=8).hexdigest()


def etag_of(versions: dict[str, object], shapes: dict[str, str] | None = None) -> str:
    """The etag of a page made by walking its records: the records and when they were changed, and
    the versions of the shapes of their types, a line each, sorted. (A page whose record has a stored
    etag takes that: `page_etag`.)"""
    lines = [f"{rec_uri} {modified.isoformat()}" for rec_uri, modified in versions.items()]
    lines += [f"shape:{slug} {version}" for slug, version in (shapes or {}).items()]
    return digest(sorted(lines))


def page_etag(stored: str, shapes: dict[str, str]) -> str:
    """The etag of a page: the etag stored for its record, and the versions of the shapes of its
    types, which are files and not in the database."""
    return digest([f"etag:{stored}", *sorted(f"shape:{slug} {version}" for slug, version in shapes.items())])


def shape_versions(record, slugs) -> dict[str, str]:
    """The version of the files of each of these types (by slug) that the repository has."""
    res_types = record.repository.res_types
    return {slug: res_types[slug].assets_version for slug in sorted(set(slugs) & set(res_types))}


def shapes_of(record, slugs) -> list:
    """The quads of the shapes for a page made of records of these types (slugs): the shape that
    every record has, and the shape of the content of each type that is there. A type that has no
    schema has no shape."""
    res_types = record.repository.res_types
    quads = list(assets.record_shape(None, None)[0])
    for slug in sorted(set(slugs) & set(res_types)):
        content = res_types[slug].shape()
        if content is not None:
            quads += list(content[0])
    return quads


def slugs_of(uris) -> set[str]:
    """The types of records, from their uris (`rec:<type>:<n>`)."""
    return {parsed[0] for u in uris if (parsed := uri.parse_record_uri(u))}


def index(request) -> Response:
    record, store = request.context, request.store
    record_id = store.record_id_of(record.uri)
    cached = request.root.cache.etags(store.repo, [record_id])[record_id] if record_id is not None else None
    stored = (cached[0], list(cached[1])) if cached else None
    if stored is not None:  # one row: no walk through the references
        etag = page_etag(stored[0], shape_versions(record, stored[1]))
        slugs, uris = stored[1], None
    else:
        versions = store.dependencies(record.uri, MAX_DEPTH)
        slugs, uris = slugs_of(versions), sorted(versions)
        etag = etag_of(versions, shape_versions(record, slugs))
    response = Response(content_type="application/rdf+xml", charset="utf-8")
    response.etag = etag
    response.cache_control = "private, no-cache"  # always ask, the etag answers (see the module)
    if etag in request.if_none_match:
        raise HTTPNotModified(headers={"ETag": response.headers["ETag"]})
    found = store.get(uris if uris is not None else store.page_uris(record.uri))
    shown = Graph(
        ox.Dataset(q for q in [*found, *shapes_of(record, slugs)] if q.predicate.value not in HIDDEN)
    )
    response.text = shown.serialize(
        "rdfxml",
        {**SHAPES, **record.repository.prefix_map()},
        xslt_params={"record_uri": record.uri},
    )
    return response
