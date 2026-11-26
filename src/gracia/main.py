import contextlib
import importlib
import os
import secrets
from pathlib import Path

from pyramid.config import Configurator
from pyramid.events import ApplicationCreated, NewResponse
from pyramid.registry import Registry
from pyramid.session import SignedCookieSessionFactory

from gracia import config as gracia_config
from gracia import (
    extensions,
    predicates,
    static,
    traversal,
    types,
)
from gracia.actions import BATCH, BATCH_BYTES, POLL, Worker, lock_dir_for
from gracia.application import Application
from gracia.cache import Cache
from gracia.database import Database
from gracia.jobs import job as jobs_job
from gracia.jobs.stage import load_modules
from gracia.security import UsersPolicy
from gracia.views import deform as views_deform
from gracia.views import xslt as views_xslt


def init(
    registry,
    db_file: str | Path | None = ":memory:",
    *,
    quack: str | None = None,
    token: str | None = None,
    cache_dir: str | Path | None = None,
    cache_sizes: dict[str, int] | None = None,
) -> Application:
    database = Database(db_file, quack=quack, token=token)
    watched = db_file if db_file not in (None, ":memory:") else None
    registry["database"] = database
    registry["cache"] = Cache(
        database, registry, db_file=watched, graph_dir=cache_dir, sizes=cache_sizes
    )
    registry["indexes"] = None
    application = Application(None, registry)
    registry["application"] = application
    return application


def init_from_settings(registry, settings: dict, target: str) -> Application:
    cache_dir = settings.get("gracia.cache_dir") or None
    sizes = gracia_config.cache_sizes(settings)
    if gracia_config.is_remote(target):
        return init(
            registry,
            settings.get("gracia.db.file") or None,
            quack=target,
            token=gracia_config.read_token(settings),
            cache_dir=cache_dir,
            cache_sizes=sizes,
        )
    return init(registry, target, cache_dir=cache_dir, cache_sizes=sizes)


def shutdown(registry) -> None:
    registry["cache"].close()
    registry["database"].close()


@contextlib.contextmanager
def opened(db_file: str | Path | None = ":memory:", **kwargs):
    registry = Registry()
    application = init(registry, db_file, **kwargs)
    try:
        yield application
    finally:
        shutdown(registry)


def includeme(config: Configurator) -> None:
    settings = config.registry.settings  # ty: ignore[missing-union-attribute]
    application = settings.get("gracia.application")
    if application is None:  # else one that was opened in this registry (`make_config`)
        target = settings.get("gracia.db") or os.environ.get("GRACIA_DB", "gracia.db")
        application = init_from_settings(config.registry, settings, target)

    def db(request):
        connection = request.registry["database"].cursor()
        request.add_finished_callback(lambda request: connection.close())
        return connection

    config.add_request_method(db, name="db", reify=True)

    def store(request):
        repository = getattr(request.context, "repository", None)
        if repository is None:
            raise LookupError("this request did not reach a repository")
        return repository.store

    config.add_request_method(store, name="store", reify=True)

    if settings.get(
        "gracia.index_dir"
    ):  # the search index follows what the worker hands over
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
            lambda event: (
                event.request.method in ("GET", "HEAD", "OPTIONS") or worker.wake()
            ),
            NewResponse,
        )

    config.set_root_factory(lambda request: Application(request.db, request.registry))
    policy = settings.get("gracia.security_policy")
    if policy is None:
        policy = UsersPolicy()
    config.registry.challenge = getattr(policy, "challenge", False)  # ty: ignore[invalid-assignment]
    config.set_security_policy(policy)
    config.set_default_permission(
        "view"
    )  # default deny: a view without a permission needs "view"

    secret = settings.get("gracia.session_secret")
    if (
        not secret
    ):  # without a setting every process has its own: a flash does not outlive it
        secret = secrets.token_hex(32)
    config.set_session_factory(
        SignedCookieSessionFactory(
            secret,
            cookie_name="gracia_session",
            httponly=True,
            samesite="Lax",
            secure=settings.get("gracia.session_secure", "false").lower()
            in ("1", "true", "yes"),
            timeout=None,
            reissue_time=None,  # looking at the session must not set the cookie again
        )
    )
    config.include(
        "gracia.api"
    )  # the add_api_operation directive and what the API offers
    config.add_directive("add_record_class", types.add_record_class)
    config.add_directive("add_gracia_type", types.add_gracia_type)
    config.add_directive("add_gracia_library", static.add_library)
    config.add_directive("add_gracia_deform_view", views_deform.add_gracia_deform_view)
    config.add_directive("add_gracia_predicate", predicates.add_gracia_predicate)
    config.add_directive("add_gracia_extension", extensions.add_gracia_extension)
    config.add_directive("add_gracia_traversable", traversal.add_gracia_traversable)
    config.add_view_predicate("enabled_extension", extensions.ExtensionPredicate)
    # what is below a repository besides its types (`jobs`, `deform`, `rec:`): the
    # nodes that say so with `@traversable`
    config.scan("gracia.resources")
    config.add_renderer(views_deform.RENDERER, views_deform.DeformViewRenderer)
    config.add_renderer(".xslt", views_xslt.XsltRenderer)  # a stylesheet makes the view
    config.add_directive("add_gracia_job", jobs_job.add_gracia_job)
    # a request that does not say what it wants gets data; a browser (it asks for
    # text/html
    # first) gets records
    config.add_accept_view_order("application/json", weighs_more_than="text/html")
    config.add_view_predicate("type_class", types.TypeClassPredicate)
    config.include(
        "gracia.static"
    )  # the scripts and styles: served below /_static/, in the head
    load_modules(settings)  # the stages a job can run: Luigi tasks, found by Luigi
    for target in jobs_job.scan_targets(settings):  # the jobs a person can start: IJob
        config.scan(target)
    extra = types.listed(settings, "gracia.types")
    for (
        system
    ) in types.SYSTEM_TYPES:  # what the application knows of its types: IGraciaType
        config.add_gracia_type(system, system=True)
    for content_type in types.available(extra).values():
        config.add_gracia_type(content_type)
    for package in types.packages(
        settings
    ):  # the system types and every content type there is
        if hasattr(importlib.import_module(package), "includeme"):
            config.include(package)  # it registers its record class and views
    config.scan(
        "gracia.predicates"
    )  # the code behind gracia: predicates (the core ones)
    targets = extensions.scan_targets(settings)
    for target in targets:  # the extensions and what they add with decorators
        config.scan(target)
    extensions.include_modules(config, targets)  # and with an includeme(config)
    config.scan("gracia.views")  # the views register themselves with @view_config


def make_config(settings: dict | None = None) -> Configurator:
    """The configuration of the web application. With an application in the settings
    (`gracia.application`, one that `init` opened) it is made in the registry of that
    application, which is where its database and cache are; without, the settings say
    which database to open, in a registry of its own."""
    settings = dict(settings or {})
    application = settings.get("gracia.application")
    if application is None:
        config = Configurator(settings=settings)
    else:
        config = Configurator(registry=application.registry)
        config.setup_registry(settings=settings)
    config.include("gracia.main")
    return config


def make_app(settings: dict | None = None):
    return make_config(settings).make_wsgi_app()
