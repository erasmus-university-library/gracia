"""A property that is an rdf:list of URIs, in the order the form puts them in."""

import re

import pyoxigraph as ox
from harness import FOAF_PERSON
from widgets_support import (
    PEOPLE,
    POSTING,
    RDF,
    ROSTER,
    FormCase,
    sequence_params,
)

ADD = "/test_repo/roster/add"
EDIT = "/test_repo/roster/{}/edit"
SEQUENCE = "/test_repo/roster/sequence"


class ListCase(FormCase):
    types = (ROSTER, FOAF_PERSON)

    def post_members(self, members, path=ADD, **values):
        return self.post(path, rows={"members": members}, **values)

    def members(self, number=1) -> list[str]:
        roster = self.record("roster", number)
        head = roster.graph.objects(roster.resource, "http://example.org/members")
        if not head:
            return []
        return [o.value for o in roster.graph.walk_list(head[0])]


class ListTest(ListCase):
    def test_the_members_are_a_list_with_a_handle_on_each_row_in_the_order_of_the_list(
        self,
    ):
        page = self.page(ADD)
        self.assertIn('id="list-members"', page)
        self.assertIn('hx-trigger="reorder"', page)
        self.assertIn('aria-label="Move member 1: arrow up or down"', page)
        self.assertIn("list.js", page)
        self.saved(ADD, rows={"members": [PEOPLE[2], PEOPLE[0], PEOPLE[1]]})
        page = self.page(EDIT.format(1))
        block = re.search(
            r'(?s)id="list-members".*?'
            r'(?=<input type="hidden" name="__end__" value="members:sequence")',
            page,
        )
        assert block is not None
        self.assertEqual(
            re.findall(r'name="item" value="([^"]*)"', block[0]),
            [PEOPLE[2], PEOPLE[0], PEOPLE[1]],
        )
        self.assertEqual(block[0].count("list-handle"), 3)

    def test_the_members_are_stored_as_an_rdf_list_that_another_list_replaces(self):
        self.saved(ADD, rows={"members": [PEOPLE[1], PEOPLE[0], PEOPLE[2]]})
        self.assertEqual(self.members(), [PEOPLE[1], PEOPLE[0], PEOPLE[2]])
        roster = self.record("roster")
        head = roster.graph.objects(roster.resource, "http://example.org/members")
        self.assertEqual([type(o).__name__ for o in head], ["BlankNode"])  # one value
        self.assertEqual(roster.validate(), [])
        # another order replaces the list and leaves no cells behind
        self.saved(EDIT.format(1), rows={"members": [PEOPLE[2], PEOPLE[0], PEOPLE[1]]})
        self.assertEqual(self.members(), [PEOPLE[2], PEOPLE[0], PEOPLE[1]])
        roster = self.record("roster")
        first = ox.NamedNode(RDF + "first")
        self.assertEqual(len([q for q in roster.graph if q.predicate == first]), 3)
        self.assertEqual(roster.validate(), [])
        # fewer members are a shorter list, and one given twice is in the list once
        self.saved(EDIT.format(1), rows={"members": [PEOPLE[1]]})
        self.assertEqual(self.members(), [PEOPLE[1]])
        self.saved(EDIT.format(1), rows={"members": [PEOPLE[0], PEOPLE[1], PEOPLE[0]]})
        self.assertEqual(self.members(), PEOPLE[:2])

    def test_a_list_that_is_empty_or_has_what_is_no_uri_is_refused_and_a_node_is_no_list(
        self,
    ):
        response = self.post_members([])
        self.assertEqual(response.status_int, 200)
        self.assertIn("At least one member is needed", response.text)
        response = self.post_members([PEOPLE[0], "Ada Lovelace"])
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a URI", response.text)
        self.saved(ADD, rows={"members": [PEOPLE[0]]})
        roster = self.record("roster")
        roster.graph.replace(
            roster.resource, "http://example.org/members", ox.NamedNode(PEOPLE[0])
        )
        self.assertNotEqual(roster.validate(), [])

    def test_the_rows_that_are_posted_are_shown_again_in_that_order(self):
        # the second row was dragged to the first place: the form posts that order
        params = self.posted(ADD, rows={"members": [PEOPLE[1], PEOPLE[0], PEOPLE[2]]})
        params = [(n, v) for n, v in params if n != "save"]
        response = self.web.post(
            SEQUENCE,
            params=sequence_params(params, "members", "reorder"),
            headers=POSTING,
        )
        shown = re.findall(r'name="item" value="([^"]*)"', response.text)
        self.assertEqual(shown, [PEOPLE[1], PEOPLE[0], PEOPLE[2]])
        rows = response.text.split('class="bag-item mb-2"')[1:]
        self.assertEqual(
            [("bag-remove" in r, "bag-add" in r) for r in rows],
            [(True, False), (True, False), (False, True)],
        )  # + at the last
