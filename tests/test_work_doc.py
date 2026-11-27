"""The doc of a work: the fields it is searched by, and the work as CSL JSON."""

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import csl_schema
import pyoxigraph as ox
import tantivy
from harness import BUNDLED, ConfiguredCase

from gracia.change_events import ChangeEvent
from gracia.search import RECORDS, Field, Indexes, check_document
from gracia.utils import assets

PREFIXES = """
@prefix g: <https://gracia.pub/ns/> . @prefix schema: <https://schema.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""
ROLE = "<http://example.org/vocab/role/%s>"


def record(kind: str, number: int, resource: str) -> str:
    """A record, as populate_schemaorg.sh makes them: resource uri and resource."""
    return (
        f"<rec:{kind}:{number}> a g:Record ; "
        f"g:resource_uri <ark:/99999/ir/{kind}/{number}> ; "
        f"g:resource [ {resource} ] .\n"
    )


def ark(kind: str, number: int) -> str:
    return f"<ark:/99999/ir/{kind}/{number}>"


def identifier(kind: str, value: str, url: str = "") -> str:
    more = f" ; schema:url <{url}>" if url else ""
    return (
        "schema:identifier [ a schema:PropertyValue ; "
        f'schema:propertyID "{kind}" ; schema:value "{value}"{more} ]'
    )


def role(name: str, kind: str, number: int) -> str:
    return (
        f"schema:contributor [ a schema:Role ; schema:roleName {ROLE % name} ; "
        f"schema:contributor {ark(kind, number)} ]"
    )


def person(number: int, name: str, given: str = "", family: str = "") -> str:
    parts = f' ; schema:givenName "{given}" ; schema:familyName "{family}"'
    return record(
        "person",
        number,
        f'a schema:Person ; schema:name "{name}"{parts if family else ""}',
    )


def series(number: int, kind: str, name: str, issn: str) -> str:
    return record(
        "series",
        number,
        f'a schema:{kind} ; schema:name "{name}" ; {identifier("ISSN", issn)}',
    )


def work(number: int, kind: str, title: str, more: str = "") -> str:
    tail = f" ; {more}" if more else ""
    return record("work", number, f'a schema:{kind} ; schema:name "{title}"{tail}')


# what is in the repository: people, an organization, series of the four kinds
BASE = "".join(
    (
        person(1, "Ada Lovelace", "Ada", "Lovelace"),
        person(2, "Alan Turing", "Alan", "Turing"),
        person(3, "Plato"),  # a name and no parts of it
        record(
            "organization",
            1,
            'a schema:CollegeOrUniversity ; schema:name "Delft University" ; '
            "schema:address [ a schema:PostalAddress ; "
            'schema:addressLocality "Delft" ]',
        ),
        series(1, "Periodical", "Journal of Engines", "0302-9743"),
        series(2, "BookSeries", "Lecture Notes in Computing", "1611-3349"),
        series(3, "Newspaper", "The Daily Gear", "1234-5679"),
        series(4, "CreativeWorkSeries", "Report Series", "2345-678X"),
    )
)

ARTICLE = work(
    1,
    "ScholarlyArticle",
    "On the Analytical Engine",
    f"schema:author ( {ark('person', 1)} {ark('person', 2)} ) ; "
    f"schema:publisher {ark('organization', 1)} ; "
    f"schema:isPartOf {ark('series', 1)} ; "
    'schema:datePublished "1843-07-01"^^xsd:date ; schema:volumeNumber "3" ; '
    'schema:issueNumber "12" ; schema:pageStart "666" ; schema:pageEnd "731" ; '
    'schema:inLanguage "en" ; schema:keywords "engines, computation" ; '
    'schema:abstract "Notes on a machine." ; '
    + identifier("DOI", "10.1000/xyz123", "https://doi.org/10.1000/xyz123"),
)
THESIS = work(
    2,
    "Thesis",
    "Computable Numbers",
    f"schema:author ( {ark('person', 2)} ) ; "
    'schema:datePublished "2019"^^xsd:gYear ; '
    'schema:inSupportOf "Master of Science" ; schema:inLanguage "nl" ; '
    + role("ThesisSupervisor", "person", 1)
    + " ; "
    + role("DegreeGrantor", "organization", 1),
)
BOOK = work(
    3,
    "Book",
    "Notes on Computing",
    f"schema:author ( {ark('person', 2)} ) ; "
    f"schema:publisher {ark('organization', 1)} ; "
    f"schema:isPartOf {ark('series', 2)} ; "
    'schema:datePublished "2020-05"^^xsd:gYearMonth ; '
    + identifier("ISBN", "978-3-16-148410-0", "urn:isbn:978-3-16-148410-0"),
)
NEWS = work(
    4,
    "Article",
    "Engines Run Late",
    f'schema:isPartOf {ark("series", 3)} ; schema:datePublished "2021-01-02"^^xsd:date',
)
BARE = work(5, "Dataset", "Measurements")
NUMBERS = (1, 2, 3, 4, 5)


class WorkDocCase(ConfiguredCase):
    def setUp(self):
        super().setUp()
        kinds = ("work", "person", "organization", "series")
        self.application.create_repository(
            "lib",
            admin=("admin", "correct horse battery"),
            content_types=[BUNDLED[k] for k in kinds],
        )
        self.put(BASE, ARTICLE, THESIS, BOOK, NEWS, BARE)

    def put(self, *turtle: str) -> None:
        text = (PREFIXES + "".join(turtle)).encode()
        data = ox.Dataset(ox.parse(text, format=ox.RdfFormat.TURTLE))
        with self.application.new_store("lib") as store:
            store.put_multi(data)

    def doc(self, number: int) -> dict:
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        found = repository.store.get_doc(f"rec:work:{number}")
        assert found is not None
        return found

    def csl(self, number: int) -> dict:
        blob = self.doc(number)["csl"]
        assert isinstance(blob, str)  # a string: the search index gets one term
        return json.loads(blob)


class SearchFieldsTest(WorkDocCase):
    def test_a_scholarly_article_has_the_fields_it_is_searched_by(self):
        doc = self.doc(1)
        wanted = {
            "year": 1843,
            "author_name": ["Ada Lovelace", "Alan Turing"],
            "journal_name": "Journal of Engines",
            "publisher_name": "Delft University",
            "work_type": "ScholarlyArticle",
            "csl_type": "article-journal",
            "language": "en",
            "keyword": ["computation", "engines"],
            "doi": ["10.1000/xyz123"],
            "issn": ["0302-9743"],  # the one of the journal
            "volume": "3",
            "issue": "12",
        }
        self.assertEqual({k: doc[k] for k in wanted}, wanted)
        self.assertNotIn("series_name", doc)

    def test_the_authors_are_in_the_order_of_the_list(self):
        authors = f"schema:author ( {ark('person', 2)} {ark('person', 1)} )"
        self.put(work(6, "Book", "Two Authors", authors))
        self.assertEqual(self.doc(6)["author_name"], ["Alan Turing", "Ada Lovelace"])
        families = [a["family"] for a in self.csl(6)["author"]]
        self.assertEqual(families, ["Turing", "Lovelace"])

    def test_a_thesis_has_what_it_is_and_who_has_a_role_in_it(self):
        doc = self.doc(2)
        self.assertEqual(
            (doc["year"], doc["work_type"], doc["csl_type"], doc["language"]),
            (2019, "Thesis", "thesis", "nl"),
        )
        self.assertEqual(doc["contributor_name"], ["Ada Lovelace", "Delft University"])
        self.assertEqual(doc["publisher_name"], "Delft University")  # the grantor
        self.assertNotIn("journal_name", doc)

    def test_a_book_in_a_series_of_books_has_the_series_and_not_a_journal(self):
        doc = self.doc(3)
        self.assertEqual(doc["series_name"], "Lecture Notes in Computing")
        self.assertNotIn("journal_name", doc)
        self.assertEqual(doc["isbn"], ["978-3-16-148410-0"])
        self.assertEqual(doc["issn"], ["1611-3349"])
        self.assertEqual((doc["year"], doc["csl_type"]), (2020, "book"))

    def test_an_article_in_a_newspaper_has_it_as_its_journal(self):
        doc = self.doc(4)
        self.assertEqual(doc["journal_name"], "The Daily Gear")
        self.assertEqual((doc["csl_type"], doc["year"]), ("article-newspaper", 2021))
        self.assertNotIn("author_name", doc)

    def test_a_work_with_only_a_name_has_the_fields_of_every_record_only(self):
        doc = self.doc(5)
        for field in ("year", "author_name", "journal_name", "keyword", "doi"):
            self.assertNotIn(field, doc)
        self.assertEqual((doc["work_type"], doc["csl_type"]), ("Dataset", "dataset"))

    def test_the_fields_that_are_made_for_all_records_are_still_there(self):
        doc = self.doc(1)
        self.assertEqual(doc["record"], "rec:work:1")
        self.assertEqual(doc["type"], "work")
        self.assertEqual(doc["resource_uri"], "ark:/99999/ir/work/1")
        self.assertEqual(doc["resource_type"], ["https://schema.org/ScholarlyArticle"])


class CslTest(WorkDocCase):
    def test_a_scholarly_article_as_csl(self):
        self.assertEqual(
            self.csl(1),
            {
                "id": "ark:/99999/ir/work/1",
                "type": "article-journal",
                "title": "On the Analytical Engine",
                "author": [
                    {"family": "Lovelace", "given": "Ada"},
                    {"family": "Turing", "given": "Alan"},
                ],
                "issued": {"date-parts": [[1843, 7, 1]]},
                "container-title": "Journal of Engines",
                "volume": "3",
                "issue": "12",
                "page": "666-731",
                "publisher": "Delft University",
                "publisher-place": "Delft",
                "language": "en",
                "abstract": "Notes on a machine.",
                "keyword": "computation, engines",
                "DOI": "10.1000/xyz123",
                "ISSN": "0302-9743",
            },
        )

    def test_a_thesis_as_csl(self):
        csl = self.csl(2)
        self.assertEqual(csl["type"], "thesis")
        self.assertEqual(csl["genre"], "Master of Science")
        self.assertEqual(csl["issued"], {"date-parts": [[2019]]})
        self.assertEqual(csl["publisher"], "Delft University")
        self.assertEqual(csl["author"], [{"family": "Turing", "given": "Alan"}])
        self.assertNotIn("editor", csl)

    def test_a_book_in_a_series_names_the_series_and_not_a_container(self):
        csl = self.csl(3)
        self.assertEqual(csl["collection-title"], "Lecture Notes in Computing")
        self.assertNotIn("container-title", csl)
        self.assertEqual(csl["ISBN"], "978-3-16-148410-0")
        self.assertEqual(csl["issued"], {"date-parts": [[2020, 5]]})

    def test_the_type_follows_the_class_and_what_the_work_is_part_of(self):
        for number, kind, container, expected in (
            (10, "ScholarlyArticle", 1, "article-journal"),
            (11, "ScholarlyArticle", 3, "article-newspaper"),
            (12, "ScholarlyArticle", 2, "article"),  # in a series of books
            (13, "ScholarlyArticle", None, "article"),  # a preprint
            (14, "Article", 1, "article-magazine"),
            (15, "Article", 3, "article-newspaper"),
            (16, "Article", None, "article"),
            (17, "Report", 4, "report"),
            (18, "Book", None, "book"),
            (19, "Dataset", None, "dataset"),
            (20, "SoftwareSourceCode", None, "software"),
            (21, "Chapter", None, "chapter"),
            (22, "Thesis", None, "thesis"),
        ):
            with self.subTest(kind=kind, container=container):
                part = (
                    f"schema:isPartOf {ark('series', container)}" if container else ""
                )
                self.put(work(number, kind, f"A {kind}", part))
                self.assertEqual(self.csl(number)["type"], expected)

    def test_a_report_in_a_series_of_reports_has_it_as_its_collection(self):
        self.put(work(30, "Report", "A report", f"schema:isPartOf {ark('series', 4)}"))
        csl = self.csl(30)
        self.assertEqual(csl["collection-title"], "Report Series")
        self.assertNotIn("container-title", csl)

    def test_an_editor_is_an_editor_and_the_other_roles_are_not(self):
        both = role("Editor", "person", 1) + " ; " + role("Funder", "organization", 1)
        self.put(work(31, "Chapter", "A chapter", both))
        csl = self.csl(31)
        self.assertEqual(csl["editor"], [{"family": "Lovelace", "given": "Ada"}])
        self.assertNotIn("contributor", csl)
        names = self.doc(31)["contributor_name"]
        self.assertEqual(names, ["Ada Lovelace", "Delft University"])

    def test_somebody_with_one_name_is_a_literal_and_so_is_an_organization(self):
        authors = f"schema:author ( {ark('person', 3)} {ark('organization', 1)} )"
        self.put(work(32, "Book", "Dialogues", authors))
        self.assertEqual(
            self.csl(32)["author"],
            [{"literal": "Plato"}, {"literal": "Delft University"}],
        )

    def test_a_work_with_only_a_name_is_an_item_with_an_id_and_a_type(self):
        self.assertEqual(
            self.csl(5),
            {"id": "ark:/99999/ir/work/5", "type": "dataset", "title": "Measurements"},
        )

    def test_what_the_input_does_not_have_is_left_out_and_does_no_harm(self):
        gone = (
            f"schema:author ( {ark('person', 99)} ) ; "
            f"schema:publisher {ark('organization', 99)} ; "
            f"schema:isPartOf {ark('series', 99)} ; "
            'schema:datePublished "soon"'
        )
        self.put(work(33, "Book", "Lost", gone))
        self.assertEqual(
            self.csl(33),
            {"id": "ark:/99999/ir/work/33", "type": "book", "title": "Lost"},
        )
        self.assertNotIn("author_name", self.doc(33))

    def test_a_date_in_a_form_that_is_not_one_of_the_three_is_left_out(self):
        for number, date in enumerate(("1843-7-1", "July 1843", "18430701", ""), 40):
            with self.subTest(date=date):
                self.put(
                    work(number, "Book", "A book", f'schema:datePublished "{date}"')
                )
                self.assertNotIn("issued", self.csl(number))
                self.assertNotIn("year", self.doc(number))

    def test_the_pages_are_the_first_and_the_last_or_the_first_alone(self):
        for number, pages in (
            (50, 'schema:pageStart "10" ; schema:pageEnd "20"'),
            (51, 'schema:pageStart "10"'),
            (52, 'schema:pageStart "7" ; schema:pageEnd "7"'),
        ):
            self.put(work(number, "Chapter", "A chapter", pages))
        found = [self.csl(n)["page"] for n in (50, 51, 52)]
        self.assertEqual(found, ["10-20", "10", "7"])

    def test_every_item_is_right_by_the_schema_of_csl(self):
        for number in NUMBERS:
            with self.subTest(work=number):
                self.assertEqual(csl_schema.validate([self.csl(number)]), [])

    def test_the_batch_is_a_list_of_items_the_schema_takes_and_it_is_json(self):
        items = [self.csl(n) for n in NUMBERS]
        self.assertEqual(csl_schema.validate(items), [])
        self.assertEqual(json.loads(json.dumps(items)), items)

    def test_the_check_of_the_schema_does_not_pass_what_is_wrong(self):
        good = self.csl(1)
        for what, bad in (
            ("a type that is none", {**good, "type": "journal-article"}),
            ("a key the schema has not", {**good, "journal": "x"}),
            ("no id", {k: v for k, v in good.items() if k != "id"}),
            (
                "a date of four parts",
                {**good, "issued": {"date-parts": [[1, 2, 3, 4]]}},
            ),
            ("a name with another key", {**good, "author": [{"name": "x"}]}),
        ):
            with self.subTest(what=what):
                self.assertNotEqual(csl_schema.validate([bad]), [])


class SchemaAndIndexTest(WorkDocCase):
    FIELDS: ClassVar[tuple[str, ...]] = (
        "year",
        "author_name",
        "contributor_name",
        "journal_name",
        "series_name",
        "publisher_name",
        "work_type",
        "csl_type",
        "language",
        "keyword",
        "doi",
        "isbn",
        "issn",
        "volume",
        "issue",
        "csl",
    )
    BY_WORD: ClassVar[tuple[str, ...]] = (
        "author_name",
        "journal_name",
        "publisher_name",
        "keyword",
        "series_name",
    )
    AS_A_WHOLE: ClassVar[tuple[str, ...]] = (
        "work_type",
        "csl_type",
        "doi",
        "isbn",
        "issn",
        "language",
    )

    def offered(self) -> dict[str, dict]:
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        found = self.application.cache.docs.schema(repository, list(repository.types))
        return {f["name"]: f for f in found}

    def test_the_stylesheet_offers_the_fields_for_works_only(self):
        offered = self.offered()
        for name in self.FIELDS:
            with self.subTest(field=name):
                self.assertEqual(offered[name]["types"], ["work"])
                self.assertFalse(offered[name].get("conflict", False))

    def test_the_fields_have_the_types_a_search_needs(self):
        offered = self.offered()
        self.assertEqual(offered["year"]["type"], "integer")
        self.assertTrue(offered["year"]["fast"])  # to sort and filter by
        for name in self.BY_WORD:
            self.assertEqual(offered[name]["type"], "text", name)
            self.assertEqual(offered[name]["tokenizer"], "default", name)
        for name in self.AS_A_WHOLE:
            self.assertEqual(offered[name]["tokenizer"], "raw", name)
        self.assertEqual(offered["csl"]["type"], "text")
        self.assertEqual(offered["csl"]["tokenizer"], "raw")

    def definition(self):
        offered = self.offered()
        extra = tuple(
            Field(
                name,
                offered[name]["type"],
                stored=offered[name]["stored"],
                indexed=offered[name]["indexed"],
                fast=offered[name]["fast"],
                tokenizer=offered[name]["tokenizer"],
            )
            for name in self.FIELDS
        )
        return replace(RECORDS, fields=(*RECORDS.fields, *extra))

    def test_the_docs_are_right_by_an_index_that_has_those_fields(self):
        definition = self.definition()
        for number in NUMBERS:
            with self.subTest(work=number):
                self.assertEqual(check_document(definition, self.doc(number)), [])

    def searcher(self, tmp: str, numbers=NUMBERS):
        definition = self.definition()
        indexes = Indexes(Path(tmp))
        docs = {self.doc(n)["record"]: self.doc(n) for n in numbers}
        changes = [
            ChangeEvent(d["record_id"], uri, "insert") for uri, d in docs.items()
        ]
        indexes.update("lib", definition, changes, docs)
        index = tantivy.Index.open(str(indexes.path("lib", definition)))
        index.reload()
        return index, index.searcher()

    def test_works_are_found_by_year_author_journal_and_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, searcher = self.searcher(tmp)

            def found(field, value):
                query = index.parse_query(f"{field}:{value}", [field])
                hits = searcher.search(query, 20).hits
                return sorted(searcher.doc(a).to_dict()["record"][0] for _, a in hits)

            self.assertEqual(found("year", "1843"), ["rec:work:1"])
            self.assertEqual(found("author_name", "lovelace"), ["rec:work:1"])
            self.assertEqual(
                found("author_name", "turing"),
                ["rec:work:1", "rec:work:2", "rec:work:3"],
            )
            self.assertEqual(found("journal_name", "engines"), ["rec:work:1"])
            self.assertEqual(found("work_type", "Thesis"), ["rec:work:2"])
            self.assertEqual(found("csl_type", "book"), ["rec:work:3"])
            self.assertEqual(found("doi", "10.1000/xyz123"), ["rec:work:1"])
            self.assertEqual(found("series_name", "notes"), ["rec:work:3"])

    def test_the_blob_is_kept_with_the_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            _index, searcher = self.searcher(tmp, numbers=(1,))
            (hit,) = searcher.search(tantivy.Query.all_query(), 5).hits
            stored = json.loads(searcher.doc(hit[1]).to_dict()["csl"][0])
            self.assertEqual(stored["title"], "On the Analytical Engine")
            self.assertEqual(stored["issued"], {"date-parts": [[1843, 7, 1]]})


class TypeTest(WorkDocCase):
    def test_the_type_names_the_stylesheet(self):
        self.assertEqual(BUNDLED["work"].doc, "work-schemaorg-01.xsl")

    def test_a_repository_that_has_the_type_already_can_choose_it_in_its_settings(self):
        self.assertEqual(
            assets.docs_of("gracia:assets/", "work"), ["work-schemaorg-01.xsl"]
        )

    def test_the_type_record_of_a_new_repository_names_it(self):
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        self.assertEqual(repository.types["work"].doc_template, "work-schemaorg-01.xsl")
