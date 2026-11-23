import unittest

import duckdb
from pyramid import testing
from pyramid.registry import Registry
from webtest import TestApp

from gracia.application import Application
from gracia.main import init, make_config, opened, shutdown
from gracia.store import Store

__all__ = ["ConfiguredCase", "open_application", "open_store", "opened"]


def open_store(path=":memory:", triples: str = "triples", repo: str = "main", **kwargs):
    return Store(
        duckdb.connect(str(path)), triples, repo, close_connection=True, **kwargs
    )


def open_application(
    test: unittest.TestCase, db_file=":memory:", **kwargs
) -> Application:
    registry = Registry()
    application = init(registry, db_file, **kwargs)
    test.addCleanup(shutdown, registry)
    return application


class ConfiguredCase(unittest.TestCase):
    #: the settings the configuration is made with, besides the application
    settings: dict = {}  # noqa: RUF012

    def setUp(self):
        self.application = open_application(self)
        self.config = make_config(
            {**self.settings, "gracia.application": self.application}
        )
        self.config.begin()
        self.addCleanup(self.config.end)

    def client(self) -> TestApp:
        self.config.commit()
        return TestApp(self.config.make_wsgi_app())

    def request(self, path: str = "/", **kwargs):
        db = self.application.database.cursor()
        self.addCleanup(db.close)
        request = testing.DummyRequest(path=path, **kwargs)
        request.registry = self.application.registry
        request.db = db  # ty: ignore[missing-attribute]
        return request
