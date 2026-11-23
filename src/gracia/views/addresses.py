from pyramid.events import ContextFound, subscriber
from pyramid.httpexceptions import HTTPGone, HTTPMovedPermanently, HTTPPermanentRedirect
from pyramid.location import lineage
from pyramid.view import exception_view_config

from gracia.resources import Record, RecordDeleted


@exception_view_config(RecordDeleted)
def gone(exc, request):
    return HTTPGone()


@subscriber(ContextFound)
def to_the_current_address(event) -> None:
    request = event.request
    path = list(reversed(list(lineage(request.context))))[1:]  # below the root
    traversed = request.traversed
    for node, said in zip(path, traversed, strict=False):
        if isinstance(node, Record):
            if said == node.__name__ or not request.has_permission("view", node):
                return  # (where a record is now is not told to who may not see it)
            break
    else:
        return
    rest = [*([request.view_name] if request.view_name else []), *request.subpath]
    target = request.resource_url(request.context, *rest)
    if not rest:
        target = target.rstrip("/")  # the address of the record has no slash
    if request.query_string:
        target += f"?{request.query_string}"
    redirect = (
        HTTPMovedPermanently
        if request.method in ("GET", "HEAD")
        else HTTPPermanentRedirect
    )
    raise redirect(location=target)
