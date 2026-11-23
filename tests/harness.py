"""A harness for tests of the web application, on `pyramid.testing`.

`ConfiguredCase` opens an application on a database in memory (`init`: its
database, cache and indexes are in a registry of its own), makes the configuration the
web application makes (`make_config`, committed, and current for the thread the way
`testing.testConfig` makes one: `config.begin()`, and `config.end()` when the test is
over) and says how to get a client and a request. A test adds what it needs to
`self.config` (a view, a type, an extension) before `self.client()`:

    class MyTest(ConfiguredCase):
        def test_a_view(self):
            self.config.add_view(lambda request: Response("ok"), name="probe", ...)
            self.assertEqual(self.client().get("/probe").text, "ok")
"""

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
    """A `Store` on a DuckDB connection of its own, to a file or to memory, that the
    store closes. The other arguments are those of `Store`."""
    return Store(
        duckdb.connect(str(path)), triples, repo, close_connection=True, **kwargs
    )


def open_application(
    test: unittest.TestCase, db_file=":memory:", **kwargs
) -> Application:
    """`init` in a registry of its own (the application of a test; two must not share
    one), and `shutdown` when the test is over. The arguments are those of `init`."""
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
        """A new client (no cookies) on the application as it is configured now."""
        self.config.commit()
        return TestApp(self.config.make_wsgi_app())

    def request(self, path: str = "/", **kwargs):
        """A `DummyRequest` on the registry of the application, with its cursor as
        `request.db` (closed when the test is over)."""
        db = self.application.database.cursor()
        self.addCleanup(db.close)
        request = testing.DummyRequest(path=path, **kwargs)
        request.registry = self.application.registry
        request.db = db  # ty: ignore[missing-attribute]
        return request
