import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path

import pyoxigraph as ox
from pyramid.registry import Registry
from test_web import WebCase

from gracia import types
from gracia.actions import RecordsChanged, process_pending
from gracia.application import Application
from gracia.graph import serialize_bytes
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
    """A file database with the person type, the doc cache on, and two or three
    records."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.application = Application(
            str(self.tmp / "gracia.db"), cache_dir=self.tmp / "cache"
        )
        self.addCleanup(self.application.close)
        self.settings = {"gracia.application": self.application}
        self.application.create_repository(
            "library",
            admin=ADMIN,
            content_types=[types.describe("gracia.types.person")],
        )
        self.cache = self.application.cache
        self.put(ADA, THING, USER)

    def put(self, *turtle: str) -> None:
        data = ox.Dataset(
            ox.parse((PREFIXES + "".join(turtle)).encode(), format=ox.RdfFormat.TURTLE)
        )
        with self.application.open_repository("library") as store:
            store.put(data)

    def rid(self, rec_uri: str) -> int:
        with self.application.open_repository("library") as store:
            return store.record_id_of(rec_uri)

    def library(self):
        repository = self.application["library"]
        self.addCleanup(repository.close)
        return repository

    def compiled(self):
        return self.cache.docs.compiled(self.library())

    def doc(self, rec_uri: str) -> dict:
        return self.cache.doc("library", self.rid(rec_uri), rec_uri)


class DocTest(DocsCase):
    def test_every_doc_has_the_fields_of_the_base(self):
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

    def test_a_record_of_a_type_with_no_template_has_the_doc_of_the_base_only(self):
        self.assertNotIn("given_name", self.doc("rec:thing:1"))

    def test_the_text_is_the_literals_of_the_resource_and_not_its_links(self):
        self.put(
            '<rec:thing:2> a g:Record ; g:label "x" ; g:resource [ <http://ex/p> '
            '"words" ; '
            "<http://ex/q> <ark:/1/thing/9> ] .\n"
        )
        text = self.doc("rec:thing:2")["text"]
        self.assertEqual(
            text, "words"
        )  # a link points at a record: not words to search for

    def test_a_repository_with_no_types_has_the_base_docs(self):
        with self.application.open_repository("library") as store:
            store.con.execute("DELETE FROM triples WHERE s = 'rec:repository:1'")
        self.cache.etags("library", [])  # nothing: the point is the compile below
        self.application.cache.docs.drop("library")
        self.assertEqual(self.doc("rec:thing:1")["slug"], "gear")

    def test_the_text_never_has_a_credential(self):
        doc = self.doc("rec:user:7")
        self.assertNotIn("SECRETHASH", json.dumps(doc))
        self.assertIn("Ed", doc["text"])

    def test_one_run_makes_the_docs_of_records_of_many_types(self):
        ids = {u: self.rid(u) for u in ("rec:person:1", "rec:thing:1")}
        merged = ox.Dataset(
            quad
            for i in ids.values()
            for quad in ox.parse(
                self.cache.graph("library", i), format=ox.RdfFormat.RDF_XML
            )
        )
        made = self.cache.docs.bulk_update(
            self.library(),
            ids,
            serialize_bytes(merged, format=ox.RdfFormat.RDF_XML).decode(),
        )
        self.assertEqual(set(made), set(ids))
        self.assertIn("given_name", made["rec:person:1"])
        self.assertNotIn("given_name", made["rec:thing:1"])

    def test_a_record_that_is_not_in_the_input_is_an_error_and_the_others_are_made(
        self,
    ):
        xml = self.cache.graph("library", self.rid("rec:thing:1")).decode()
        made = self.cache.docs.bulk_update(
            self.library(), {"rec:thing:1": 1, "rec:thing:99": 2}, xml
        )
        self.assertIn("error", made["rec:thing:99"])
        self.assertNotIn("error", made["rec:thing:1"])

    def person_doc(self, repository) -> dict:
        rid = self.rid("rec:person:1")
        xml = self.cache.graph("library", rid).decode()
        return self.cache.docs.bulk_update(repository, {"rec:person:1": rid}, xml)[
            "rec:person:1"
        ]

    def test_a_doc_template_that_is_not_a_file_is_left_out_and_the_others_go_on(self):
        repository = self.library()
        for name in ("doc-nothing-01.xsl", "evil/../x.xsl"):
            repository.res_types["person"].doc_template = name
            self.cache.docs.drop("library")
            with self.assertLogs("gracia.cache", level="WARNING"):
                doc = self.person_doc(repository)
            self.assertNotIn("given_name", doc)  # the doc of the base
            self.assertEqual(doc["type"], "person")

    def test_the_type_record_names_the_doc_template(self):
        repository = self.library()
        self.assertEqual(repository.res_types["person"].doc_template, "doc-full-01.xsl")
        self.assertIn("given_name", self.person_doc(repository))


class DocCacheTest(DocsCase):
    def make_counter(self):
        calls, docs = [], self.cache.docs
        real = docs.bulk_update
        docs.bulk_update = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        return calls

    def test_a_doc_is_made_once_and_kept(self):
        calls = self.make_counter()
        first = self.doc("rec:person:1")
        self.assertEqual(self.doc("rec:person:1"), first)
        self.assertEqual(len(calls), 1)

    def test_a_changed_record_has_a_new_doc(self):
        self.doc("rec:person:1")
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
            self.cache.etags("library", [self.rid("rec:person:1")])[
                self.rid("rec:person:1")
            ][0],
        )
        fingerprint = self.compiled().fingerprint
        self.assertIsNotNone(store.get("library", rid, etag, fingerprint))
        self.assertIsNone(store.get("library", rid, etag, "0123456789abcdef"))
        self.assertIsNone(store.get("library", rid, "f" * 16, fingerprint))
        self.assertEqual(len(calls), 1)

    def test_a_doc_that_cannot_be_made_is_not_kept(self):
        self.assertIsNone(self.cache.doc("library", 99999, "rec:thing:99"))

    def test_without_a_cache_directory_a_doc_is_made_every_time(self):
        app = Application(str(self.tmp / "gracia.db"))
        self.addCleanup(app.close)
        self.assertFalse(app.cache.keeps_docs)
        rid = self.rid("rec:thing:1")
        self.assertEqual(app.cache.doc("library", rid, "rec:thing:1")["slug"], "gear")

    def test_a_deleted_record_loses_its_doc(self):
        self.doc("rec:thing:1")
        rid = self.rid("rec:thing:1")
        self.cache.forget("library", [rid])
        etag = self.cache.etags("library", [rid])[rid][0]
        self.assertIsNone(
            self.cache.docs.get("library", rid, etag, self.compiled().fingerprint)
        )

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
        process_pending(self.application, registry)
        return events

    def test_the_docs_of_a_batch_are_in_the_event_and_in_the_cache(self):
        events = self.run_worker()
        docs_ = {}
        for event in events:
            docs_.update(event.docs)
        self.assertEqual(docs_["rec:person:1"]["given_name"], "Ada")
        self.assertEqual(docs_["rec:thing:1"]["type"], "thing")
        self.assertTrue(all("error" not in d for d in docs_.values()))
        rid = self.rid("rec:person:1")
        etag = self.cache.etags("library", [rid])[rid][0]
        kept = self.cache.docs.get("library", rid, etag, self.compiled().fingerprint)
        self.assertEqual(json.loads(kept), docs_["rec:person:1"])

    def test_the_view_then_serves_what_the_worker_made(self):
        self.run_worker()
        calls = []
        real = self.cache.docs.bulk_update
        self.cache.docs.bulk_update = lambda *a, **k: (calls.append(1), real(*a, **k))[
            1
        ]
        got = self.client().get("/library/person/1/doc", headers=basic(*ADMIN))
        self.assertEqual(got.json["given_name"], "Ada")
        self.assertEqual(calls, [])

    def test_a_stylesheet_that_does_not_compile_does_not_stop_the_batch(self):
        def broken(repository):
            raise xslt.XsltError("no")

        self.cache.docs._compile = broken
        with self.assertLogs("gracia.actions", level="ERROR"):
            events = self.run_worker()
        self.assertTrue(events)
        self.assertTrue(all(event.docs == {} for event in events))
        with self.application.open_repository("library") as store:
            self.assertEqual(
                fetch_one(store.con.execute("SELECT count(*) FROM actions_queue"))[0], 0
            )


class DocViewTest(DocsCase):
    def test_the_doc_of_a_record_as_json_for_who_may_manage(self):
        got = self.client().get("/library/person/1/doc", headers=basic(*ADMIN))
        self.assertEqual(got.status_int, 200)
        self.assertEqual(got.content_type, "application/json")
        self.assertEqual(got.json["record"], "rec:person:1")
        self.assertEqual(got.json["family_name"], "Lovelace")

    def test_nobody_else_sees_it(self):
        web = self.client()
        self.assertIn(
            web.get("/library/person/1/doc", status="*").status_int, (401, 403)
        )


class DocTemplateFieldTest(DocsCase):
    def test_the_edit_form_of_a_type_has_a_select_of_the_files_in_doc(self):
        page = (
            self.client()
            .get("/library/person/manage", headers={**basic(*ADMIN), **HTML})
            .text
        )
        self.assertIn('name="docTemplate"', page)
        self.assertIn("doc-full-01.xsl", page)


class SwitchingDocTemplateTest(DocsCase):
    """The doc template of the type changes: by its form, or by writing the type record,
    which is what an import does."""

    def template(self):
        repository = self.application["library"]
        self.addCleanup(repository.close)
        return repository.res_types["person"].doc_template

    def post(self, name: str, force: bool = False):
        page = self.client().get(
            "/library/person/manage", headers={**basic(*ADMIN), **HTML}
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
        """The type record of the person type with this doc template (None: none)."""
        repository = self.application["library"]
        self.addCleanup(repository.close)
        record = repository.res_types["person"].record
        with self.application.open_repository("library") as store:
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
            store.put(ox.Dataset(quads))

    def test_the_form_with_no_template_gives_the_doc_of_the_base(self):
        self.assertIn("given_name", self.doc("rec:person:1"))
        self.assertEqual(self.post("").status_int, 302)
        self.assertIsNone(self.template())
        self.assertNotIn(
            "given_name", self.doc("rec:person:1")
        )  # the stylesheets are compiled again

    def test_a_template_written_to_the_type_record_is_used_and_taken_away_again(self):
        self.write(None)
        self.assertNotIn("given_name", self.doc("rec:person:1"))
        self.write("doc-full-01.xsl")
        self.assertEqual(self.template(), "doc-full-01.xsl")
        self.assertIn("given_name", self.doc("rec:person:1"))

    def test_a_name_that_is_not_a_file_is_refused(self):
        self.assertEqual(
            self.post("doc-evil-09.xsl", force=True).status_int, 200
        )  # the form again, with the error
        self.assertEqual(self.template(), "doc-full-01.xsl")


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

    def test_runs_in_many_threads_take_turns(self):
        compiled = xslt.Compiled(
            self.SHEET.format(
                '<xsl:output method="text"/><xsl:param name="p"/><xsl:template '
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

    def test_the_parameters_of_a_run_are_not_the_ones_of_the_run_before(self):
        compiled = xslt.Compiled(
            self.SHEET.format(
                '<xsl:output method="text"/><xsl:param name="p" '
                "select=\"'none'\"/><xsl:template "
                'match="/"><xsl:value-of select="$p"/></xsl:template>'
            )
        )
        self.addCleanup(compiled.close)
        self.assertEqual(compiled.transform("<a/>", {"p": "one"}), "one")
        self.assertEqual(compiled.transform("<a/>"), "none")  # not "one"

    def test_it_is_only_ever_touched_by_one_thread_whoever_asks(self):
        """SaxonC objects belong to the thread that made them (a fatal "wrong
        IsolateThread" otherwise): however many threads ask, compile, run and close all
        happen on the owner thread."""
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

    def test_an_error_on_the_owner_thread_reaches_the_caller(self):
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

    def test_a_fatal_signal_says_what_every_thread_was_doing(self):
        """A segmentation fault anywhere in the process is reported by SaxonC's handler
        as a StackOverflowError that says nothing; with Saxon started, the report of the
        threads comes first."""
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

    def test_a_closed_one_says_so(self):
        compiled = xslt.Compiled(self.SHEET.format("<xsl:template match='/'/>"))
        compiled.close()
        with self.assertRaises(xslt.XsltError):
            compiled.transform("<a/>")

    def test_the_fingerprint_follows_the_stylesheet_and_its_imports(self):
        a = xslt.Compiled(self.SHEET.format("<xsl:template match='/'/>"))
        b = xslt.Compiled(self.SHEET.format("<xsl:template match='/'>x</xsl:template>"))
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertNotEqual(a.fingerprint, b.fingerprint)


class ManagePageAfterSaveTest(DocsCase):
    """The manage page of a type after the type was saved: its record has a slug then,
    and looking the record up by its number answers with a redirect to the slug (an
    exception that left the view: the whole request was a 301 to
    `/library/type/person`). The page finds its record without being sent anywhere."""

    def test_the_page_and_a_second_save_work_after_a_save(self):
        web = self.client()
        headers = {**basic(*ADMIN), **HTML}
        for template in ("", "doc-full-01.xsl"):
            page = web.get("/library/person/manage", headers=headers)
            self.assertEqual(page.status_int, 200)
            form = page.forms[
                "form-type-settings"
            ]  # the page has its form: it is not the page of the record
            form["docTemplate"] = template
            saved = form.submit(
                "save", headers={**basic(*ADMIN), "Origin": "http://localhost:80"}
            )
            self.assertEqual(saved.status_int, 302)
            self.assertTrue(
                saved.headers["Location"].endswith(
                    "/library/person/manage?tab=settings"
                )
            )


class SchemaModeTest(DocsCase):
    """The schema mode of the doc stylesheets: the fields the docs have, so the index
    can be compared with them."""

    def offered(self) -> dict[str, dict]:
        repository = self.application["library"]
        self.addCleanup(repository.close)
        return {
            f["name"]: f
            for f in self.cache.docs.schema(repository, list(repository.res_types))
        }

    def test_the_base_fields_are_for_every_type_and_a_type_adds_its_own(self):
        found = self.offered()
        everyone = {"repository", "type", "user", "group", "person"}
        for name in (
            "record_id",
            "record",
            "type",
            "slug",
            "label",
            "text",
            "resource_uri",
            "published",
            "modified",
        ):
            self.assertEqual(set(found[name]["types"]), everyone, name)
        self.assertEqual(found["given_name"]["types"], ["person"])
        self.assertEqual(found["family_name"]["types"], ["person"])
        self.assertEqual(
            {f for f in found} - {"given_name", "family_name"},
            {
                "record_id",
                "record",
                "type",
                "slug",
                "label",
                "text",
                "resource_uri",
                "published",
                "modified",
            },
        )

    def test_a_field_says_what_the_index_needs_to_know(self):
        found = self.offered()
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
        )
        self.assertEqual(
            (
                found["slug"]["tokenizer"],
                found["text"]["stored"],
                found["modified"]["type"],
            ),
            ("raw", False, "date"),
        )
        self.assertNotIn("conflict", found["label"])

    def test_a_type_without_a_template_has_the_base_fields_only(self):
        found = self.cache.docs.schema(self.library(), ["thing"])
        self.assertNotIn("given_name", {f["name"] for f in found})
        self.assertEqual({tuple(f["types"]) for f in found}, {("thing",)})

    def test_what_the_docs_have_is_what_the_schema_says(self):
        """The two descriptions must not drift: every field of every doc is a field of
        the schema mode (a doc field the schema lacks would be dropped by the index
        without anyone knowing)."""
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
        self.cache.docs._compiled["library"] = xslt.Compiled(
            text
        )  # the store closes it with the others
        found = {
            f["name"]: f for f in self.cache.docs.schema(self.library(), ["a", "b"])
        }
        self.assertTrue(found["x"]["conflict"])
        self.assertEqual(found["x"]["types"], ["a", "b"])
        self.assertNotIn("conflict", found["same"])
        self.assertNotIn("conflict", found["label"])
