import pyoxigraph as ox
from pyramid.httpexceptions import HTTPNotFound
from pyramid.renderers import render_to_response
from pyramid.view import view_config

from gracia import forms, hints, uri
from gracia.graph import dataset_rdfxml
from gracia.resources import Record, RecordDeleted, Type
from gracia.store import PAGE_SIZE, page_window
from gracia.views.index import index
from gracia.views.xslt import page

# never shown, whoever asks and whatever the record says
HIDDEN = frozenset({uri.CREDENTIALS})


def _plain(term):
    return term.value


def _reachable(request, res_type, number) -> bool:
    try:
        record = res_type[str(number)]
    except (KeyError, RecordDeleted):
        return False
    return bool(request.has_permission("view", record))


def wants_html(request) -> bool:
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept


def page_rdfxml(graph) -> str:
    """The RDF/XML that a page of the stylesheets is given of a graph, without what is
    never shown."""
    shown = graph.without(HIDDEN)
    if not len(shown):
        return ""
    return dataset_rdfxml(shown.dataset).decode()


def type_page(request):
    """The records of a type, in the order of `etags`, a page of them at a time, made by
    the stylesheets of the repository (`skin/type.xslt`): the numbers, the records of the
    page and their own triples, which have their labels and slugs."""
    context, store = request.context, request.store
    published = not request.has_permission("edit", context)
    total_published = store.count_referencing_records(record_type=context.number)
    total_all = (
        total_published
        if published
        else store.count_referencing_records(
            record_type=context.number, include_non_public=True
        )
    )
    asked = request.params.get("page")
    try:
        page_number, pages, offset, limit = page_window(
            total_published if published else total_all,
            int(asked) if asked is not None else None,
            PAGE_SIZE,
        )
    except ValueError:  # not a number, or a page that is not there
        raise HTTPNotFound from None
    ids = []
    if limit:
        ids = store.referencing_records(
            record_type=context.number,
            offset=offset,
            limit=limit,
            include_non_public=not published,
        )
    record_uris = []
    for record_id in ids:
        record_uris.append(uri.record_uri(context.slug, record_id % uri.TYPE_BASE))
    return page(
        request,
        "type",
        heading=context.name,
        total_published=total_published,
        total_all=total_all,
        records=record_uris,
        paging={
            "page": page_number,
            "pages": pages,
            "total": total_published if published else total_all,
            "size": PAGE_SIZE,
        },
        rdfxml=page_rdfxml(store.get_multi(ids)),
    )


def record_page(request):
    """A record, made by the stylesheets of the repository (`skin/record.xslt`): the
    properties of the shape that may be shown, each under the name the shape gives it, and
    the RDF/XML of the record and what it is made of."""
    record = request.context
    res_type = record.type
    shapes, shape = res_type.shacl_shape()
    names = hints.names(shapes, shape)
    properties = []
    for role in ("key_info", "description"):
        for path in dict.fromkeys(res_type.property_role_paths(role)):
            if path not in HIDDEN:
                properties.append((path, names.get(path, forms.local_name(path)), role))
    graph = request.store.get_multi([record.uri], compound_records=True)
    return page(
        request,
        "record",
        own=res_type.view_spec,
        heading=record.title,
        type_name=res_type.name,
        properties=properties,
        rdfxml=page_rdfxml(graph),
    )


@view_config(context=Type, request_method="GET")
def res_type(request):
    if wants_html(request):
        return type_page(request)
    context = request.context
    prefix = f"rec:{context.slug}:"
    numbers = sorted(
        int(key.removeprefix(prefix))
        for key in context.store.records_of_type(context.slug)
        if key.startswith(prefix)
    )
    if context.class_uri == uri.TYPE_CLASS:
        # the type records are public, except those of the private types: do not even
        # list them
        numbers = [n for n in numbers if _reachable(request, context, n)]
    return render_to_response(
        "json",
        {
            "type": context.name,
            "slug": context.slug,
            "class": context.class_uri,
            "records": numbers,
        },
        request,
    )


@view_config(context=Record, request_method="GET")
def record(request):
    if "application/json" in request.headers.get("Accept", ""):
        return render_to_response("json", record_values(request), request)
    if wants_html(request):
        return record_page(request)
    return index(request)


def record_values(request) -> dict:
    context = request.context
    subject, resource = ox.NamedNode(context.uri), context.resource

    def values(node):
        found: dict[str, list[str]] = {}
        for quad in context.graph.quads_for_subject(node):
            if quad.predicate.value in HIDDEN or isinstance(quad.object, ox.BlankNode):
                continue
            found.setdefault(quad.predicate.value, []).append(_plain(quad.object))
        return {predicate: sorted(items) for predicate, items in sorted(found.items())}

    shown = values(subject)
    shown.pop(uri.RESOURCE, None)
    return {
        "uri": context.uri,
        "type": context.__parent__.name,
        "record": shown,
        "resource": values(resource) if resource is not None else {},
        "resource_uri": [
            o.value for o in context.graph.objects(subject, uri.RESOURCE_URI)
        ],
    }
