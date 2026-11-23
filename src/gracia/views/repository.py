import json

from pyramid.httpexceptions import HTTPFound, HTTPNotFound
from pyramid.response import Response
from pyramid.view import view_config

from gracia import search, static, uri
from gracia.api import document as api_document
from gracia.resources import Repository, Resolved
from gracia.views import render


@view_config(context=Resolved, request_method=("GET", "HEAD"))
def resolve(request):
    record = request.context.record
    if not request.has_permission("view", record):
        raise HTTPNotFound()
    return HTTPFound(request.resource_url(record))


def prefers_html(request) -> bool:
    if not request.headers.get("Accept"):
        return False
    offers = request.accept.acceptable_offers(
        ["application/json", "text/html"]
    )  # a tie is JSON
    return bool(offers) and offers[0][0] == "text/html"


@view_config(context=Repository, request_method="GET")
def repository(request):
    if prefers_html(request):
        return search_page(request)
    return Response(
        json.dumps({"id": request.context.__name__, **request.store.counts()}),
        content_type="application/json",
        charset="utf-8",
    )


def search_page(request):
    repository, query = request.context, request.params.get("q", "").strip()
    message, hits, total = "", [], 0
    indexes = request.root.indexes
    definitions = search.definitions_for(repository) if indexes is not None else []
    if indexes is None:
        message = "There is no search index: set gracia.index_dir."
    elif not definitions:
        message = "Search is turned off in this repository."
    elif query:
        try:
            total, found = indexes.find(
                repository.__name__, definitions[0], query, limit=100
            )
            if indexes.status(repository.__name__, definitions[0]) == "stale":
                message = (
                    "This index was made for other fields than the settings of the "
                    "repository "
                    "have, so it may"
                    " lack what was changed since: the administrator makes it again "
                    "with Rebuild "
                    "search."
                )
        except LookupError:
            message = (
                "The search index is not there yet: "
                "the administrator makes it with Rebuild search."
            )
            found = []
        allowed: dict[str, tuple[bool, bool]] = {}  # per type: may view, may edit
        types: dict[
            str, object
        ] = {}  # the type nodes, which the addresses of its records are made from
        for hit in found:
            slug = hit.get("type", "")
            if slug not in allowed:
                try:
                    types[slug] = repository[slug]
                    allowed[slug] = (
                        request.has_permission("view", types[slug]),
                        request.has_permission("edit", types[slug]),
                    )
                except KeyError:
                    allowed[slug] = (False, False)
            may_view, may_edit = allowed[slug]
            if may_view and (hit.get("published", True) or may_edit):
                parsed = uri.parse_record_uri(hit["record"])
                hits.append(
                    {
                        "label": hit.get("label") or hit["record"],
                        "type": slug,
                        "record": hit["record"],
                        # what `request.resource_url(record)` gives: below its type, by
                        # its slug
                        # when it has
                        # one, else by its number (`Record.__name__`). The slug is a
                        # field of the
                        # index, so
                        # no record has to be read to make the address.
                        "url": request.resource_url(
                            types[slug], hit.get("slug") or str(parsed[1])
                        )
                        if parsed
                        else None,
                        "published": hit.get("published", True),
                    }
                )
        hits = hits[:20]
        if query and not hits:
            message = f"Nothing found. {message}".strip()
    response = render(
        request,
        "gracia:assets/templates/search.pt",
        heading=repository.__name__,
        subtitle="Search the records",
        action=request.resource_url(repository),
        query=query,
        hits=hits,
        message=message,
        shown=len(hits),
        total=total,
    )
    response.headers["Cache-Control"] = "private"  # what is found depends on who asks
    response.headers["Vary"] = "Authorization, Cookie, Accept"
    return response


@view_config(
    context=Repository,
    name="api",
    renderer="json",
    request_method="GET",
    accept="application/json",
)
def api(request):
    # what a caller may see depends on who they are: never share it through a public
    # cache
    request.response.headers["Cache-Control"] = "private"
    request.response.headers["Vary"] = "Authorization, Cookie, Accept"
    return api_document(request)


@view_config(context=Repository, name="api", request_method="GET", accept="text/html")
def api_browser(request):
    document = api_document(request)
    operations = [
        {"path": path, "method": method.upper(), **found}
        for path, methods in document["paths"].items()
        for method, found in methods.items()
    ]
    static.api_browser_js.need()
    response = render(
        request,
        "gracia:assets/templates/api.pt",
        heading=document["info"]["title"],
        subtitle="What you may do here. The same document is JSON for a program.",
        operations=operations,
        base=request.resource_url(request.context).rstrip(
            "/"
        ),  # the paths of operations follow it
        document_url=request.resource_url(request.context, "api"),
    )
    response.headers["Cache-Control"] = "private"
    response.headers["Vary"] = "Authorization, Cookie, Accept"
    return response
