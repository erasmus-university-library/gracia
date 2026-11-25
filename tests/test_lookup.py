"""Finding the records a reference can point at: a typeahead of the index by label, of
the resources of given rdf types."""

from dataclasses import replace
from unittest.mock import patch

import pyoxigraph as ox
from test_search import SearchCase, person

from gracia.search import RECORDS, Field, IndexDefinition

FOAF = "http://xmlns.com/foaf/0.1/Person"
ORG = "https://schema.org/Organization"


def as_organization(dataset: ox.Dataset) -> ox.Dataset:
    """The record of a person whose resource is an organization (a record of one type
    can have resources of other rdf types)."""
    person, organization = ox.NamedNode(FOAF), ox.NamedNode(ORG)
    return ox.Dataset(
        ox.Quad(
            q.subject,
            q.predicate,
            organization if q.object == person else q.object,
            q.graph_name,
        )
        for q in dataset
    )


class LookupCase(SearchCase):
    def setUp(self):
        super().setUp()
        self.add(
            person(1, "Ada Lovelace"),
            person(2, "Ada Byron"),
            person(3, "Charles Babbage"),
            as_organization(person(4, "Lovelace Foundation")),
        )
        self.handle()

    def labels(self, text, types=()):
        _count, found = self.indexes.lookup("main", RECORDS, text, types)
        return [hit["label"] for hit in found]


class LookupTest(LookupCase):
    def test_every_word_is_the_beginning_of_a_word_of_the_label(self):
        self.assertEqual(self.labels("lov"), ["Ada Lovelace", "Lovelace Foundation"])
        self.assertEqual(self.labels("ada lov"), ["Ada Lovelace"])
        self.assertEqual(self.labels("A"), ["Ada Byron", "Ada Lovelace"])
        self.assertEqual(self.labels("babbage ch"), ["Charles Babbage"])

    def test_what_is_not_the_beginning_of_a_word_is_not_found(self):
        self.assertEqual(self.labels("velace"), [])
        self.assertEqual(self.labels("zzz"), [])
        self.assertEqual(self.labels("ada zzz"), [])

    def test_nothing_typed_is_every_record_by_label(self):
        everyone = [
            "Ada Byron",
            "Ada Lovelace",
            "Charles Babbage",
            "Lovelace Foundation",
        ]
        self.assertEqual(self.labels(""), everyone)
        self.assertEqual(self.labels("  "), everyone)
        self.assertEqual(self.labels("%$#"), everyone)  # no word in it
        self.assertEqual(
            self.labels("", [FOAF]), ["Ada Byron", "Ada Lovelace", "Charles Babbage"]
        )

    def test_it_says_how_many_there_are(self):
        count, found = self.indexes.lookup("main", RECORDS, "lov")
        self.assertEqual((count, len(found)), (2, 2))
        self.assertEqual(self.indexes.lookup("main", RECORDS, "zzz"), (0, []))

    def test_of_a_great_many_only_the_cap_is_taken_and_the_count_is_all_of_them(self):
        with patch("gracia.search.LOOKUP_CAP", 2):
            count, found = self.indexes.lookup("main", RECORDS, "")
        self.assertEqual((count, len(found)), (4, 2))

    def test_digits_are_the_number_of_a_record_as_well_and_that_record_is_first(self):
        self.assertEqual(self.labels("3"), ["Charles Babbage"])  # rec:person:3
        self.assertEqual(self.labels("03"), ["Charles Babbage"])  # a number
        self.assertEqual(self.labels("3", [FOAF]), ["Charles Babbage"])
        self.assertEqual(
            self.labels("3", [ORG]), []
        )  # of the classes that are asked for
        self.assertEqual(self.labels("4"), ["Lovelace Foundation"])
        self.assertEqual(self.labels("99"), [])

    def test_the_record_of_the_number_comes_before_labels_that_begin_with_it(self):
        self.add(person(5, "3D Printing"), person(6, "Zed"))
        self.handle()
        self.assertEqual(self.labels("3"), ["Charles Babbage", "3D Printing"])
        self.assertEqual(self.labels("3d"), ["3D Printing"])  # not only digits: a word

    def test_a_number_among_words_is_a_word(self):
        self.assertEqual(self.labels("ada 3"), [])
        self.assertEqual(self.labels("charles 3"), [])

    def test_the_resources_are_of_the_rdf_types_that_are_asked_for(self):
        self.assertEqual(self.labels("lov", [FOAF]), ["Ada Lovelace"])
        self.assertEqual(self.labels("lov", [ORG]), ["Lovelace Foundation"])
        self.assertEqual(
            self.labels("lov", [FOAF, ORG]), ["Ada Lovelace", "Lovelace Foundation"]
        )
        self.assertEqual(self.labels("lov", ["https://schema.org/Book"]), [])

    def test_a_hit_says_what_to_point_at(self):
        _count, (hit,) = self.indexes.lookup("main", RECORDS, "babbage", [FOAF])
        self.assertEqual(hit["record"], "rec:person:3")
        self.assertEqual(hit["resource_uri"], "ark:/1/person/3")
        self.assertEqual(hit["resource_type"], FOAF)
        self.assertEqual(hit["type"], "person")
        self.assertTrue(hit["published"])

    def test_an_index_without_the_type_of_the_resource_cannot_filter_by_it(self):
        without = replace(
            RECORDS,
            fields=tuple(f for f in RECORDS.fields if f.name != "resource_type"),
        )
        self.indexes.reset("main", without)
        self.indexes.open("main", without)
        with self.assertRaisesRegex(LookupError, "resource_type"):
            self.indexes.lookup("main", without, "lov", [FOAF])
        self.assertEqual(self.indexes.lookup("main", without, "lov"), (0, []))  # none

    def test_an_index_that_is_not_there_is_said_to_be_missing(self):
        other = IndexDefinition("nothing", (Field("label", "text"),))
        with self.assertRaisesRegex(LookupError, "missing"):
            self.indexes.lookup("main", other, "lov")
