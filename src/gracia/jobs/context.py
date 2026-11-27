import contextlib
import os

from gracia.utils import config
from pyramid.registry import Registry

from gracia.types.application import Application
from gracia.main import init_from_settings

DEFAULT_WORKERS = 4


class _Use:
    application: Application | None = None
    settings: dict = {}  # noqa: RUF012
    owner = None  # the pid that called `use`
    config = None  # the Pyramid configuration (the registry of predicates and extensions) ...
    config_pid = None  # ... of the process that made it


_current = _Use()
_own: dict[int, Application] = {}


def use(application: Application, settings: dict) -> None:
    if _current.application is not application:  # the configuration is of the application it was made for
        _current.config = _current.config_pid = None
    _current.application, _current.settings, _current.owner = (
        application,
        dict(settings),
        os.getpid(),
    )


def application() -> Application:
    pid = os.getpid()
    if pid == _current.owner:
        return _current.application
    if pid not in _own:
        target = _current.settings.get("gracia.db")
        if not target or not config.is_remote(target):
            raise RuntimeError(
                "a worker process needs the database owner (gracia serve-db): "
                "a job on a file or in-memory database runs with one worker"
            )
        _own[pid] = init_from_settings(Registry(), _current.settings, target)
    return _own[pid]


def settings() -> dict:
    return dict(_current.settings)


def registry():
    return _config().registry


def _config():
    pid = os.getpid()
    if _current.config_pid != pid:
        from gracia.main import make_config

        settings = {k: v for k, v in _current.settings.items() if k.startswith("gracia.")}
        config_ = make_config({**settings, "gracia.application": application()})
        config_.commit()
        _current.config, _current.config_pid = config_, pid
    return _current.config


@contextlib.contextmanager
def repository(repo_id: str):
    config_ = _config()
    config_.begin()
    node = application()[repo_id]
    try:
        yield node
    finally:
        node.close()
        config_.end()


def worker_count(app: Application, settings: dict, batches: int) -> int:
    if not app.database.remote:
        return 1
    wanted = int(settings.get("gracia.jobs_workers") or DEFAULT_WORKERS)
    return max(1, min(wanted, batches))
