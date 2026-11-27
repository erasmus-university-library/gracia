import atexit
import shutil
import tempfile
import unittest
from pathlib import Path
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
FIXTURES = Path(__file__).parent / "fixtures"
FOAF_PERSON = ContentType(
    name="Person",
    slug="person",
    class_uri="http://xmlns.com/foaf/0.1/Person",
    schema="person-minimal-01.ttl",
    doc="person-unittest-01.xsl",
    assets=f"{FIXTURES}/foaf/",
)  # the person of the foaf profiles: the tests of the forms and the hooks use it


SHAPE_PREFIXES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix dash: <http://datashapes.org/dash#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix gracia: <https://gracia.pub/ns/> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
@prefix ex: <http://example.org/> .
"""
_SHAPE_DIRS = Path(tempfile.mkdtemp(prefix="gracia_test_shapes_"))
atexit.register(shutil.rmtree, _SHAPE_DIRS, ignore_errors=True)
_SHAPE_TYPES: dict[tuple, ContentType] = {}


def shape_type(
    shape: str,
    *,
    name: str = "Thing",
    slug: str = "thing",
    class_uri: str = "http://example.org/Thing",
    doc: str | None = None,
) -> ContentType:
    """A content type on a shape that the test writes itself, so that the test does not
    depend on the shapes of a preset. `shape` is Turtle (the prefixes in `SHAPE_PREFIXES`
    are added) with a `sh:NodeShape` that has `sh:targetClass <class_uri>`. The file is
    made once for a process, in a directory that stays the same for the same shape, so
    that a database made on it can be reused (`conftest.prepared`)."""
    key = (shape, name, slug, class_uri, doc)
    if key not in _SHAPE_TYPES:
        folder = _SHAPE_DIRS / f"{len(_SHAPE_TYPES)}"
        (folder / "shacl").mkdir(parents=True)
        schema = f"{slug}-unittest-01.ttl"
        (folder / "shacl" / schema).write_text(SHAPE_PREFIXES + shape)
        _SHAPE_TYPES[key] = ContentType(
            name=name,
            slug=slug,
            class_uri=class_uri,
            schema=schema,
            doc=doc,
            assets=f"{folder}/",
        )
    return _SHAPE_TYPES[key]


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
