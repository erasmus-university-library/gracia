import re
from dataclasses import replace
from typing import ClassVar

import pyoxigraph as ox
import tantivy
from pyramid.registry import Registry
from support import must_search
from test_actions import BatchCase
from test_manage import ADMIN, ManageCase

from gracia import search
from gracia.actions import Change, RecordsChanged, process_pending
from gracia.graph import serialize_bytes
from gracia.search import (
    RECORDS,
    REQUIRED_FIELDS,
    Field,
    IndexDefinition,
    Indexes,
    _for_tantivy,
    check_document,
    compare,
    on_records_changed,
)

XSD = "http://www.w3.org/2001/XMLSchema#"


def person(
    number: int,
    name: str,
    nick: str = "",
    published: bool = True,
    modified: str = "2026-05-01T10:00:00.000000Z",
    slug: str = "",
) -> ox.Dataset:
    text = (
        "@prefix g: <https://gracia.pub/ns/> . @prefix foaf: "
        "<http://xmlns.com/foaf/0.1/> . "
        f"@prefix xsd: <{XSD}> .\\n"
        f"<rec:person:{number}> a g:Record ; g:resource_uri <ark:/1/person/{number}> ; "
        "g:label "
        f'"{name}" ; '
        f'g:modified_at "{modified}"^^xsd:dateTime ; g:is_published '
        f'"{str(published).lower()}"^^xsd:boolean ; '
        + (f'g:slug "{slug}" ; ' if slug else "")
        + f'g:resource [ a foaf:Person ; foaf:name "{name}" ; foaf:nick "{nick}" ; '
        + "foaf:knows <ark:/1/person/99> ] ."
    ).replace("\\n", "\n")
    return ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE))


class SearchCase(BatchCase):
    def setUp(self):
        super().setUp()
        self.indexes = Indexes(self.tmp / "index")
        self.app.indexes = self.indexes
        self.registry.registerHandler(on_records_changed, (RecordsChanged,))

    def add(self, *datasets) -> None:
        self.store.put(ox.Dataset(q for d in datasets for q in d))

    def handle(self) -> int:
        return process_pending(self.app, self.registry)

    def hits(
        self,
        query: str,
        fields=("label", "text"),
        definition: IndexDefinition = RECORDS,
    ) -> list[int]:
        index = tantivy.Index.open(str(self.indexes.path("main", definition)))
        index.reload()
        searcher = index.searcher()
        found = searcher.search(index.parse_query(query, list(fields)), 50).hits
        return sorted(searcher.doc(addr).to_dict()["record_id"][0] for _, addr in found)

    def count(self, definition: IndexDefinition = RECORDS) -> int:
        return self.indexes.stats("main")[0]["documents"]

    def docs_of(self, *numbers: int) -> tuple[list[Change], dict[str, dict]]:
        changes = [
            Change(
                self.store.record_id_of(f"rec:person:{n}"),
                f"rec:person:{n}",
                "insert",
            )
            for n in numbers
        ]
        merged = ox.Dataset(
            q
            for c in changes
            for q in ox.parse(
                self.store.closure_source(c.record_id)[1].encode(),
                format=ox.RdfFormat.N_TRIPLES,
            )
        )
        repository = self.app["main"]
        try:
            made = self.app.cache.docs.bulk_update(
                repository,
                {c.record: c.record_id for c in changes},
                serialize_bytes(merged, format=ox.RdfFormat.RDF_XML).decode(),
            )
        finally:
            repository.close()
        return changes, made


class DefinitionTest(SearchCase):
    def test_the_schema_has_the_fields_of_the_definition(self):
        self.assertEqual(
            {f.name for f in RECORDS.fields},
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
        self.assertIsNotNone(RECORDS.schema())
        with self.assertRaisesRegex(ValueError, "unknown type"):
            IndexDefinition("x", (Field("a", "banana"),)).schema()

    def test_the_hash_follows_the_fields(self):
        base = RECORDS.hash()
        self.assertEqual(RECORDS.hash(), base)
        self.assertNotEqual(replace(RECORDS, fields=RECORDS.fields[:-1]).hash(), base)
        self.assertNotEqual(replace(RECORDS, name="other").hash(), base)

    def test_text_and_json_fields_can_be_fast_too(self):
        fast = IndexDefinition(
            "x",
            (
                Field("a", "text", fast=True, tokenizer="raw"),
                Field("d", "json", fast=True),
                Field("record_id", "integer"),
            ),
        )
        self.assertIsNotNone(fast.schema())
        self.assertNotEqual(
            fast.hash(),
            IndexDefinition(
                "x",
                (
                    Field("a", "text", tokenizer="raw"),
                    Field("d", "json"),
                    Field("record_id", "integer"),
                ),
            ).hash(),
        )


class CheckTest(SearchCase):
    good: ClassVar[dict] = {
        "record_id": 1,
        "record": "rec:person:1",
        "label": "Ada",
        "published": True,
        "modified": "2026-05-01T10:00:00Z",
    }

    def test_a_document_that_is_right_has_no_problem(self):
        self.assertEqual(check_document(RECORDS, self.good), [])
        self.assertEqual(
            check_document(RECORDS, {**self.good, "label": ["a", "b"]}), []
        )  # many values
        with_json = replace(RECORDS, fields=(*RECORDS.fields, Field("data", "json")))
        self.assertEqual(
            check_document(with_json, {**self.good, "data": {"a": [1]}}), []
        )

    def test_what_is_wrong_is_said_for_each_field(self):
        wrong = check_document(
            replace(RECORDS, fields=(*RECORDS.fields, Field("data", "json"))),
            {
                "record_id": "1",
                "label": 7,
                "published": "true",
                "modified": "yesterday",
                "data": "text",
                "slug": None,
            },
        )
        text = " | ".join(wrong)
        for part in (
            "record_id",
            "label: 7 is not text",
            "published",
            "modified: 'yesterday' is not a date",
            "data",
            "slug: no value",
        ):
            self.assertIn(part, text)

    def test_a_field_the_schema_does_not_have_is_no_problem_and_is_left_out(self):
        doc = {**self.good, "given_name": "Ada", "odd": None, "nested": {"a": 1}}
        self.assertEqual(check_document(RECORDS, doc), [])
        self.assertEqual(set(_for_tantivy(RECORDS, doc)), set(self.good))

    def test_a_document_needs_the_id_of_its_record_and_an_error_is_a_problem(self):
        self.assertIn("record_id", " ".join(check_document(RECORDS, {"label": "x"})))
        self.assertIn(
            "could not make",
            check_document(RECORDS, {"record": "rec:person:1", "error": "boom"})[0],
        )
        self.assertIn(
            "record_id", " ".join(check_document(RECORDS, {"record_id": True}))
        )  # a boolean is not an id


class HandlerTest(SearchCase):
    def test_a_batch_is_indexed_with_one_commit_and_found(self):
        self.add(
            person(1, "Ada Lovelace", "countess"),
            person(2, "Charles Babbage"),
            person(3, "Grace Hopper"),
        )
        self.assertEqual(self.handle(), 3)
        self.assertEqual(self.count(), 3)
        ada = self.store.record_id_of("rec:person:1")
        self.assertEqual(self.hits("lovelace"), [ada])
        self.assertEqual(
            self.hits("countess"), [ada]
        )  # the text, which is not stored, is searched
        self.assertEqual(len(self.hits("type:person", ("type",))), 3)

    def test_a_record_that_changed_is_replaced_and_one_that_is_gone_is_taken_out(self):
        self.add(person(1, "Ada Lovelace"), person(2, "Charles Babbage"))
        self.handle()
        self.add(person(1, "Ada King", modified="2026-06-01T10:00:00.000000Z"))
        self.handle()
        self.assertEqual(
            (self.count(), self.hits("lovelace"), len(self.hits("king"))), (2, [], 1)
        )
        self.store.delete(["rec:person:2"])
        self.handle()
        self.assertEqual((self.count(), self.hits("babbage")), (1, []))

    def test_doing_the_same_batch_twice_leaves_one_document_for_each(self):
        self.add(person(1, "Ada Lovelace"))
        self.handle()
        changes, docs = self.docs_of(1)
        for _ in range(2):
            self.indexes.update("main", RECORDS, changes, docs)
        self.assertEqual(self.count(), 1)

    def test_a_document_that_is_not_right_is_left_out_and_reported_and_the_rest_goes_in(
        self,
    ):
        self.add(person(1, "Ada Lovelace"), person(2, "Charles Babbage"))
        broken = replace(
            RECORDS,
            fields=tuple(
                replace(f, type="integer") if f.name == "label" else f
                for f in RECORDS.fields
            ),
        )
        changes, docs = self.docs_of(1, 2)
        done = self.indexes.update("main", broken, changes, docs)
        self.assertEqual(
            (done["indexed"], sorted(done["problems"])),
            (0, ["rec:person:1", "rec:person:2"]),
        )
        self.assertIn("label", done["problems"]["rec:person:1"][0])
        self.assertEqual(
            self.indexes.status("main", broken), "ok"
        )  # and the index is not broken by it

    def test_an_index_made_for_another_definition_is_stale_and_is_not_written_to(self):
        self.add(person(1, "Ada Lovelace"))
        self.handle()
        newer = replace(RECORDS, fields=(*RECORDS.fields, Field("extra", "text")))
        self.assertEqual(self.indexes.status("main", newer), "stale")
        self.assertIsNone(self.indexes.open("main", newer))
        changes, docs = self.docs_of(1)
        done = self.indexes.update("main", newer, changes, docs)
        self.assertEqual((done["state"], done["indexed"]), ("stale", 0))
        self.indexes.reset("main", newer)  # a rebuild starts from nothing
        self.assertEqual(self.indexes.status("main", newer), "missing")
        self.indexes.open("main", newer)
        self.assertEqual(self.indexes.status("main", newer), "ok")

    def test_a_doc_with_fields_the_schema_lacks_is_indexed_and_the_fields_are_left_out(
        self,
    ):
        self.add(person(1, "Ada Lovelace"))
        changes, docs = self.docs_of(1)
        docs["rec:person:1"].update(
            {"given_name": "Ada", "odd": None, "nested": {"a": 1}}
        )
        done = self.indexes.update("main", RECORDS, changes, docs)
        self.assertEqual((done["indexed"], done["problems"]), (1, {}))
        self.assertEqual(self.hits("lovelace"), [changes[0].record_id])

    def test_the_docs_of_the_event_are_used_and_only_the_missing_ones_are_made(self):
        self.add(person(1, "Ada Lovelace"), person(2, "Charles Babbage"))
        changes, docs = self.docs_of(1, 2)
        asked = []
        real = self.app.cache.docs.bulk_update
        self.app.cache.docs.bulk_update = lambda repo, ids, xml: (  # ty: ignore[invalid-assignment]
            asked.append(sorted(ids)),
            real(repo, ids, xml),
        )[1]
        graphs = {
            c.record: ox.Dataset(
                ox.parse(
                    self.store.closure_source(c.record_id)[1].encode(),
                    format=ox.RdfFormat.N_TRIPLES,
                )
            )
            for c in changes
        }
        on_records_changed(
            RecordsChanged(
                self.app["main"],
                changes,
                graphs,
                {"rec:person:1": docs["rec:person:1"]},
            )
        )
        self.assertEqual(asked, [["rec:person:2"]])
        self.assertEqual(self.count(), 2)

    def test_without_indexes_the_handler_does_nothing(self):
        self.app.indexes = None
        self.add(person(1, "Ada Lovelace"))
        self.assertEqual(self.handle(), 1)  # the event comes, nobody indexes

    def test_the_repositories_have_indexes_of_their_own(self):
        self.add(person(1, "Ada Lovelace"))
        self.handle()
        self.assertTrue(self.indexes.path("main", RECORDS).is_dir())
        self.assertFalse(self.indexes.path("other", RECORDS).exists())
        self.assertEqual(self.indexes.stats("other")[0]["state"], "missing")


class WiringTest(BatchCase):
    def test_the_setting_makes_the_indexes_and_subscribes_the_handler(self):
        from gracia.main import make_config

        config = make_config(
            {"gracia.application": self.app, "gracia.index_dir": str(self.tmp / "idx")}
        )
        config.make_wsgi_app()
        self.assertEqual(self.app.indexes.directory, self.tmp / "idx")
        self.store.put(person(1, "Ada Lovelace"))
        process_pending(
            self.app, config.registry
        )  # the registry of the application: it has the subscriber
        self.assertEqual(self.app.indexes.stats("main")[0]["documents"], 1)

    def test_without_the_setting_there_are_none(self):
        from gracia.main import make_config

        make_config({"gracia.application": self.app}).make_wsgi_app()
        self.assertIsNone(self.app.indexes)


class StatsRowTest(ManageCase):
    def setUp(self):
        super().setUp()
        import tempfile

        self.application.indexes = Indexes(tempfile.mkdtemp())
        self.addCleanup(
            __import__("shutil").rmtree, self.application.indexes.directory, True
        )

    def row(self, repo="test_repo") -> str:
        _, page = self.tabs(f"/{repo}/manage?tab=stats")
        found = must_search(r'<tr id="stats-index-records".*?</tr>', page, re.S)[0]
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", found)).strip()

    def test_an_index_that_is_not_made_yet_says_so(self):
        self.assertRegex(self.row(), r"^records not made yet 0 \d+ 0 B$")

    def test_an_index_has_its_documents_of_the_records_there_are(self):
        registry = Registry()
        registry.registerHandler(on_records_changed, (RecordsChanged,))
        process_pending(self.application, registry, "test_repo")
        row = self.row()
        records = sum(
            self.application.new_store("test_repo").count_records_by_type().values()
        )
        self.assertRegex(row, rf"^records ok {records} {records} \d")

    def test_without_a_directory_it_says_there_is_no_index(self):
        self.application.indexes = None
        _, page = self.tabs("/test_repo/manage?tab=stats")
        self.assertIn('id="stats-search-none"', page)


class ExtensionCase(ManageCase):
    def setUp(self):
        super().setUp()
        import tempfile

        from gracia.main import make_config

        self.index_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.index_dir, True)
        self.settings["gracia.index_dir"] = self.index_dir  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()
        self.addCleanup(self.config.end)
        self.application.create_repository("archive", content_types=[self.person()])
        self.repository = self.application["archive"]
        self.addCleanup(self.repository.close)

    def extension(self):
        return self.repository.extension("search")

    def put_person(self, number: int, name: str) -> None:
        with self.application.new_store("archive") as store:
            store.put(person(number, name))


class SearchExtensionTest(ExtensionCase):
    def test_it_is_on_in_a_repository_that_never_said_and_ships_with_the_default_definition(  # noqa: E501
        self,
    ):
        from gracia.extensions.search import DEFAULT_INDEXES

        self.assertTrue(self.extension().enabled)
        self.assertEqual(self.extension().settings["indexes"], DEFAULT_INDEXES)
        (definition,) = search.definitions_for(self.repository)
        self.assertEqual(
            definition, RECORDS
        )  # the fields of the default, as `search.RECORDS` has them
        self.assertEqual(
            [f["name"] for f in DEFAULT_INDEXES[0]["fields"]],  # ty: ignore[invalid-argument-type]
            [
                "record_id",
                "record",
                "type",
                "slug",
                "label",
                "text",
                "resource_uri",
                "published",
                "modified",
            ],
        )

    def test_there_is_a_card_for_it_on_the_extensions_tab(self):
        _, page = self.tabs("/archive/manage?tab=extensions")
        card = must_search(
            r'id="extension-search".*?(?=<div class="extension|\Z)', page, re.S
        )[0]
        self.assertIn("Search", card)
        self.assertIn("enabled", card)
        self.assertIn("record_id", card)  # the fields are in the form of its settings
        for label in (
            "stored",
            "indexed",
            "fast",
        ):  # the three boxes of a field say what they are
            self.assertRegex(
                card, rf'<label[^>]*class="form-check-label"[^>]*>{label}</label>'
            )

    def test_the_definition_can_be_changed_and_the_index_is_then_stale(self):
        from gracia.extensions.search import DEFAULT_INDEXES

        indexes = self.application.indexes = Indexes(self.index_dir)
        self.put_person(1, "Ada Lovelace")
        process_pending(self.application, self.config.registry, "archive")
        (before,) = search.definitions_for(self.repository)
        self.assertEqual(indexes.status("archive", before), "ok")
        self.extension().save_settings(
            {
                "indexes": [
                    {**DEFAULT_INDEXES[0], "fields": DEFAULT_INDEXES[0]["fields"][:-1]}
                ]
            }
        )
        (after,) = search.definitions_for(self.application["archive"])
        self.assertEqual(len(after.fields), len(before.fields) - 1)
        self.assertEqual(
            indexes.status("archive", after), "stale"
        )  # until the job has made it again

    def test_a_field_is_added_and_removed_in_the_settings(self):
        from gracia.extensions import ExtensionError

        field = {
            "name": "given_name",
            "type": "text",
            "stored": True,
            "indexed": True,
            "fast": False,
            "tokenizer": "default",
        }
        self.extension().add_field(
            "records", {**field, "types": ["person"], "conflict": False}
        )  # as the schema mode has it
        names = [
            f["name"]
            for f in self.application["archive"]
            .extension("search")
            .settings["indexes"][0]["fields"]
        ]
        self.assertEqual(names[-1], "given_name")
        with self.assertRaisesRegex(ExtensionError, "has a field 'given_name' already"):
            self.application["archive"].extension("search").add_field("records", field)
        self.application["archive"].extension("search").remove_field(
            "records", "given_name"
        )
        names = [
            f["name"]
            for f in self.application["archive"]
            .extension("search")
            .settings["indexes"][0]["fields"]
        ]
        self.assertNotIn("given_name", names)
        with self.assertRaisesRegex(ExtensionError, "no field 'given_name'"):
            self.application["archive"].extension("search").remove_field(
                "records", "given_name"
            )
        with self.assertRaisesRegex(ExtensionError, "no index 'nope'"):
            self.extension().add_field("nope", field)

    def test_what_the_index_cannot_do_without_stays(self):
        from gracia.extensions import ExtensionError

        for name in search.REQUIRED_FIELDS:
            with self.assertRaisesRegex(ExtensionError, "cannot be removed"):
                self.extension().remove_field("records", name)

    def test_a_repository_that_has_it_on_is_indexed_and_one_that_turned_it_off_is_not(
        self,
    ):
        indexes = self.application.indexes = Indexes(self.index_dir)
        self.put_person(1, "Ada Lovelace")
        process_pending(self.application, self.config.registry, "archive")
        self.assertEqual(
            indexes.stats("archive")[0]["documents"], 1 + self.records_of_types()
        )
        self.extension().disable()
        self.assertFalse(self.application["archive"].extension("search").enabled)
        self.assertEqual(search.definitions_for(self.application["archive"]), [])
        before = indexes.stats("archive")[0]["documents"]
        self.put_person(2, "Charles Babbage")
        process_pending(self.application, self.config.registry, "archive")
        self.assertEqual(
            indexes.stats("archive")[0]["documents"], before
        )  # nothing went in
        self.application["archive"].extension("search").enable()
        self.assertTrue(self.application["archive"].extension("search").enabled)

    def records_of_types(self) -> int:
        with self.application.new_store("archive") as store:
            return sum(
                n
                for slug, n in store.count_records_by_type().items()
                if slug != "person"
            )

    def test_the_stats_say_when_search_is_turned_off_in_the_repository(self):
        self.application.indexes = Indexes(self.index_dir)
        self.extension().disable()
        _, page = self.tabs("/archive/manage?tab=stats")
        self.assertIn("turned off in this repository", page)
        self.assertNotIn('id="stats-index-records"', page)


class SearchPageTest(ManageCase):
    HTML: ClassVar[dict] = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}

    def setUp(self):
        super().setUp()
        import tempfile

        from gracia.main import make_config

        self.index_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.index_dir, True)
        self.settings["gracia.index_dir"] = self.index_dir  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()
        self.addCleanup(self.config.end)
        self.application.create_repository("archive", content_types=[self.person()])
        with self.application.new_store("archive") as store:
            store.put(
                ox.Dataset(
                    q
                    for d in (
                        person(1, "Ada Lovelace", "countess"),
                        person(2, "Ada Secret", published=False),
                        person(3, "Charles Babbage"),
                    )
                    for q in d
                )
            )
        process_pending(self.application, self.config.registry, "archive")

    def page(self, query: str = "", headers=None, accept=None):
        return self.client().get(
            f"/archive/{('?q=' + query) if query else ''}",
            headers={**(accept or self.HTML), **(headers or {})},
            expect_errors=True,
        )

    def test_a_browser_gets_a_page_with_a_search_box_and_a_program_still_gets_json(
        self,
    ):
        page = self.page()
        self.assertEqual((page.status_int, page.content_type), (200, "text/html"))
        self.assertRegex(page.text, r'<input type="search" name="q"')
        self.assertNotIn('id="search-hits"', page.text)
        self.assertEqual(
            self.client().get("/archive/").json["id"], "archive"
        )  # no Accept: JSON
        self.assertEqual(
            self.client().get("/archive/", headers={"Accept": "*/*"}).json["id"],
            "archive",
        )
        self.assertEqual(
            self.client()
            .get("/archive/", headers={"Accept": "application/json"})
            .json["id"],
            "archive",
        )

    def test_it_finds_records_and_links_to_them(self):
        page = self.page("lovelace", ADMIN)
        self.assertIn('id="search-hits"', page.text)
        self.assertIn('href="http://localhost/archive/person/1"', page.text)
        self.assertIn("Ada Lovelace", page.text)
        self.assertIn("1 of 1 record found", re.sub(r"\s+", " ", page.text))
        self.assertNotIn("Babbage", page.text)

    def test_the_address_is_the_slug_of_the_record_when_it_has_one_and_its_number_when_not(  # noqa: E501
        self,
    ):
        with self.application.new_store("archive") as store:
            store.put(person(4, "Grace Hopper", slug="grace-hopper"))
        process_pending(self.application, self.config.registry, "archive")
        page = self.page("hopper", ADMIN).text
        self.assertIn('href="http://localhost/archive/person/grace-hopper"', page)
        self.assertNotIn("/archive/person/4", page)
        self.assertIn(
            'href="http://localhost/archive/person/1"',
            self.page("lovelace", ADMIN).text,
        )  # no slug: the number
        record = self.application["archive"]["person"][
            "grace-hopper"
        ]  # the node: its name is what the address ends in
        self.assertEqual(record.__name__, "grace-hopper")
        self.assertEqual(self.application["archive"]["person"]["1"].__name__, "1")
        for url in ("person/grace-hopper", "person/4"):  # both ways reach the record
            self.assertNotEqual(
                self.client()
                .get(f"/archive/{url}", headers=ADMIN, expect_errors=True)
                .status_int,
                404,
                url,
            )

    def test_an_index_made_for_other_fields_keeps_answering_and_says_so(self):
        from gracia.extensions.search import DEFAULT_INDEXES

        fewer = [
            {
                **DEFAULT_INDEXES[0],
                "fields": [
                    f
                    for f in DEFAULT_INDEXES[0]["fields"]
                    if f["name"] != "modified"  # ty: ignore[invalid-argument-type]
                ],
            }
        ]
        node = self.application["archive"]
        self.addCleanup(node.close)
        node.extension("search").save_settings(
            {"indexes": fewer}
        )  # the index on disk was made for all the fields
        page = self.page("lovelace", ADMIN)
        self.assertIn("Ada Lovelace", page.text)  # what it had
        self.assertIn("made for other fields", page.text)
        self.assertNotIn("not there yet", page.text)
        self.assertIn(
            "Nothing found. This index was made for other fields",
            re.sub(r"\s+", " ", self.page("zzzz", ADMIN).text),
        )

    def test_the_words_of_the_record_are_searched_not_only_its_label(self):
        self.assertIn("Ada Lovelace", self.page("countess", ADMIN).text)

    def test_a_record_that_is_not_published_is_for_those_who_may_edit(self):
        self.assertNotIn("Ada Secret", self.page("secret").text)  # anyone
        self.assertIn("Nothing found", self.page("secret").text)
        admin = self.page("secret", ADMIN).text
        self.assertIn("Ada Secret", admin)
        self.assertIn("not published", admin)

    def test_the_records_of_a_type_the_caller_may_not_view_are_not_found(self):
        admin = self.page("type:group", ADMIN).text
        self.assertIn(
            "3 of 3 records found", re.sub(r"\s+", " ", admin)
        )  # the groups are there for an admin
        self.assertIn("rec:group:1", admin)
        anyone = self.page("type:group").text
        self.assertIn(
            "Nothing found", anyone
        )  # and not for anyone else, though the index has them
        self.assertNotIn("rec:group", anyone)

    def test_a_query_that_is_not_well_formed_is_read_as_well_as_it_can_be(self):
        page = self.page("ada%20AND%20(", ADMIN)
        self.assertEqual(page.status_int, 200)
        self.assertIn("Ada Lovelace", page.text)

    def test_what_is_found_depends_on_who_asks_so_it_is_not_shared(self):
        self.assertEqual(self.page("ada").headers["Cache-Control"], "private")

    def test_without_the_indexes_or_with_search_off_or_before_the_index_is_made_it_says_so(  # noqa: E501
        self,
    ):
        # the application is made on first use, which sets the indexes: change them
        # after
        self.client()
        self.application.indexes = None
        self.assertIn("gracia.index_dir", self.page("ada").text)
        self.application.indexes = Indexes(self.index_dir)
        self.application["archive"].extension("search").disable()
        self.assertIn("turned off", self.page("ada").text)
        self.application["archive"].extension("search").enable()
        self.application.indexes = Indexes(self.index_dir + "/elsewhere")
        self.assertIn("not there yet", self.page("ada").text)


OFFERED = [{**f.as_dict(), "types": ["person", "work"]} for f in RECORDS.fields] + [
    {
        "name": "given_name",
        "type": "text",
        "stored": True,
        "indexed": True,
        "fast": False,
        "tokenizer": "default",
        "types": ["person"],
    }
]


class CompareTest(SearchCase):
    def rows(self, definition=RECORDS) -> dict[str, dict]:  # ty: ignore[incompatible-method-override]
        return {row["name"]: row for row in compare(OFFERED, definition)}

    def test_a_field_in_both_is_indexed_and_one_only_the_docs_have_is_available(self):
        rows = self.rows()
        self.assertEqual(rows["label"]["state"], "indexed")
        self.assertEqual(rows["given_name"]["state"], "available")
        self.assertEqual(rows["given_name"]["types"], ["person"])
        self.assertEqual(
            list(rows)[-1], "given_name"
        )  # the offered ones come in their order

    def test_a_field_only_the_definition_has_is_an_orphan_after_the_others(self):
        extra = replace(RECORDS, fields=(*RECORDS.fields, Field("old", "text")))
        rows = compare(OFFERED, extra)
        self.assertEqual((rows[-1]["name"], rows[-1]["state"]), ("old", "orphan"))
        self.assertEqual(
            {r["name"]: r["state"] for r in rows}["given_name"], "available"
        )

    def test_a_field_the_definition_has_otherwise_is_different_and_is_shown_as_the_definition_has_it(  # noqa: E501
        self,
    ):
        other = replace(
            RECORDS,
            fields=tuple(
                replace(f, fast=True, tokenizer="raw") if f.name == "label" else f
                for f in RECORDS.fields
            ),
        )
        row = self.rows(other)["label"]
        self.assertEqual(
            (row["state"], sorted(row["differences"]), row["fast"], row["tokenizer"]),
            ("different", ["fast", "tokenizer"], True, "raw"),
        )

    def test_the_required_fields_are_marked_and_without_a_definition_everything_is_available(  # noqa: E501
        self,
    ):
        self.assertTrue(
            all(
                r["required"] == (r["name"] in REQUIRED_FIELDS)
                for r in compare(OFFERED, RECORDS)
            )
        )
        self.assertEqual({r["state"] for r in compare(OFFERED, None)}, {"available"})


class FieldStatsTest(SearchCase):
    def test_the_fast_fields_are_counted_and_the_others_are_not(self):
        self.add(
            person(1, "Ada Lovelace"), person(2, "Charles Babbage", published=False)
        )
        self.handle()
        found = self.indexes.field_stats("main", RECORDS)
        self.assertEqual(
            (found["record_id"], found["published"], found["modified"]), (2, 2, 2)
        )
        self.assertIsNone(found["label"])  # not a fast field: Tantivy cannot count it
        self.assertEqual(set(found), {f.name for f in RECORDS.fields})

    def test_an_index_that_is_not_there_or_is_stale_has_no_counts(self):
        self.assertEqual(
            set(self.indexes.field_stats("main", RECORDS).values()), {None}
        )
        self.add(person(1, "Ada Lovelace"))
        self.handle()
        newer = replace(RECORDS, fields=(*RECORDS.fields, Field("extra", "text")))
        self.assertEqual(set(self.indexes.field_stats("main", newer).values()), {None})


class SearchTabTest(ManageCase):
    URL = "/archive/manage?tab=search"

    def setUp(self):
        super().setUp()
        import tempfile

        from gracia.main import make_config

        self.index_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.index_dir, True)
        self.settings["gracia.index_dir"] = self.index_dir  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()
        self.addCleanup(self.config.end)
        self.application.create_repository("archive", content_types=[self.person()])
        self.application.indexes = Indexes(self.index_dir)

    def page(self) -> str:
        return self.tabs(self.URL)[1]

    def row(self, name: str) -> str:
        return must_search(rf'<tr id="search-field-{name}".*?</tr>', self.page(), re.S)[
            0
        ]

    def post(self, **data):
        form = {"__formid__": "form-repository-search", "index": "records", **data}
        return self.client().post(
            "/archive/manage?tab=search",
            form,
            headers={**ADMIN, "Origin": "http://localhost:80"},
            expect_errors=True,
        )

    def fields(self) -> list[str]:
        node = self.application["archive"]
        self.addCleanup(node.close)
        return [
            f["name"] for f in node.extension("search").settings["indexes"][0]["fields"]
        ]

    def test_the_fields_of_the_index_are_listed_and_the_ones_only_the_docs_have_are_greyed_with_add(  # noqa: E501
        self,
    ):
        self.assertIn('data-state="indexed"', self.row("label"))
        self.assertIn('name="remove" value="label"', self.row("label"))
        given = self.row("given_name")
        self.assertIn('data-state="available"', given)
        self.assertIn("text-muted", given)
        self.assertIn('name="add" value="given_name"', given)
        self.assertIn("person", given)  # the type that has it
        self.assertIn("every type", self.row("label"))

    def test_the_required_fields_have_no_remove_button_that_works(self):
        self.assertRegex(self.row("record_id"), r'name="remove"[^>]*disabled')
        answer = self.post(remove="record_id")  # a request by hand
        self.assertEqual(answer.status_int, 200)
        self.assertIn("cannot be removed", answer.text)
        self.assertIn("record_id", self.fields())

    def build(self) -> None:
        registry = Registry()
        registry.registerHandler(on_records_changed, (RecordsChanged,))
        with self.application.new_store("archive") as store:
            store.put(person(1, "Ada Lovelace"))
        process_pending(self.application, registry, "archive")

    def test_add_puts_the_field_in_the_settings_and_the_index_needs_a_rebuild(self):
        self.build()
        self.assertNotIn("needs a rebuild", self.page())
        answer = self.post(add="given_name")
        self.assertEqual(answer.status_int, 302)
        self.assertEqual(self.fields()[-1], "given_name")
        self.assertIn('data-state="indexed"', self.row("given_name"))
        self.assertIn("needs a rebuild", self.page())

    def test_remove_takes_it_out_again_and_it_is_available_again(self):
        self.post(add="given_name")
        self.assertEqual(self.post(remove="given_name").status_int, 302)
        self.assertNotIn("given_name", self.fields())
        self.assertIn('data-state="available"', self.row("given_name"))

    def test_a_field_no_doc_has_cannot_be_added_and_an_orphan_can_be_removed(self):
        answer = self.post(add="nothing_has_this")
        self.assertEqual(answer.status_int, 200)
        self.assertIn("no doc has a field", answer.text)
        node = self.application["archive"]
        self.addCleanup(node.close)
        node.extension("search").add_field("records", {"name": "old", "type": "text"})
        self.assertIn('data-state="orphan"', self.row("old"))
        self.assertEqual(self.post(remove="old").status_int, 302)
        self.assertNotIn("old", self.fields())

    def test_the_counts_of_a_built_index_are_shown_for_the_fast_fields(self):
        self.build()
        self.assertRegex(
            self.row("record_id"), r'<td class="text-end">\s*<span>\d+</span>'
        )
        self.assertIn('title="Only a fast field can be counted"', self.row("label"))

    def test_it_says_so_without_the_indexes_or_with_search_off(self):
        # the application is made on first use, which sets the indexes: change them
        # after
        self.client()
        self.application.indexes = None
        self.assertIn("gracia.index_dir", self.page())
        self.application.indexes = Indexes(self.index_dir)
        node = self.application["archive"]
        self.addCleanup(node.close)
        node.extension("search").disable()
        self.assertIn("turned off", self.page())

    def test_a_doc_stylesheet_that_does_not_work_is_a_message_and_not_a_failed_page(
        self,
    ):
        self.client()  # the application is made on first use
        self.application.cache.docs.schema = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        self.assertIn("could not make the schema", self.page())


class RestartTest(ExtensionCase):
    def new_process(self):
        node = self.application["archive"]
        self.addCleanup(node.close)
        return node, Indexes(self.index_dir)

    def build(self) -> None:
        self.application.indexes = Indexes(self.index_dir)
        self.put_person(1, "Ada Lovelace")
        process_pending(self.application, self.config.registry, "archive")

    def test_an_index_made_for_the_default_settings_is_ok_after_a_restart(self):
        self.build()
        node, indexes = self.new_process()
        (definition,) = search.definitions_for(node)
        self.assertEqual(indexes.status("archive", definition), "ok")

    def test_an_index_made_for_saved_settings_is_ok_after_a_restart(self):
        from gracia.extensions.search import DEFAULT_INDEXES

        fewer = [
            {
                **DEFAULT_INDEXES[0],
                "fields": [
                    f
                    for f in DEFAULT_INDEXES[0]["fields"]
                    if f["name"] != "text"  # ty: ignore[invalid-argument-type]
                ],
            }
        ]
        self.extension().save_settings(
            {"indexes": fewer}
        )  # stored as JSON text in the database
        self.build()
        node, indexes = self.new_process()
        (definition,) = search.definitions_for(node)
        self.assertEqual(
            [f.name for f in definition.fields],
            [f["name"] for f in fewer[0]["fields"]],  # ty: ignore[invalid-argument-type]
        )
        self.assertEqual(indexes.status("archive", definition), "ok")
