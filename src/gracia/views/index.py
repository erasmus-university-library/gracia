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
    return hashlib.blake2b("\n".join(lines).encode("utf-8"), digest_size=8).hexdigest()


def etag_of(versions: dict[str, object], shapes: dict[str, str] | None = None) -> str:
    lines = [
        f"{rec_uri} {modified.isoformat()}"  # ty: ignore[missing-attribute]
        for rec_uri, modified in versions.items()
    ]
    lines += [f"shape:{slug} {version}" for slug, version in (shapes or {}).items()]
    return digest(sorted(lines))


def page_etag(stored: str, shapes: dict[str, str]) -> str:
    return digest(
        [
            f"etag:{stored}",
            *sorted(f"shape:{slug} {version}" for slug, version in shapes.items()),
        ]
    )


def shape_versions(record, slugs) -> dict[str, str]:
    types = record.repository.types
    return {
        slug: types[slug].assets_version for slug in sorted(set(slugs) & set(types))
    }


def shapes_of(record, slugs) -> list:
    types = record.repository.types
    quads = list(assets.record_shape(None, None)[0])
    for slug in sorted(set(slugs) & set(types)):
        content = types[slug].shacl_shape(include_record_shape=False)
        if content is not None:
            quads += list(content[0])
    return quads


def slugs_of(uris) -> set[str]:
    return {parsed[0] for u in uris if (parsed := uri.parse_record_uri(u))}


def index(request) -> Response:
    record, store = request.context, request.store
    record_id = store.record_id_of(record.uri)
    cached = store.etags([record_id])[record_id] if record_id is not None else None
    if cached is not None:  # one row: no walk through the references
        # the version of the schema of every type: a change of one is a change of all
        # the pages (the types of the page are known when it is made)
        etag = page_etag(cached, shape_versions(record, record.repository.types.keys()))
        slugs, uris = None, None
    else:
        versions = store.dependencies(record.uri, MAX_DEPTH)
        slugs, uris = slugs_of(versions), sorted(versions)
        etag = etag_of(versions, shape_versions(record, slugs))
    response = Response(content_type="application/rdf+xml", charset="utf-8")
    response.etag = etag
    response.cache_control = (
        "private, no-cache"  # always ask, the etag answers (see the module)
    )
    if etag in request.if_none_match:
        raise HTTPNotModified(headers={"ETag": response.headers["ETag"]})
    if uris is None:
        uris = store.page_uris(record.uri)
    if slugs is None:
        slugs = slugs_of(uris)
    found = store.get_multi(uris)
    shown = Graph(
        ox.Dataset(
            q
            for q in [*found, *shapes_of(record, slugs)]
            if q.predicate.value not in HIDDEN
        )
    )
    response.text = shown.serialize(
        "rdfxml",
        {**SHAPES, **record.repository.prefix_map()},
        xslt_params={"record_uri": record.uri},
    )
    return response
