import contextlib
from pathlib import Path

import deform
from pyramid.httpexceptions import HTTPForbidden, HTTPNotFound, HTTPServiceUnavailable
from pyramid.response import FileResponse, Response
from pyramid.view import view_config

from gracia import static
from gracia.interfaces import IJob
from gracia.jobs import ACTIVE, jobs_dir, procs
from gracia.jobs.db import JobsDb
from gracia.jobs.job import all_jobs
from gracia.jobs.spec import JobSpecError, validate_spec
from gracia.resources import JobRun, Jobs
from gracia.views import render, same_origin

LIST_LIMIT = (
    50  # the newest jobs of the list; the finished ones go with `gracia jobs clean`
)
LOG_CHUNK = 64 * 1024
LOGS = {"luigi": "luigi.log", "job": "job.log"}


def _directory(request):
    try:
        return jobs_dir(request.registry.settings)
    except ValueError as err:
        raise HTTPServiceUnavailable(str(err)) from None


@contextlib.contextmanager
def _db(request):
    with JobsDb(request.root) as db:
        yield db


def _moment(value):
    return value.isoformat() + "Z" if value else None  # naive UTC in the tables


def _label(request, job: dict) -> str:
    found = request.registry.queryUtility(IJob, job["job"] or "")
    title = found.title if found is not None else (job["job"] or "Job")
    return f"{title}: {job['subject']}" if job["subject"] else title


def _stages(job: dict) -> list[dict]:
    snapshot = (job.get("progress") or {}).get("stages")
    if snapshot:
        return snapshot
    return [
        {
            "name": entry["stage"],
            "state": "pending",
            "batches": {"done": 0, "running": 0, "total": None},
            "processed": 0,
            "passed": 0,
            "rejected": {},
            "samples": [],
            "total": None,
            "estimated": False,
        }
        for entry in job["stages"]
    ]


def _percent(job: dict, stages: list[dict]) -> float:
    if job["state"] == "done":
        return 100.0
    fractions = []
    for stage in stages:
        batches = stage["batches"]
        if stage["state"] == "done":
            fractions.append(1.0)
        elif batches["total"]:
            fractions.append(min(batches["done"] / batches["total"], 1.0))
        else:
            fractions.append(0.0)
    return round(sum(fractions) / len(fractions) * 100, 1) if fractions else 0.0


def summary(request, jobs_node: Jobs, job: dict) -> dict:
    stages = _stages(job)
    node = JobRun(jobs_node, job)
    url = request.resource_url(node)
    known = any(s["batches"]["total"] is not None for s in stages)
    rejected = sum(sum(s["rejected"].values()) for s in stages)
    return {
        "id": job["id"],
        "url": url,
        "label": _label(request, job),
        "job": job["job"],
        "subject": job["subject"],
        "state": job["state"],
        "active": job["state"] in ACTIVE,
        "message": job["message"],
        "error": job["error_msg"],
        "percent": _percent(job, stages),
        "determinate": job["state"] == "done" or known,
        "stages": stages,
        "rejected": rejected,
        "download_url": request.resource_url(node, "download")
        if job["download_file"]
        else None,
        "log_url": request.resource_url(node, "log"),
        "logs_url": request.resource_url(node, "logs"),
        "created_at": _moment(job["created_at"]),
        "started_at": _moment(job["started_at"]),
        "finished_at": _moment(job["finished_at"]),
        "can_stop": request.has_permission("stop_job", node),
    }


def _visible(
    request, jobs_node: Jobs, db: JobsDb, job=None, subject=None
) -> list[dict]:
    out = []
    for row in db.list_jobs(
        repo=jobs_node.repository.__name__, job=job, subject=subject
    ):
        if request.has_permission("view_job", JobRun(jobs_node, row)):
            out.append(summary(request, jobs_node, row))
            if len(out) >= LIST_LIMIT:
                break
    return out


def start_job(request, node: Jobs, job, params: dict) -> str:
    spec = validate_spec(job.spec(node.repository.__name__, params))
    directory = _directory(request)
    settings = request.registry.settings
    with _db(request) as db:
        who = request.authenticated_userid
        job_id = db.create(spec, created_by=f"user:{who}" if who else None)
        (directory / job_id).mkdir(parents=True, exist_ok=True)
        procs.start_job(
            db,
            job_id,
            directory,
            settings.get("gracia.config_file"),
            settings.get("gracia.db"),
        )
    return job_id


@view_config(context=Jobs, permission="list_jobs", request_method="GET")
def jobs_page(request):
    node = request.context
    repository = node.repository
    startable = []
    for job in all_jobs(request.registry):
        if request.has_permission(job.start_permission, node):
            form = job.form(node, request)
            static.need_form(form)
            startable.append(
                {
                    "job": job,
                    "form": form.render({}),
                    "fields": len(form.schema.children),
                }
            )
    static.jobs_js.need()
    return render(
        request,
        "gracia:assets/templates/jobs.pt",
        heading=f"Jobs of {repository.__name__}",
        subtitle="Start a job, and watch the jobs that run.",
        startable=startable,
        start_url=request.resource_url(node, "start"),
        list_url=request.resource_url(node, "list"),
        panel_filter="",
    )


@view_config(
    context=Jobs,
    name="list",
    permission="list_jobs",
    renderer="json",
    request_method="GET",
)
def jobs_list(request):
    directory = _directory(request)
    request.response.headers["Cache-Control"] = "no-store"
    with _db(request) as db:
        procs.reconcile(
            db, directory
        )  # a job whose process is gone is `failed`, not running
        found = _visible(
            request,
            request.context,
            db,
            job=request.params.get("job") or None,
            subject=request.params.get("subject") or None,
        )
        return {"jobs": found}


@view_config(
    context=Jobs,
    name="start",
    permission="list_jobs",
    renderer="json",
    request_method="POST",
)
def start(request):
    same_origin(request)
    node = request.context
    job = request.registry.queryUtility(IJob, request.params.get("job", ""))
    if job is None:
        raise HTTPNotFound("no such job")
    if not request.has_permission(job.start_permission, node):
        raise HTTPForbidden(f"starting {job.name} needs {job.start_permission}")
    _directory(request)  # a clear 503 before anything is made
    form = job.form(node, request)
    try:
        params = form.validate(list(request.POST.items()))
    except deform.ValidationFailure as failure:
        request.response.status = 422
        return {"form": failure.render()}
    try:
        return {"id": start_job(request, node, job, params)}
    except JobSpecError as err:
        request.response.status = 422
        return {"errors": err.errors}


@view_config(
    context=JobRun, permission="view_job", renderer="json", request_method="GET"
)
def job_status(request):
    node = request.context
    with _db(request) as db:
        procs.reconcile(db, _directory(request))
        row = db.get(node.__name__)
        return summary(request, node.repository, row)


@view_config(
    context=JobRun, name="download", permission="view_job", request_method="GET"
)
def download(request):
    node = request.context
    with _db(request) as db:
        row = db.get(node.__name__)
    if row is None or not row["download_file"]:
        raise HTTPNotFound("this job has nothing to download")
    path = _directory(request) / node.__name__ / Path(row["download_file"]).name
    if not path.is_file():
        raise HTTPNotFound("the file is gone")
    response = FileResponse(
        str(path),
        request=request,
        content_type=row["download_content_type"] or "text/plain",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return response


@view_config(context=JobRun, name="log", permission="view_job", request_method="GET")
def log(request):
    node = request.context
    name = LOGS.get(request.params.get("name", "luigi"))
    if name is None:
        raise HTTPNotFound("no such log")
    path = _directory(request) / node.__name__ / name
    size = path.stat().st_size if path.is_file() else 0
    try:
        offset = int(request.params["offset"])
    except (KeyError, ValueError):
        offset = max(0, size - LOG_CHUNK)
    offset = min(max(offset, 0), size)
    data = b""
    if size > offset:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(LOG_CHUNK)
    if data and not data.endswith(b"\n") and b"\n" in data:
        with _db(request) as db:
            row = db.get(node.__name__)
        if row is not None and row["state"] in ACTIVE:
            data = data[: data.rindex(b"\n") + 1]  # the unfinished line waits
    response = Response(body=data, content_type="text/plain", charset="utf-8")
    response.headers["X-Log-Offset"] = str(offset + len(data))
    response.headers["Cache-Control"] = "no-store"
    return response


@view_config(context=JobRun, name="logs", permission="view_job", request_method="GET")
def log_page(request):
    node = request.context
    with _db(request) as db:
        row = db.get(node.__name__)
    name = request.params.get("name", "luigi")
    static.job_log_js.need()
    embedded = (
        request.params.get("embed") == "1"
    )  # for the iframe in the job panel: just the log
    return render(
        request,
        "gracia:assets/templates/job_log_embed.pt"
        if embedded
        else "gracia:assets/templates/job_log.pt",
        heading=f"Log of {_label(request, row)}",
        subtitle=node.__name__,
        name=name if name in LOGS else "luigi",
        log_url=request.resource_url(node, "log"),
        status_url=request.resource_url(node),
    )


@view_config(
    context=JobRun,
    name="stop",
    permission="stop_job",
    renderer="json",
    request_method="POST",
)
def stop(request):
    same_origin(request)
    node = request.context
    with _db(request) as db:
        row = db.get(node.__name__)
    if row is None or row["state"] not in ACTIVE:
        return {"state": row["state"] if row else "gone"}
    settings = request.registry.settings
    who = request.authenticated_userid or "someone"
    procs.spawn_stop(
        node.__name__,
        settings.get("gracia.config_file"),
        settings.get("gracia.db"),
        by=who,
    )
    request.response.status = 202
    return {"state": "stopping"}
