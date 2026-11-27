import unittest
from types import SimpleNamespace

import duckdb
from pyramid import testing
from pyramid.registry import Registry
from webtest import TestApp

from gracia.change_events import ChangeEventsWorker
from gracia.main import init, make_config, opened, shutdown
from gracia.store import Store
from gracia.types.application import Application
from gracia.types.collection import CollectionRecord
from gracia.types.organization import OrganizationRecord
from gracia.types.person import PersonRecord
from gracia.types.series import SeriesRecord
from gracia.types.type import ContentType
from gracia.types.work import WorkRecord

__all__ = [
    "FOAF_PERSON",
    "ConfiguredCase",
    "fake_type",
    "open_application",
    "open_store",
    "opened",
]


BUNDLED = {  # the types of gracia that are on schema.org, by slug
    cls.content_type.slug: cls.content_type
    for cls in (
        PersonRecord,
        OrganizationRecord,
        SeriesRecord,
        CollectionRecord,
        WorkRecord,
    )
}
FOAF_PERSON = ContentType(
    name="Person",
    slug="person",
    class_uri="http://xmlns.com/foaf/0.1/Person",
    schema="person-minimal-01.ttl",
    doc="person-schemaorg-01.xsl",
)  # the person of the foaf profiles: the tests of the forms and the hooks use it


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


def etag_of(application, repo: str, record_id: int | None) -> str:
    """The etag of a record that is there, as the cache of the application has it."""
    assert record_id is not None
    with application.new_store(repo) as store:
        etag = store.etags([record_id])[record_id]
    assert etag is not None
    return etag


def graph_of(application, repo: str, record_id: int | None) -> str:
    """The compound graph of a record as RDF/XML, from the cache of the application
    ("" if there is no such record)."""
    assert record_id is not None
    with application.new_store(repo) as store:
        return store.get_multi([record_id], compound_records=True, format="rdfxml")


def stopped_worker(application) -> ChangeEventsWorker:
    """A worker that is not running, set on the cache before the reads that ask it to
    make records again (`ChangeEventsWorker.rework`): a test makes it work with `worker.turn()`."""
    worker = ChangeEventsWorker(application, application.registry)
    worker.stop()
    application.cache.worker = worker
    return worker


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
