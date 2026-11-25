import json

from pyramid.response import Response
from pyramid.view import view_config

from gracia import forms, search, uri
from gracia.resources import Repository, edit_address

SHOWN = 10  # what a typeahead shows


def visible(request, repository, hits: list[dict]) -> list[dict]:
    """The hits that the person may see: of a type they may view, and published, or of a
    type they may edit."""
    allowed: dict[str, tuple[bool, bool]] = {}  # per type: may view, may edit
    found = []
    for hit in hits:
        slug = hit.get("type", "")
        if slug not in allowed:
            try:
                node = repository[slug]
                allowed[slug] = (
                    request.has_permission("view", node),
                    request.has_permission("edit", node),
                )
            except KeyError:
                allowed[slug] = (False, False)
        may_view, may_edit = allowed[slug]
        if may_view and (hit.get("published", True) or may_edit):
            found.append(hit)
    return found


def type_icon(repository, slug: str) -> str:
    res_type = repository.types.get(slug)
    return (res_type.icon or "") if res_type is not None else ""


def kind_of(repository, hit: dict) -> str:
    """What the resource is, as it is shown: the name of its rdf type (the label the
    shape of the type gives it), or the name of the type of the record, if none."""
    res_type = repository.types.get(hit.get("type", ""))
    given = hit.get("resource_type")
    given = given[0] if isinstance(given, list) else given
    shapes = res_type.shacl_shape(include_record_shape=False) if res_type else None
    if given and shapes:
        return forms.describe(shapes[0], [given])[given]["label"]
    return res_type.name if res_type is not None else ""


def item(hit: dict, edit: str = "", icon: str = "", kind: str = "") -> dict:
    """What the widget of a reference gets of a hit: the uri to point at, the label to
    show for it, what it is and the record it is in, and where its form is (for the
    dialog), if the person may edit it."""
    record = hit["record"]
    return {
        "uri": hit.get("resource_uri") or uri.default_resource_uri(record),
        "label": hit.get("label") or record,
        "type": hit.get("type", ""),
        "kind": kind or hit.get("type", ""),
        "record": record,
        "published": hit.get("published", True),
        "edit": edit,
        "icon": icon,
    }


def number(request, name: str, default: int, highest: int) -> int:
    try:
        return min(max(int(request.params.get(name, default)), 1), highest)
    except ValueError:
        return default


# The typeahead of a reference (ReferenceWidget): the records whose label begins with
# the words typed (`q`; none: every record), of the resources of the rdf types in `type`
# (one parameter for each; none: any), as JSON, a page of `size` of them (`page`, from
# 1). Only what the person may see is told. Of a great many records only the first
# LOOKUP_CAP, by label, can be paged through (`truncated`): more words narrow it down.
@view_config(context=Repository, name="lookup", request_method="GET")
def lookup(request):
    repository = request.context
    indexes = request.root.indexes
    definitions = search.definitions_for(repository) if indexes is not None else []
    problem = ""
    items: list[dict] = []
    total, pages, page, truncated = 0, 1, 1, False
    size = number(request, "size", SHOWN, 50)
    if indexes is None:
        problem = "There is no search index."
    elif not definitions:
        problem = "Search is turned off in this repository."
    else:
        try:
            count, hits = indexes.lookup(
                repository.__name__,
                definitions[0],
                request.params.get("q", ""),
                request.params.getall("type"),
            )
            seen = visible(request, repository, hits)
            total = len(seen)
            truncated = count > len(hits)
            pages = max(1, -(-total // size))
            page = min(number(request, "page", 1, pages), pages)
            items = [
                item(
                    hit,
                    edit_address(request, repository, hit["record"], hit.get("slug")),
                    type_icon(repository, hit.get("type", "")),
                    kind_of(repository, hit),
                )
                for hit in seen[(page - 1) * size : page * size]
            ]
        except LookupError as problem_:
            problem = str(problem_)
    response = Response(
        json.dumps(
            {
                "items": items,
                "problem": problem,
                "page": page,
                "pages": pages,
                "size": size,
                "total": total,
                "truncated": truncated,
            }
        ),
        content_type="application/json",
        charset="utf-8",
        status=503 if problem else 200,
    )
    response.headers["Cache-Control"] = "private"  # what is found depends on who asks
    response.headers["Vary"] = "Authorization, Cookie"
    return response
