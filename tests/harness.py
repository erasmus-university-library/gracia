import unittest
from types import SimpleNamespace

import duckdb
from pyramid import testing
from pyramid.registry import Registry
from webtest import TestApp

from gracia.application import Application
from gracia.main import init, make_config, opened, shutdown
from gracia.store import Store

__all__ = [
    "ConfiguredCase",
    "fake_type",
    "open_application",
    "open_store",
    "opened",
]


def fake_type(sql_hooks=(), property_roles=(), slug: str = "thing", number: int = 5):
    """What a store is configured with (`Store.configure`), without a repository: a
    type that has a number, SHACL roles and the SQL its predicates run on a write."""
    return SimpleNamespace(
        slug=slug,
        number=number,
        property_roles=list(property_roles),
        sql_hooks=list(sql_hooks),
    )


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
