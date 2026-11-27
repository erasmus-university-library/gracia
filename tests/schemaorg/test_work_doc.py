"""The doc of a work: the fields it is searched by, and the work as CSL JSON."""

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import csl_schema
import tantivy
from harness import BUNDLED
from schemaorg.cases import NUMBERS, WorkDocCase, ark, role, work

from gracia.change_events import ChangeEvent
from gracia.search import RECORDS, Field, Indexes, check_document
from gracia.utils import assets

# what is in the repository: people, an organization, series of the four kinds


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
            "record": "rec:work:1",  # and the fields that are made for all records
            "type": "work",
            "resource_uri": "ark:/99999/ir/work/1",
            "resource_type": ["https://schema.org/ScholarlyArticle"],
        }
        self.assertEqual({k: doc[k] for k in wanted}, wanted)
        self.assertNotIn("series_name", doc)

        authors = f"schema:author ( {ark('person', 2)} {ark('person', 1)} )"
        self.put(work(6, "Book", "Two Authors", authors))
        self.assertEqual(self.doc(6)["author_name"], ["Alan Turing", "Ada Lovelace"])
        families = [a["family"] for a in self.csl(6)["author"]]
        self.assertEqual(families, ["Turing", "Lovelace"])

    def test_the_other_kinds_of_work_have_what_is_theirs(self):
        thesis = self.doc(2)
        self.assertEqual(
            (thesis["year"], thesis["work_type"], thesis["csl_type"]),
            (2019, "Thesis", "thesis"),
        )
        self.assertEqual(thesis["language"], "nl")
        names = ["Ada Lovelace", "Delft University"]  # who has a role in it
        self.assertEqual(thesis["contributor_name"], names)
        self.assertEqual(thesis["publisher_name"], "Delft University")  # the grantor
        self.assertNotIn("journal_name", thesis)

        book = self.doc(3)  # in a series of books: the series and not a journal
        self.assertEqual(book["series_name"], "Lecture Notes in Computing")
        self.assertNotIn("journal_name", book)
        self.assertEqual(book["isbn"], ["978-3-16-148410-0"])
        self.assertEqual(book["issn"], ["1611-3349"])
        self.assertEqual((book["year"], book["csl_type"]), (2020, "book"))

        news = self.doc(4)  # in a newspaper, which is its journal
        self.assertEqual(news["journal_name"], "The Daily Gear")
        self.assertEqual((news["csl_type"], news["year"]), ("article-newspaper", 2021))
        self.assertNotIn("author_name", news)

        bare = self.doc(5)  # only a name: the fields of every record only
        for field in ("year", "author_name", "journal_name", "keyword", "doi"):
            self.assertNotIn(field, bare)
        self.assertEqual((bare["work_type"], bare["csl_type"]), ("Dataset", "dataset"))


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

    def test_the_other_works_as_csl(self):
        thesis = self.csl(2)
        self.assertEqual(thesis["type"], "thesis")
        self.assertEqual(thesis["genre"], "Master of Science")
        self.assertEqual(thesis["issued"], {"date-parts": [[2019]]})
        self.assertEqual(thesis["publisher"], "Delft University")
        self.assertEqual(thesis["author"], [{"family": "Turing", "given": "Alan"}])
        self.assertNotIn("editor", thesis)

        book = self.csl(3)  # a series is a collection and not a container
        self.assertEqual(book["collection-title"], "Lecture Notes in Computing")
        self.assertNotIn("container-title", book)
        self.assertEqual(book["ISBN"], "978-3-16-148410-0")
        self.assertEqual(book["issued"], {"date-parts": [[2020, 5]]})

        self.assertEqual(  # only a name: an id and a type
            self.csl(5),
            {"id": "ark:/99999/ir/work/5", "type": "dataset", "title": "Measurements"},
        )

        self.put(work(30, "Report", "A report", f"schema:isPartOf {ark('series', 4)}"))
        report = self.csl(30)
        self.assertEqual(report["collection-title"], "Report Series")
        self.assertNotIn("container-title", report)

        authors = f"schema:author ( {ark('person', 3)} {ark('organization', 1)} )"
        self.put(work(32, "Book", "Dialogues", authors))  # one name is a literal
        self.assertEqual(
            self.csl(32)["author"],
            [{"literal": "Plato"}, {"literal": "Delft University"}],
        )

        both = role("Editor", "person", 1) + " ; " + role("Funder", "organization", 1)
        self.put(work(31, "Chapter", "A chapter", both))
        csl = self.csl(31)
        self.assertEqual(csl["editor"], [{"family": "Lovelace", "given": "Ada"}])
        self.assertNotIn("contributor", csl)
        names = self.doc(31)["contributor_name"]
        self.assertEqual(names, ["Ada Lovelace", "Delft University"])

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

    def test_what_the_input_does_not_have_or_has_wrong_is_left_out_and_does_no_harm(
        self,
    ):
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
        for number, date in enumerate(("1843-7-1", "July 1843", "18430701", ""), 40):
            with self.subTest(date=date):  # not one of the three forms of a date
                self.put(
                    work(number, "Book", "A book", f'schema:datePublished "{date}"')
                )
                self.assertNotIn("issued", self.csl(number))
                self.assertNotIn("year", self.doc(number))

        for number, pages in (
            (50, 'schema:pageStart "10" ; schema:pageEnd "20"'),
            (51, 'schema:pageStart "10"'),
            (52, 'schema:pageStart "7" ; schema:pageEnd "7"'),
        ):
            self.put(work(number, "Chapter", "A chapter", pages))
        found = [self.csl(n)["page"] for n in (50, 51, 52)]
        self.assertEqual(found, ["10-20", "10", "7"])

    def test_the_schema_of_csl_takes_every_item_and_the_batch_and_not_what_is_wrong(
        self,
    ):
        items = [self.csl(n) for n in NUMBERS]
        for number, item in zip(NUMBERS, items, strict=True):
            with self.subTest(work=number):
                self.assertEqual(csl_schema.validate([item]), [])
        self.assertEqual(csl_schema.validate(items), [])
        self.assertEqual(json.loads(json.dumps(items)), items)

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

    def test_the_stylesheet_offers_the_fields_for_works_only_as_a_search_needs(self):
        offered = self.offered()
        for name in self.FIELDS:
            with self.subTest(field=name):
                self.assertEqual(offered[name]["types"], ["work"])
                self.assertFalse(offered[name].get("conflict", False))
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

    def test_the_docs_are_right_by_an_index_and_found_in_it_with_the_blob(self):
        definition = self.definition()
        for number in NUMBERS:
            with self.subTest(work=number):
                self.assertEqual(check_document(definition, self.doc(number)), [])
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

        with tempfile.TemporaryDirectory() as tmp:
            _index, searcher = self.searcher(tmp, numbers=(1,))
            (hit,) = searcher.search(tantivy.Query.all_query(), 5).hits
            stored = json.loads(searcher.doc(hit[1]).to_dict()["csl"][0])
            self.assertEqual(stored["title"], "On the Analytical Engine")
            self.assertEqual(stored["issued"], {"date-parts": [[1843, 7, 1]]})


class TypeTest(WorkDocCase):
    def test_the_type_names_the_stylesheet_and_a_new_repository_has_it(self):
        self.assertEqual(BUNDLED["work"].doc, "work-schemaorg-01.xsl")
        self.assertEqual(  # the choice in the settings of a repository
            assets.docs_of("gracia:assets/", "work"), ["work-schemaorg-01.xsl"]
        )
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        self.assertEqual(repository.types["work"].doc_template, "work-schemaorg-01.xsl")
