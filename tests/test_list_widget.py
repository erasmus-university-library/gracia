"""`schema:author`: an rdf:list of person URIs, in the order the form puts them in."""

import re

import pyoxigraph as ox
from harness import BUNDLED
from test_dates import ADMIN, POSTING, DatesCase

AUTHOR = "https://schema.org/author"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
PEOPLE = [f"ark:/99999/ir/person/{n}" for n in range(1, 5)]
ADD = "/work_repo/work/add"
EDIT = "/work_repo/work/{}/edit"


class ListCase(DatesCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "work_repo",
            admin=("admin", "correct horse battery"),
            content_types=[BUNDLED["work"]],
        )

    def fields(self, path=ADD, authors=(), name="A work"):
        """What the form of the path posts, with the authors as the rows of the list."""
        form = self.web.get(path, headers=ADMIN).forms[0]
        out, inside = [], False
        for field, value in form.submit_fields("save"):
            if (field, value) == ("__start__", "author:sequence"):
                inside = True
                out.append((field, value))
                out += [("item", author) for author in authors]
            elif (field, value) == ("__end__", "author:sequence"):
                inside = False
                out.append((field, value))
            elif not inside:
                if field == "type" and not value:
                    value = "https://schema.org/Thesis"
                out.append((field, name if field == "name" and not value else value))
        return out

    def post(self, path=ADD, authors=(), **kw):
        return self.web.post(
            path,
            params=self.fields(path, authors, **kw),
            headers=POSTING,
            expect_errors=True,
        )

    def work(self, number=1):
        return self.application["work_repo"].types["work"].find_record(str(number))

    def authors(self, number=1) -> list[str]:
        work = self.work(number)
        head = work.graph.objects(work.resource, AUTHOR)
        return [o.value for o in work.graph.walk_list(head[0])] if head else []


class ListFormTest(ListCase):
    def test_the_authors_are_a_list_with_a_handle_on_each_row(self):
        page = self.web.get(ADD, headers=ADMIN).text
        self.assertIn('id="list-author"', page)
        self.assertIn('hx-trigger="reorder"', page)
        self.assertIn('aria-label="Move author 1: arrow up or down"', page)
        self.assertIn("list.js", page)

    def test_the_rows_are_shown_in_the_order_of_the_list(self):
        self.assertEqual(
            self.post(authors=[PEOPLE[2], PEOPLE[0], PEOPLE[1]]).status_int, 302
        )
        page = self.web.get(EDIT.format(1), headers=ADMIN).text
        block = re.search(
            r'(?s)id="list-author".*?'
            r'(?=<input type="hidden" name="__end__" value="author:sequence")',
            page,
        )
        assert block is not None
        self.assertEqual(
            re.findall(r'name="item" value="([^"]*)"', block[0]),
            [PEOPLE[2], PEOPLE[0], PEOPLE[1]],
        )
        self.assertEqual(block[0].count("list-handle"), 3)


class ListStoredTest(ListCase):
    def test_the_authors_are_stored_as_an_rdf_list_in_their_order(self):
        response = self.post(authors=[PEOPLE[1], PEOPLE[0], PEOPLE[2]])
        self.assertEqual(response.status_int, 302, response.text[:500])
        self.assertEqual(self.authors(), [PEOPLE[1], PEOPLE[0], PEOPLE[2]])
        work = self.work()
        head = work.graph.objects(work.resource, AUTHOR)
        self.assertEqual([type(o).__name__ for o in head], ["BlankNode"])  # one value
        cells = work.graph.objects(head[0], ox.NamedNode(RDF + "rest"))
        self.assertEqual(len(cells), 1)
        self.assertEqual(work.validate(), [])

    def test_another_order_replaces_the_list_and_leaves_no_cells_behind(self):
        self.post(authors=PEOPLE[:3])
        response = self.post(EDIT.format(1), authors=PEOPLE[:3][::-1])
        self.assertEqual(response.status_int, 302, response.text[:500])
        self.assertEqual(self.authors(), PEOPLE[:3][::-1])
        work = self.work()
        firsts = [q for q in work.graph if q.predicate == ox.NamedNode(RDF + "first")]
        self.assertEqual(len(firsts), 3)  # and not six
        self.assertEqual(work.validate(), [])

    def test_fewer_authors_are_a_shorter_list(self):
        self.post(authors=PEOPLE[:3])
        self.post(EDIT.format(1), authors=[PEOPLE[1]])
        self.assertEqual(self.authors(), [PEOPLE[1]])

    def test_an_author_given_twice_is_in_the_list_once(self):
        self.post(authors=[PEOPLE[0], PEOPLE[1], PEOPLE[0]])
        self.assertEqual(self.authors(), PEOPLE[:2])

    def test_a_work_needs_an_author(self):
        response = self.post(authors=[])
        self.assertEqual(response.status_int, 200)
        self.assertIn("At least one author is needed", response.text)

    def test_an_author_that_is_no_uri_is_refused(self):
        response = self.post(authors=[PEOPLE[0], "Ada Lovelace"])
        self.assertEqual(response.status_int, 200)
        self.assertIn("Not a URI", response.text)

    def test_an_author_that_is_not_a_list_does_not_conform(self):
        self.post(authors=[PEOPLE[0]])
        work = self.work()
        work.graph.replace(work.resource, AUTHOR, ox.NamedNode(PEOPLE[0]))
        self.assertNotEqual(work.validate(), [])


class ListSequenceTest(ListCase):
    def test_the_rows_that_are_posted_are_shown_again_in_that_order(self):
        # the second row was dragged to the first place: the form posts that order
        params = [
            (n, v)
            for n, v in self.fields(authors=[PEOPLE[1], PEOPLE[0], PEOPLE[2]])
            if n != "save"
        ] + [("sequence", "resource/author"), ("action", "reorder")]
        response = self.web.post(
            "/work_repo/work/sequence", params=params, headers=POSTING
        )
        shown = re.findall(r'name="item" value="([^"]*)"', response.text)
        self.assertEqual(shown, [PEOPLE[1], PEOPLE[0], PEOPLE[2]])
        rows = response.text.split('class="bag-item mb-2"')[1:]
        self.assertEqual(
            [("bag-remove" in r, "bag-add" in r) for r in rows],
            [(True, False), (True, False), (False, True)],
        )  # + at the last
