"""The OpenAPI document of a repository, built per request for the caller.

Operations are registered with the `add_api_operation` directive (gracia and any extension
package use it). The document lists only the operations the caller holds the permission for,
checked against the repository with the same ACLs that enforce access, so the documentation
and the enforcement come from one place. Hiding a path is never the protection: the view's
permission is.
"""

from dataclasses import dataclass

OPERATIONS_KEY = "gracia.api_operations"


@dataclass(frozen=True)
class Operation:
    path: str  # relative to the repository, e.g. "/" or "/api"
    method: str
    summary: str
    permission: str  # what the view asks for (shown in the document)
    at: str | None = None  # the node below the repository whose ACL decides, e.g. "jobs"
    shown: str | None = None  # the permission that lists it, when that is not `permission`


def add_api_operation(
    config,
    path: str,
    summary: str,
    permission: str = "view",
    method: str = "get",
    at: str | None = None,
    shown: str | None = None,
):
    """Config directive: describe an operation of a repository in its OpenAPI document.
    `permission` should be the permission of the view that serves it. An operation of a node
    below the repository (`at="jobs"`) is listed to those who hold `shown` (default: the
    permission) on that node: a permission that is about one object (`view_job`) is not held on
    the node itself."""
    operation = Operation(path, method.lower(), summary, permission, at, shown)

    def register():
        config.registry.setdefault(OPERATIONS_KEY, []).append(operation)

    config.action(("gracia.api_operation", operation.method, operation.path), register)


def document(request) -> dict:
    """The OpenAPI 3 document of `request.context` (a repository), for this caller."""
    repo_id = request.context.__name__
    paths: dict[str, dict] = {}
    operations = sorted(request.registry.get(OPERATIONS_KEY, []), key=lambda o: (o.path, o.method))
    for operation in operations:
        node = request.context
        if operation.at:
            try:
                node = node[operation.at]
            except KeyError:
                continue
        if request.has_permission(operation.shown or operation.permission, node):
            paths.setdefault(operation.path, {})[operation.method] = {
                "summary": operation.summary,
                "x-gracia-permission": operation.permission,
                "responses": {"200": {"description": "ok"}},
            }
    return {
        "openapi": "3.0.3",
        "info": {"title": f"gracia repository {repo_id}", "version": "0"},
        "servers": [{"url": request.resource_url(request.context).rstrip("/")}],
        "paths": paths,
    }
