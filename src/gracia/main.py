import importlib
import os
import secrets

from pyramid.config import Configurator
from pyramid.session import SignedCookieSessionFactory
from pyramid.events import ApplicationCreated, NewResponse

from gracia import (
    apidoc,
    extensions,
    predicates,
    resources,
    static,
    types,
)
from gracia import config as settings_file
from gracia.application import Application
from gracia.jobs import job as jobs_job
from gracia.jobs.stage import load_modules
from gracia.security import UsersPolicy
from gracia.views import deform as views_deform
from gracia.actions import BATCH, BATCH_BYTES, POLL, Worker, lock_dir_for


def includeme(config: Configurator) -> None:
    settings = config.registry.settings
    application = settings.get("gracia.application")
    owned = application is None
    if owned:
        target = settings.get("gracia.db") or os.environ.get("GRACIA_DB", "gracia.db")
        application = Application(
            target,
            token=settings_file.read_token(settings) if settings_file.is_remote(target) else None,
            watch=settings.get("gracia.db.file") or None,
            cache_dir=settings.get("gracia.cache_dir") or None,
        )
    config.registry["application"] = application
    config.registry["application_owned"] = owned
    
    def store(request):
        repository = getattr(request.context, "repository", None)
        if repository is None:
            raise LookupError("this request did not reach a repository")
        return repository.store()
    config.add_request_method(store, name="store", reify=True)
    
    if settings.get("gracia.index_dir"):  # the search index follows what the worker hands over
        from gracia import search
        from gracia.actions import RecordsChanged

        application.indexes = search.Indexes(settings["gracia.index_dir"])
        config.add_subscriber(search.on_records_changed, RecordsChanged)
        
    if str(settings.get("gracia.worker", "")).lower() in ("1", "true", "yes", "on"):
        # spawn the worker thread, that watches the actions queue 
        worker = Worker(
            application,
            config.registry,
            lock_dir=lock_dir_for(settings),
            batch=int(settings.get("gracia.worker.batch") or BATCH),
            batch_bytes=int(settings.get("gracia.worker.batch_bytes") or BATCH_BYTES),
            poll=float(settings.get("gracia.worker.poll") or POLL),
        )
        application.cache.worker = worker
        config.add_subscriber(lambda event: worker.start(), ApplicationCreated)
        config.add_subscriber(
            lambda event: event.request.method in (
                "GET", "HEAD", "OPTIONS") or worker.wake(), NewResponse
        )

    config.set_root_factory(lambda request: request.registry["application"])
    policy = settings.get("gracia.security_policy")
    if policy is None:
        policy = UsersPolicy()
    config.registry.challenge = getattr(policy, "challenge", False)
    config.set_security_policy(policy)
    config.set_default_permission("view")  # default deny: a view without a permission needs "view"

    secret = settings.get("gracia.session_secret")
    if not secret:  # without a setting every process has its own: a flash does not outlive it
        secret = secrets.token_hex(32)
    config.set_session_factory(
        SignedCookieSessionFactory(
            secret,
            cookie_name="gracia_session",
            httponly=True,
            samesite="Lax",
            secure=settings.get("gracia.session_secure", "false").lower() in ("1", "true", "yes"),
            timeout=None,
            reissue_time=None,  # looking at the session must not set the cookie again
        )
    )
    config.add_directive("add_api_operation", apidoc.add_api_operation)
    config.add_directive("add_record_class", types.add_record_class)
    config.add_directive("add_gracia_type", types.add_gracia_type)
    config.add_directive("add_gracia_library", static.add_library)
    config.add_directive("add_gracia_deform_view", views_deform.add_gracia_deform_view)
    config.add_directive("add_gracia_predicate", predicates.add_gracia_predicate)
    config.add_directive("add_gracia_extension", extensions.add_gracia_extension)
    config.add_directive("add_gracia_repository_child", extensions.add_gracia_repository_child)
    config.add_view_predicate("enabled_extension", extensions.ExtensionPredicate)
    # what is below a repository besides its types: found in the registry, by the name
    config.add_gracia_repository_child("jobs", lambda repository, name: resources.Jobs(repository))
    config.add_gracia_repository_child(
        "deform", lambda repository, name: resources.DeformViews(repository)
    )
    config.add_gracia_repository_child("rec:", resources.resolve_record_key, prefix=True)
    config.add_renderer(views_deform.RENDERER, views_deform.DeformViewRenderer)
    config.add_directive("add_gracia_job", jobs_job.add_gracia_job)
    # a request that does not say what it wants gets data; a browser (it asks for text/html
    # first) gets records
    config.add_accept_view_order("application/json", weighs_more_than="text/html")
    config.add_view_predicate("type_class", types.TypeClassPredicate)
    config.include("gracia.static")  # the scripts and styles: served below /_static/, in the head
    load_modules(settings)  # the stages a job can run: Luigi tasks, found by Luigi
    for target in jobs_job.scan_targets(settings):  # the jobs a person can start: IJob
        config.scan(target)
    extra = types.listed(settings, "gracia.types")
    for system in types.SYSTEM_TYPES:  # what the application knows of its types: IGraciaType
        config.add_gracia_type(system, system=True)
    for content_type in types.available(extra).values():
        config.add_gracia_type(content_type)
    for package in types.packages(settings):  # the system types and every content type there is
        if hasattr(importlib.import_module(package), "includeme"):
            config.include(package)  # it registers its record class and views
    config.scan("gracia.predicates")  # the code behind gracia: predicates (the core ones)
    targets = extensions.scan_targets(settings)
    for target in targets:  # the extensions and what they add with decorators
        config.scan(target)
    extensions.include_modules(config, targets)  # and with an includeme(config)
    config.scan("gracia.views")  # the views register themselves with @view_config
    config.add_api_operation("/", "The repository: its rows")
    config.add_api_operation("/{type}", "A type of record: the numbers of its records")
    config.add_api_operation("/{type}/{number}", "One record and its content")
    config.add_api_operation("/{type}/add", "Add a record of a type (HTML form)", "add")
    config.add_api_operation("/{type}/{number}/edit", "Edit a record (HTML form)", "edit")
    config.add_api_operation("/user/add", "Add a user (HTML form)", "super_edit")
    config.add_api_operation("/user/{number}/edit", "Edit a user (HTML form)", "super_edit")
    config.add_api_operation("/group/add", "Add a group (HTML form)", "manage")
    config.add_api_operation("/group/{number}/edit", "Edit a group (HTML form)", "manage")
    config.add_api_operation("/manage", "Edit the repository (HTML form)", "manage")
    config.add_api_operation(
        "/{type}/manage", "Manage a type: its schema file and validating it (HTML form)", "manage"
    )
    config.add_api_operation("/api", "This document, for the caller (a record for a browser)")
    config.add_api_operation("/jobs", "The jobs page (HTML)", "list_jobs", at="jobs")
    config.add_api_operation(
        "/jobs/list", "The jobs the caller may see, with their progress", "list_jobs", at="jobs"
    )
    config.add_api_operation(
        "/jobs/start",
        "Start a job: POST with ?job=<name> and the fields of its form (start_<name>_job)",
        "list_jobs",
        "post",
        at="jobs",
    )
    config.add_api_operation(
        "/jobs/{id}", "One job: state, task, progress",
        "view_job", at="jobs", shown="list_jobs"
    )
    config.add_api_operation(
        "/jobs/{id}/stop",
        "Stop a job (202; the stop goes on in a process of its own)",
        "stop_job",
        "post",
        at="jobs",
        shown="list_jobs",
    )


def make_config(settings: dict | None = None) -> Configurator:
    config = Configurator(settings=dict(settings or {}))
    config.include("gracia.main")
    return config


def make_app(settings: dict | None = None):
    return make_config(settings).make_wsgi_app()
