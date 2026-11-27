"""The views of the jobs of a repository: the list and the start of them (`JobsView`, of
the node `jobs`) and one run of a job (`JobRunView`, below it)."""

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
from gracia.jobs.nodes import JobRun, Jobs
from gracia.jobs.spec import JobSpecError, validate_spec
from gracia.forms import fragment
from gracia.security import same_origin
from gracia.templates import XsltRenderer

LIST_LIMIT = (
    50  # the newest jobs of the list; the finished ones go with `gracia jobs clean`
)
LOG_CHUNK = 64 * 1024
LOGS = {"luigi": "luigi.log", "job": "job.log"}


class JobsBase:
    """What the views of the jobs share: where the jobs are, the table of them, and what
    is said of a job."""

    def __init__(self, request):
        self.request = request
        self.context = request.context


    def directory(self):
        try:
            return jobs_dir(self.request.registry.settings)
        except ValueError as err:
            raise HTTPServiceUnavailable(str(err)) from None

    @contextlib.contextmanager
    def db(self):
        with JobsDb(self.request.root) as db:
            yield db

    @staticmethod
    def moment(value):
        return value.isoformat() + "Z" if value else None  # naive UTC in the tables

    def label(self, job: dict) -> str:
        found = self.request.registry.queryUtility(IJob, job["job"] or "")
        title = found.title if found is not None else (job["job"] or "Job")
        return f"{title}: {job['subject']}" if job["subject"] else title

    @staticmethod
    def stages(job: dict) -> list[dict]:
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

    @staticmethod
    def percent(job: dict, stages: list[dict]) -> float:
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

    def summary(self, jobs_node: Jobs, job: dict) -> dict:
        request = self.request
        stages = self.stages(job)
        node = jobs_node.run(job)
        url = request.resource_url(node)
        known = any(s["batches"]["total"] is not None for s in stages)
        rejected = sum(sum(s["rejected"].values()) for s in stages)
        return {
            "id": job["id"],
            "url": url,
            "label": self.label(job),
            "job": job["job"],
            "subject": job["subject"],
            "state": job["state"],
            "active": job["state"] in ACTIVE,
            "message": job["message"],
            "error": job["error_msg"],
            "percent": self.percent(job, stages),
            "determinate": job["state"] == "done" or known,
            "stages": stages,
            "rejected": rejected,
            "download_url": request.resource_url(node, "download")
            if job["download_file"]
            else None,
            "log_url": request.resource_url(node, "log"),
            "logs_url": request.resource_url(node, "logs"),
            "created_at": self.moment(job["created_at"]),
            "started_at": self.moment(job["started_at"]),
            "finished_at": self.moment(job["finished_at"]),
            "can_stop": request.has_permission("stop_job", node),
        }


class JobsView(JobsBase):
    @view_config(context=Jobs, permission="list_jobs", request_method="GET")
    def index(self):
        request, node = self.request, self.context
        repository = node.repository
        startable = []
        fragments = {}
        for job in all_jobs(request.registry):
            if request.has_permission(job.start_permission, node):
                form = job.form(node, request)
                static.need_form(form)
                startable.append(
                    {
                        "name": job.name,
                        "title": job.title,
                        "description": job.description,
                        "form_id": job.form_id,
                        "fields": len(form.schema.children),
                    }
                )
                fragments[f"form-{job.name}"] = form.render({})
        static.jobs_js.need()
        fragments["panel"] = fragment(
            request,
            "gracia:assets/templates/job_panel.pt",
            panel_list_url=request.resource_url(node, "list"),
            panel_filter="",
            panel_watch="",
            panel_compact="",
        )
        return XsltRenderer.render_page(
            request,
            "gracia:assets/xslt/skin/jobs.xslt",
            heading=f"Jobs of {repository.__name__}",
            subtitle="Start a job, and watch the jobs that run.",
            data={"startable": startable},
            fragments=fragments,
            start_url=request.resource_url(node, "start"),
        )

    def visible(self, db: JobsDb, job=None, subject=None) -> list[dict]:
        request, node = self.request, self.context
        out = []
        for row in db.list_jobs(repo=node.repository.__name__, job=job, subject=subject):
            if request.has_permission("view_job", node.run(row)):
                out.append(self.summary(node, row))
                if len(out) >= LIST_LIMIT:
                    break
        return out

    @view_config(
        context=Jobs,
        name="list",
        permission="list_jobs",
        renderer="json",
        request_method="GET",
    )
    def listing(self):
        directory = self.directory()
        self.request.response.headers["Cache-Control"] = "no-store"
        with self.db() as db:
            procs.reconcile(
                db, directory
            )  # a job whose process is gone is `failed`, not running
            found = self.visible(
                db,
                job=self.request.params.get("job") or None,
                subject=self.request.params.get("subject") or None,
            )
            return {"jobs": found}

    def start_job(self, job, params: dict) -> str:
        spec = validate_spec(job.spec(self.context.repository.__name__, params))
        directory = self.directory()
        settings = self.request.registry.settings
        with self.db() as db:
            who = self.request.authenticated_userid
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

    @view_config(
        context=Jobs,
        name="start",
        permission="list_jobs",
        renderer="json",
        request_method="POST",
    )
    def start(self):
        request, node = self.request, self.context
        same_origin(request)
        job = request.registry.queryUtility(IJob, request.params.get("job", ""))
        if job is None:
            raise HTTPNotFound("no such job")
        if not request.has_permission(job.start_permission, node):
            raise HTTPForbidden(f"starting {job.name} needs {job.start_permission}")
        self.directory()  # a clear 503 before anything is made
        form = job.form(node, request)
        try:
            params = form.validate(list(request.POST.items()))
        except deform.ValidationFailure as failure:
            request.response.status = 422
            return {"form": failure.render()}
        try:
            return {"id": self.start_job(job, params)}
        except JobSpecError as err:
            request.response.status = 422
            return {"errors": err.errors}


class JobRunView(JobsBase):
    @view_config(
        context=JobRun, permission="view_job", renderer="json", request_method="GET"
    )
    def status(self):
        node = self.context
        with self.db() as db:
            procs.reconcile(db, self.directory())
            row = db.get(node.__name__)
            return self.summary(node.__parent__, row)

    @view_config(
        context=JobRun, name="download", permission="view_job", request_method="GET"
    )
    def download(self):
        node = self.context
        with self.db() as db:
            row = db.get(node.__name__)
        if row is None or not row["download_file"]:
            raise HTTPNotFound("this job has nothing to download")
        path = self.directory() / node.__name__ / Path(row["download_file"]).name
        if not path.is_file():
            raise HTTPNotFound("the file is gone")
        response = FileResponse(
            str(path),
            request=self.request,
            content_type=row["download_content_type"] or "text/plain",
        )
        response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        return response

    @view_config(
        context=JobRun, name="log", permission="view_job", request_method="GET"
    )
    def log(self):
        node = self.context
        name = LOGS.get(self.request.params.get("name", "luigi"))
        if name is None:
            raise HTTPNotFound("no such log")
        path = self.directory() / node.__name__ / name
        size = path.stat().st_size if path.is_file() else 0
        try:
            offset = int(self.request.params["offset"])
        except (KeyError, ValueError):
            offset = max(0, size - LOG_CHUNK)
        offset = min(max(offset, 0), size)
        data = b""
        if size > offset:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(LOG_CHUNK)
        if data and not data.endswith(b"\n") and b"\n" in data:
            with self.db() as db:
                row = db.get(node.__name__)
            if row is not None and row["state"] in ACTIVE:
                data = data[: data.rindex(b"\n") + 1]  # the unfinished line waits
        response = Response(body=data, content_type="text/plain", charset="utf-8")
        response.headers["X-Log-Offset"] = str(offset + len(data))
        response.headers["Cache-Control"] = "no-store"
        return response

    @view_config(
        context=JobRun, name="logs", permission="view_job", request_method="GET"
    )
    def logs(self):
        request, node = self.request, self.context
        with self.db() as db:
            row = db.get(node.__name__)
        name = request.params.get("name", "luigi")
        static.job_log_js.need()
        embedded = (
            request.params.get("embed") == "1"
        )  # for the iframe in the job panel: just the log
        return XsltRenderer.render_page(
            request,
            "gracia:assets/xslt/skin/job_log_embed.xslt"
            if embedded
            else "gracia:assets/xslt/skin/job_log.xslt",
            bare=embedded,
            heading=f"Log of {self.label(row)}",
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
    def stop(self):
        request, node = self.request, self.context
        same_origin(request)
        with self.db() as db:
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
