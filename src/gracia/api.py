from dataclasses import dataclass

OPERATIONS_KEY = "gracia.api_operations"


@dataclass(frozen=True)
class Operation:
    path: str  # relative to the repository, e.g. "/" or "/api"
    method: str
    summary: str
    permission: str  # what the view asks for (shown in the document)
    at: str | None = (
        None  # the node below the repository whose ACL decides, e.g. "jobs"
    )
    shown: str | None = (
        None  # the permission that lists it, when that is not `permission`
    )


def add_api_operation(
    config,
    path: str,
    summary: str,
    permission: str = "view",
    method: str = "get",
    at: str | None = None,
    shown: str | None = None,
):
    operation = Operation(path, method.lower(), summary, permission, at, shown)

    def register():
        config.registry.setdefault(OPERATIONS_KEY, []).append(operation)

    config.action(("gracia.api_operation", operation.method, operation.path), register)


def document(request) -> dict:
    repo_id = request.context.__name__
    paths: dict[str, dict] = {}
    operations = sorted(
        request.registry.get(OPERATIONS_KEY, []), key=lambda o: (o.path, o.method)
    )
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


def includeme(config) -> None:
    config.add_directive("add_api_operation", add_api_operation)
    config.add_api_operation("/", "The repository: its rows")
    config.add_api_operation("/{type}", "A type of record: the numbers of its records")
    config.add_api_operation("/{type}/{number}", "One record and its content")
    config.add_api_operation("/{type}/add", "Add a record of a type (HTML form)", "add")
    config.add_api_operation(
        "/{type}/{number}/edit", "Edit a record (HTML form)", "edit"
    )
    config.add_api_operation("/user/add", "Add a user (HTML form)", "super_edit")
    config.add_api_operation(
        "/user/{number}/edit", "Edit a user (HTML form)", "super_edit"
    )
    config.add_api_operation("/group/add", "Add a group (HTML form)", "manage")
    config.add_api_operation(
        "/group/{number}/edit", "Edit a group (HTML form)", "manage"
    )
    config.add_api_operation("/manage", "Edit the repository (HTML form)", "manage")
    config.add_api_operation(
        "/{type}/manage",
        "Manage a type: its schema file and validating it (HTML form)",
        "manage",
    )
    config.add_api_operation(
        "/api", "This document, for the caller (a record for a browser)"
    )
    config.add_api_operation("/jobs", "The jobs page (HTML)", "list_jobs", at="jobs")
    config.add_api_operation(
        "/jobs/list",
        "The jobs the caller may see, with their progress",
        "list_jobs",
        at="jobs",
    )
    config.add_api_operation(
        "/jobs/start",
        "Start a job: POST with ?job=<name> and the fields of its form "
        "(start_<name>_job)",
        "list_jobs",
        "post",
        at="jobs",
    )
    config.add_api_operation(
        "/jobs/{id}",
        "One job: state, task, progress",
        "view_job",
        at="jobs",
        shown="list_jobs",
    )
    config.add_api_operation(
        "/jobs/{id}/stop",
        "Stop a job (202; the stop goes on in a process of its own)",
        "stop_job",
        "post",
        at="jobs",
        shown="list_jobs",
    )
