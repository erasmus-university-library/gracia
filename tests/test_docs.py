import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path

import pyoxigraph as ox
from harness import FOAF_PERSON, etag_of, graph_of, open_application
from pyramid.registry import Registry
from test_web import WebCase

from gracia.change_events import ChangeEventsWorker, RecordsChanged
from gracia.store import fetch_one
from gracia.utils import assets, xslt

ADMIN = ("admin", "correct horse")
PREFIXES = (
    "@prefix g: <https://gracia.pub/ns/> . @prefix foaf: <http://xmlns.com/foaf/0.1/> "
    ".\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
)
ADA = (
    '<rec:person:1> a g:Record ; g:slug "ada" ; g:label "Ada Lovelace" ; '
    "g:resource_uri "
    "<ark:/1/person/1> ;"
    ' g:modified_at "2026-01-02T03:04:05Z"^^xsd:dateTime ;'
    ' g:resource [ a foaf:Person ; foaf:name "Ada Lovelace" ; foaf:givenName "Ada" ;'
    ' foaf:familyName "Lovelace" ] .\n'
)
THING = (
    '<rec:thing:1> a g:Record ; g:slug "gear" ; g:label "A gear" ; g:resource_uri '
    "<ark:/1/thing/1> "
    ";"
    ' g:is_published "false"^^xsd:boolean ; g:resource [ <http://ex/p> "cog wheel" ] '
    ".\n"
)
USER = (
    "<rec:user:7> a g:Record ; g:resource_uri <ark:/1/user/7> ;"
    ' g:resource [ g:userid "ed" ; g:credentials "SECRETHASH" ; g:name "Ed" ] .\n'
)
HTML = {"Accept": "text/html"}  # without it a page of a type may answer with its RDF


def basic(user: str, password: str) -> dict:
    return {
        "Authorization": "Basic "
        + base64.b64encode(f"{user}:{password}".encode()).decode()
    }


class DocsCase(WebCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.application = open_application(
            self, str(self.tmp / "gracia.db"), cache_dir=self.tmp / "cache"
        )
        self.settings = {"gracia.application": self.application}
        self.application.create_repository(
            "test_repo",
            admin=ADMIN,
            content_types=[FOAF_PERSON],
        )
        self.cache = self.application.cache
        self.put(ADA, THING, USER)

    def put(self, *turtle: str) -> None:
        data = ox.Dataset(
            ox.parse((PREFIXES + "".join(turtle)).encode(), format=ox.RdfFormat.TURTLE)
        )
        with self.application.new_store("test_repo") as store:
            store.put_multi(data)

    def rid(self, rec_uri: str) -> int:
        with self.application.new_store("test_repo") as store:
            return store.record_id_of(rec_uri)  # ty: ignore[invalid-explicit-return]

    def library(self):
        repository = self.application["test_repo"]
        self.addCleanup(repository.close)
        return repository

    def compiled(self):
        return self.cache.docs.compiled(self.library())

    def doc(self, rec_uri: str) -> dict:
        with self.application["test_repo"] as repository:
            return repository.store.get_doc(rec_uri)


class DocTest(DocsCase):
    def test_a_type_with_no_template_has_the_doc_of_the_base_and_the_text_is_literals_only(
        self,
    ):
        doc = self.doc("rec:thing:1")
        self.assertEqual(
            doc,
            {
                "record_id": self.rid("rec:thing:1"),
                "record": "rec:thing:1",
                "type": "thing",
                "slug": "gear",
                "label": "A gear",
                "text": "cog wheel",
                "resource_uri": "ark:/1/thing/1",
                "published": False,
            },  # no modified: it has none, and a field with no value is left out
        )
        self.put(
            '<rec:thing:2> a g:Record ; g:label "x" ; g:resource [ <http://ex/p> '
            '"words" ; '
            "<http://ex/q> <ark:/1/thing/9> ] .\n"
        )
        text = self.doc("rec:thing:2")["text"]
        self.assertEqual(
            text, "words"
        )  # a link points at a record: not words to search for
        user = self.doc("rec:user:7")  # and a credential is never in it
        self.assertNotIn("SECRETHASH", json.dumps(user))
        self.assertIn("Ed", user["text"])

    def test_a_type_adds_its_fields_to_the_ones_of_the_base(self):
        doc = self.doc("rec:person:1")
        self.assertEqual((doc["given_name"], doc["family_name"]), ("Ada", "Lovelace"))
        self.assertEqual(
            (doc["record"], doc["type"], doc["slug"], doc["published"]),
            ("rec:person:1", "person", "ada", True),
        )
        self.assertEqual(doc["modified"], "2026-01-02T03:04:05Z")
        self.assertEqual(
            doc["text"], "Ada Ada Lovelace Lovelace"
        )  # sorted, distinct values
        repository = self.library()  # the type record names the doc template
        self.assertEqual(
            repository.types["person"].doc_template, "person-unittest-01.xsl"
        )
        self.assertIn("given_name", self.person_doc(repository))

    def test_the_rdf_types_are_in_the_doc_whichever_way_the_xml_says_them(self):
        """RDF/XML says one type as the name of an element and the others as properties,
        or all of them as properties: it is as the serializer comes upon them."""
        wrap = (
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns:g="https://gracia.pub/ns/" xmlns:foaf="http://xmlns.com/foaf/0.1/" '
            'xmlns:s="https://schema.org/">'
            '<rdf:Description rdf:about="rec:person:1"><g:resource rdf:nodeID="b1"/>'
            "</rdf:Description>{}</rdf:RDF>"
        )
        person, thing = "http://xmlns.com/foaf/0.1/Person", "https://schema.org/Thing"
        as_property = (
            '<rdf:Description rdf:nodeID="b1"><rdf:type rdf:resource="{}"/>'
            "</rdf:Description>"
        )
        forms = {
            "an element name": (
                '<foaf:Person rdf:nodeID="b1"><foaf:name>Ada</foaf:name></foaf:Person>',
                [person],
            ),
            "a property": (as_property.format(person), [person]),
            "an element name and a property": (
                '<foaf:Person rdf:nodeID="b1"><rdf:type rdf:resource="' + thing + '"/>'
                "</foaf:Person>",
                [person, thing],
            ),
            "no type": ('<rdf:Description rdf:nodeID="b1"/>', None),
        }
        repository = self.application["test_repo"]
        for how, (xml, expected) in forms.items():
            with self.subTest(how=how):
                found = self.cache.docs.bulk_update(
                    repository,
                    {"rec:person:1": self.rid("rec:person:1")},
                    wrap.format(xml),
                )
                self.assertEqual(found["rec:person:1"].get("resource_type"), expected)

    def test_a_repository_with_no_types_has_the_base_docs(self):
        with self.application.new_store("test_repo") as store:
            store.con.execute("DELETE FROM triples WHERE s = 'rec:repository:1'")
        self.cache.check()  # the repository was written: the compile below is new
        self.application.cache.docs.drop("test_repo")
        self.assertEqual(self.doc("rec:thing:1")["slug"], "gear")

    def test_one_run_makes_the_docs_of_many_types_and_a_record_not_in_the_input_is_an_error(
        self,
    ):
        ids = {u: self.rid(u) for u in ("rec:person:1", "rec:thing:1")}
        with self.application.new_store("test_repo") as store:
            xml = store.get_multi(
                ids.values(), compound_records=True, format="rdfxml"
            )  # the compound graphs of both, one input
        made = self.cache.docs.bulk_update(self.library(), ids, xml)
        self.assertEqual(set(made), set(ids))
        self.assertIn("given_name", made["rec:person:1"])
        self.assertNotIn("given_name", made["rec:thing:1"])
        made = self.cache.docs.bulk_update(
            self.library(), {"rec:thing:1": ids["rec:thing:1"], "rec:thing:99": 2}, xml
        )
        self.assertIn("error", made["rec:thing:99"])
        self.assertNotIn("error", made["rec:thing:1"])

    def person_doc(self, repository) -> dict:
        rid = self.rid("rec:person:1")
        xml = graph_of(self.application, "test_repo", rid)
        return self.cache.docs.bulk_update(repository, {"rec:person:1": rid}, xml)[
            "rec:person:1"
        ]

    def test_a_doc_template_that_is_not_a_file_is_left_out_and_the_others_go_on(self):
        repository = self.library()
        for name in ("person-nothing-01.xsl", "evil/../x.xsl"):
            repository.types["person"].doc_template = name
            self.cache.docs.drop("test_repo")
            with self.assertLogs("gracia.cache", level="WARNING"):
                doc = self.person_doc(repository)
            self.assertNotIn("given_name", doc)  # the doc of the base
            self.assertEqual(doc["type"], "person")


class DocCacheTest(DocsCase):
    def make_counter(self):
        calls, docs = [], self.cache.docs
        real = docs.bulk_update
        docs.bulk_update = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        return calls

    def test_a_doc_is_made_once_and_kept_until_the_record_changes(self):
        calls = self.make_counter()
        first = self.doc("rec:person:1")
        self.assertEqual(self.doc("rec:person:1"), first)
        self.assertEqual(len(calls), 1)
        changed = ADA.replace(
            '"Ada Lovelace" ; foaf:givenName "Ada"',
            '"Ada King" ; foaf:givenName "Augusta"',
        )
        self.put(
            changed.replace("2026-01-02T03:04:05Z", "2026-02-02T03:04:05Z")
        )  # the etag follows modified_at
        self.assertEqual(self.doc("rec:person:1")["given_name"], "Augusta")

    def test_a_doc_made_by_other_stylesheets_is_not_used(self):
        calls = self.make_counter()
        self.doc("rec:person:1")
        store = self.cache.docs
        rid, etag = (
            self.rid("rec:person:1"),
            etag_of(self.application, "test_repo", self.rid("rec:person:1")),
        )
        fingerprint = self.compiled().fingerprint
        self.assertIsNotNone(store.get("test_repo", rid, etag, fingerprint))
        self.assertIsNone(store.get("test_repo", rid, etag, "0123456789abcdef"))
        self.assertIsNone(store.get("test_repo", rid, "f" * 16, fingerprint))
        self.assertEqual(len(calls), 1)
        self.cache.forget("test_repo", [rid])  # a deleted record loses its doc
        self.assertIsNone(store.get("test_repo", rid, etag, fingerprint))

    def test_a_doc_that_cannot_be_made_is_not_kept(self):
        with self.application["test_repo"] as repository:
            self.assertIsNone(repository.store.get_doc("rec:thing:99"))
            self.assertIsNone(repository.store.get_doc("rec:nothing:1"))
        store_of = self.application.new_store("test_repo")
        with store_of as store, self.assertRaises(ValueError):  # no stylesheets here
            store.get_doc("rec:thing:1")

    def test_without_a_cache_directory_a_doc_is_made_every_time(self):
        app = open_application(self, str(self.tmp / "gracia.db"))
        self.assertFalse(app.cache.keeps_docs)
        with app["test_repo"] as repository:
            doc = repository.store.get_doc("rec:thing:1")
        self.assertEqual(doc["slug"], "gear")

    def test_the_compiled_stylesheet_is_kept_until_the_repository_is_written(self):
        first = self.compiled()
        self.assertIs(self.compiled(), first)
        self.put(THING.replace("cog wheel", "cam"))
        second = self.compiled()
        self.assertIsNot(second, first)
        self.assertTrue(first.closed)
        self.assertEqual(
            second.fingerprint, first.fingerprint
        )  # the same files, the same stamp


class WorkerTest(DocsCase):
    def run_worker(self):
        registry, events = Registry(), []
        registry.registerHandler(events.append, (RecordsChanged,))
        ChangeEventsWorker(self.application, registry).process_pending()
        return events

    def test_the_docs_of_a_batch_are_in_the_event_and_the_cache_and_the_view_serves_them(
        self,
    ):
        events = self.run_worker()
        docs_ = {}
        for event in events:
            docs_.update(event.docs)
        self.assertEqual(docs_["rec:person:1"]["given_name"], "Ada")
        self.assertEqual(docs_["rec:thing:1"]["type"], "thing")
        self.assertTrue(all("error" not in d for d in docs_.values()))
        rid = self.rid("rec:person:1")
        etag = etag_of(self.application, "test_repo", rid)
        kept = self.cache.docs.get("test_repo", rid, etag, self.compiled().fingerprint)
        self.assertEqual(json.loads(kept), docs_["rec:person:1"])  # ty: ignore[invalid-argument-type]
        calls = []
        real = self.cache.docs.bulk_update
        self.cache.docs.bulk_update = lambda *a, **k: (calls.append(1), real(*a, **k))[
            1
        ]
        got = self.client().get("/test_repo/person/1/doc", headers=basic(*ADMIN))
        self.assertEqual(got.json["given_name"], "Ada")
        self.assertEqual(calls, [])

    def test_a_stylesheet_that_does_not_compile_does_not_stop_the_batch(self):
        def broken(repository):
            raise xslt.XsltError("no")

        self.cache.docs._compile = broken
        with self.assertLogs("gracia.change_events", level="ERROR"):
            events = self.run_worker()
        self.assertTrue(events)
        self.assertTrue(all(event.docs == {} for event in events))
        with self.application.new_store("test_repo") as store:
            self.assertEqual(
                fetch_one(store.con.execute("SELECT count(*) FROM change_events"))[0], 0
            )


class DocViewTest(DocsCase):
    def test_the_doc_of_a_record_as_json_for_who_may_manage_and_nobody_else(self):
        got = self.client().get("/test_repo/person/1/doc", headers=basic(*ADMIN))
        self.assertEqual(got.status_int, 200)
        self.assertEqual(got.content_type, "application/json")
        self.assertEqual(got.json["record"], "rec:person:1")
        self.assertEqual(got.json["family_name"], "Lovelace")
        self.assertIn(
            self.client().get("/test_repo/person/1/doc", status="*").status_int,
            (401, 403),
        )


class SwitchingDocTemplateTest(DocsCase):
    def template(self):
        repository = self.application["test_repo"]
        self.addCleanup(repository.close)
        return repository.types["person"].doc_template

    def post(self, name: str, force: bool = False):
        page = self.client().get(
            "/test_repo/person/manage", headers={**basic(*ADMIN), **HTML}
        )
        form = page.forms["form-type-settings"]
        if force:
            form["docTemplate"].force_value(name)
        else:
            form["docTemplate"] = name
        return form.submit(
            "save", headers={**basic(*ADMIN), "Origin": "http://localhost:80"}
        )

    def write(self, name: str | None) -> None:
        repository = self.application["test_repo"]
        self.addCleanup(repository.close)
        record = repository.types["person"].record
        with self.application.new_store("test_repo") as store:
            quads = [
                q
                for q in store.get(record)
                if not q.predicate.value.endswith("/docTemplate")
            ]
            resource = next(
                q.subject
                for q in quads
                if q.predicate.value.endswith("/schemaTemplate")
            )
            if name:
                quads.append(
                    ox.Quad(
                        resource,
                        ox.NamedNode("https://gracia.pub/ns/docTemplate"),
                        ox.Literal(name),
                    )
                )
            store.put_multi(ox.Dataset(quads))

    def test_the_form_with_no_template_gives_the_doc_of_the_base_and_can_be_saved_again(
        self,
    ):
        page = self.client().get(
            "/test_repo/person/manage", headers={**basic(*ADMIN), **HTML}
        )
        self.assertIn('name="docTemplate"', page.text)  # a select of the files in doc
        self.assertIn("person-unittest-01.xsl", page.text)
        self.assertIn("given_name", self.doc("rec:person:1"))
        self.assertEqual(self.post("").status_int, 302)
        self.assertIsNone(self.template())
        self.assertNotIn(
            "given_name", self.doc("rec:person:1")
        )  # the stylesheets are compiled again
        saved = self.post("person-unittest-01.xsl")  # the page works after a save
        self.assertEqual(saved.status_int, 302)
        self.assertTrue(
            saved.headers["Location"].endswith("/test_repo/person/manage?tab=settings")
        )
        self.assertEqual(self.template(), "person-unittest-01.xsl")

    def test_a_template_written_to_the_type_record_is_used_and_taken_away_again(self):
        self.write(None)
        self.assertNotIn("given_name", self.doc("rec:person:1"))
        self.write("person-unittest-01.xsl")
        self.assertEqual(self.template(), "person-unittest-01.xsl")
        self.assertIn("given_name", self.doc("rec:person:1"))

    def test_a_name_that_is_not_a_file_is_refused(self):
        self.assertEqual(
            self.post("person-evil-09.xsl", force=True).status_int, 200
        )  # the form again, with the error
        self.assertEqual(self.template(), "person-unittest-01.xsl")


class CompiledTest(unittest.TestCase):
    SHEET = '<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">{}</xsl:stylesheet>'

    def test_imports_are_resolved_when_it_is_compiled(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.xsl"
            base.write_text(
                self.SHEET.format(
                    '<xsl:output method="text"/><xsl:template '
                    'match="/">one</xsl:template>'
                )
            )
            top = self.SHEET.format(f'<xsl:import href="{base.as_uri()}"/>')
            compiled = xslt.Compiled(top)
            self.addCleanup(compiled.close)
            self.assertEqual(compiled.transform("<a/>"), "one")
            base.write_text(base.read_text().replace("one", "two"))
            self.assertEqual(
                compiled.transform("<a/>"), "one"
            )  # what it was compiled with
            other = xslt.Compiled(top)
            self.addCleanup(other.close)
            self.assertEqual(other.transform("<a/>"), "two")
        a = xslt.Compiled(self.SHEET.format("<xsl:template match='/'/>"))
        b = xslt.Compiled(self.SHEET.format("<xsl:template match='/'>x</xsl:template>"))
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertNotEqual(a.fingerprint, b.fingerprint)  # it follows the stylesheet

    def test_runs_in_many_threads_take_turns_and_have_their_own_parameters(self):
        compiled = xslt.Compiled(
            self.SHEET.format(
                '<xsl:output method="text"/><xsl:param name="p" '
                "select=\"'none'\"/><xsl:template "
                'match="/"><xsl:value-of select="$p"/></xsl:template>'
            )
        )
        self.addCleanup(compiled.close)
        got = []

        def run(n):
            for _ in range(50):
                got.append((n, compiled.transform("<a/>", {"p": str(n)})))

        threads = [threading.Thread(target=run, args=(n,)) for n in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(got), 300)
        self.assertTrue(all(str(n) == out for n, out in got))
        self.assertEqual(compiled.transform("<a/>"), "none")  # not the run before

    def test_it_is_only_ever_touched_by_one_thread_whoever_asks(self):
        compiled = xslt.Compiled(
            self.SHEET.format(
                "<xsl:output method='text'/><xsl:template match='/'>ok</xsl:template>"
            )
        )
        callers = set()

        def ask():
            callers.add(threading.get_ident())
            self.assertEqual(compiled.transform("<a/>"), "ok")

        threads = [threading.Thread(target=ask) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        compiled.transform("<a/>")
        closer = threading.Thread(target=compiled.close)
        closer.start()
        closer.join()
        self.assertTrue(compiled.closed)
        self.assertEqual(
            len(compiled.threads), 1
        )  # the one thread that made it, ran it and let it go
        self.assertNotIn(
            next(iter(compiled.threads)), callers | {threading.get_ident()}
        )
        self.assertEqual(xslt.owner().thread.name, "saxon")

    def test_an_error_reaches_the_caller_and_a_closed_one_says_so(self):
        with self.assertRaises(xslt.XsltError):
            xslt.Compiled("<xsl:stylesheet")
        compiled = xslt.Compiled(
            self.SHEET.format(
                "<xsl:template match='/'><xsl:message "
                "terminate='yes'>no</xsl:message></xsl:template>"
            )
        )
        self.addCleanup(compiled.close)
        with self.assertRaises(xslt.XsltError):
            compiled.transform("<a/>")
        closed = xslt.Compiled(self.SHEET.format("<xsl:template match='/'/>"))
        closed.close()
        with self.assertRaises(xslt.XsltError):
            closed.transform("<a/>")

    def test_a_fatal_signal_says_what_every_thread_was_doing(self):
        import os
        import subprocess
        import sys
        import textwrap

        code = textwrap.dedent(
            """
            import os, signal, threading, time
            from gracia.utils import xslt
            c = xslt.Compiled('<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"/>')
            def tantivy_say():
                time.sleep(10)
            threading.Thread(target=tantivy_say, name="worker", daemon=True).start()
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGSEGV)
            """
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Fatal Python error: Segmentation fault", done.stderr)
        self.assertIn("in tantivy_say", done.stderr)  # what the other thread was doing


class SchemaModeTest(DocsCase):
    def offered(self) -> dict[str, dict]:
        repository = self.application["test_repo"]
        self.addCleanup(repository.close)
        return {
            f["name"]: f
            for f in self.cache.docs.schema(repository, list(repository.types))
        }

    def test_the_base_fields_are_for_every_type_and_a_type_adds_its_own(self):
        found = self.offered()
        everyone = set(self.library().types)
        base = {
            "record_id",
            "record",
            "type",
            "slug",
            "label",
            "text",
            "resource_uri",
            "resource_type",
            "published",
            "modified",
        }
        for name in base:
            self.assertEqual(set(found[name]["types"]), everyone, name)
        self.assertEqual(found["given_name"]["types"], ["person"])
        self.assertEqual(found["family_name"]["types"], ["person"])
        self.assertEqual(set(found) - {"given_name", "family_name"}, base)
        self.assertEqual(
            {k: v for k, v in found["record_id"].items() if k != "types"},
            {
                "name": "record_id",
                "type": "integer",
                "stored": True,
                "indexed": True,
                "fast": True,
                "tokenizer": "default",
            },
        )  # what the index needs to know
        self.assertEqual(
            (
                found["slug"]["tokenizer"],
                found["text"]["stored"],
                found["modified"]["type"],
            ),
            ("raw", False, "date"),
        )
        self.assertNotIn("conflict", found["label"])

    def test_a_type_without_a_template_has_the_base_fields_only_and_docs_stay_in_them(
        self,
    ):
        found = self.cache.docs.schema(self.library(), ["thing"])
        self.assertNotIn("given_name", {f["name"] for f in found})
        self.assertEqual({tuple(f["types"]) for f in found}, {("thing",)})
        declared = set(self.offered())
        for record in ("rec:person:1", "rec:thing:1", "rec:user:7"):
            self.assertLessEqual(set(self.doc(record)), declared, record)

    def test_two_types_that_say_a_field_differently_are_a_conflict(self):
        base = assets.resolve(self.cache.docs.BUNDLED_BASE).as_uri()
        text = (
            '<xsl:stylesheet version="3.0" '
            'xmlns:xsl="http://www.w3.org/1999/XSL/Transform"'
            ' xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'xmlns:map="http://www.w3.org/2005/xpath-functions/map"'
            ' xmlns:doc="urn:gracia:doc">'
            f'<xsl:import href="{base}"/>'
            '<xsl:template match="/types/type[@slug = \'a\']" mode="schema" '
            'as="map(*)*">'
            '<xsl:variable name="b" as="map(*)*"><xsl:next-match/></xsl:variable>'
            "<xsl:sequence select=\"($b, doc:field('x', 'text'), doc:field('same', "
            "'text'))\"/></xsl:template>"
            '<xsl:template match="/types/type[@slug = \'b\']" mode="schema" '
            'as="map(*)*">'
            '<xsl:variable name="b" as="map(*)*"><xsl:next-match/></xsl:variable>'
            "<xsl:sequence select=\"($b, doc:field('x', 'integer'), doc:field('same', "
            "'text'))\"/></xsl:template>"
            "</xsl:stylesheet>"
        )
        # a first look at the repository drops its stylesheet: do it before putting ours
        # in
        self.cache.check()
        self.cache.docs._compiled["test_repo"] = xslt.Compiled(
            text
        )  # the store closes it with the others
        found = {
            f["name"]: f for f in self.cache.docs.schema(self.library(), ["a", "b"])
        }
        self.assertTrue(found["x"]["conflict"])
        self.assertEqual(found["x"]["types"], ["a", "b"])
        self.assertNotIn("conflict", found["same"])
        self.assertNotIn("conflict", found["label"])
