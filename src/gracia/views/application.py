"""Views of the application and the error records."""

from pyramid.view import forbidden_view_config, notfound_view_config, view_config

from gracia.application import Application


@view_config(context=Application, renderer="json", request_method="GET")
def repositories(request):
    return {"repositories": request.root.list_repositories()}


@notfound_view_config(renderer="json")
def not_found(request):
    context = getattr(request, "context", None)
    if context is not None and not request.has_permission("view", context):
        # something below a node the caller may not see: do not say whether it exists, or the
        # difference between 404 and 401/403 lists the users and groups to the public
        return forbidden(request)
    request.response.status = 404
    return {"error": "not_found", "path": request.path}


@forbidden_view_config(renderer="json")
def forbidden(request):
    if request.authenticated_userid is None and getattr(request.registry, "challenge", False):
        # not logged in: ask the browser for a login instead of refusing for good
        request.response.status = 401
        request.response.headers["WWW-Authenticate"] = 'Basic realm="gracia", charset="UTF-8"'
        return {"error": "unauthorized", "path": request.path}
    request.response.status = 403
    return {"error": "forbidden", "path": request.path}
