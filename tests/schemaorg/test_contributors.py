"""The contributors of a work: each in a role (a blank node of schema:Role that has the
role and the person or organization it is for), and what follows from the role."""

import html
import json
import re
import unittest
from pathlib import Path
from typing import ClassVar

import colander
import pyoxigraph as ox
from schemaorg.cases import ADA, ADMIN, DELFT, POSTING, WORK, ReferenceCase
from support import must_search, uri_select

from gracia import forms, uri
from gracia.graph import Graph

SCHEMA = "https://schema.org/"
ROLE = "http://example.org/vocab/role/"
EX = "http://example.org/shapes#"
PERSON = SCHEMA + "Person"
ROLES = [
    "Contributor",
    "ThesisSupervisor",
    "CoSupervisor",
    "ThesisAdvisor",
    "ExternalReviewer",
    "Expert",
    "Editor",
    "Funder",
    "Sponsor",
    "DegreeGrantor",
]
# the roles that only a thesis has (the shape of the work says so)
THESIS_ROLES = ["ThesisSupervisor", "CoSupervisor", "ThesisAdvisor", "DegreeGrantor"]
PEOPLE_ROLES = {
    "ThesisSupervisor",
    "CoSupervisor",
    "ThesisAdvisor",
    "ExternalReviewer",
    "Expert",
    "Editor",
}


class ContributorCase(ReferenceCase):
    def post_work(self, contributors=(), kind="Thesis", name="A work"):
        """Save a work of the kind that has these contributors: (role, uri) each."""
        params = []
        for field, value in self.fields(WORK, [ADA], name):
            if (field, value) == ("__end__", "resource:mapping"):
                params += [("__start__", "contributor:sequence")]
                for role, who in contributors:
                    params += [
                        ("__start__", "item:mapping"),
                        ("roleName", ROLE + role if role else ""),
                        ("contributor", who),
                        ("__end__", "item:mapping"),
                    ]
                params += [("__end__", "contributor:sequence")]
            params.append((field, SCHEMA + kind if field == "type" else value))
        return self.web.post(WORK, params=params, headers=POSTING, expect_errors=True)

    def work_graph(self, number=1):
        return self.application["refs"].types["work"].find_record(str(number))

    @staticmethod
    def roles_of(work):
        def one(node, predicate):
            return [o.value for o in work.graph.objects(node, predicate)]

        return [
            (
                one(node, SCHEMA + "roleName"),
                one(node, SCHEMA + "contributor"),
                one(node, forms.RDF_TYPE),
            )
            for node in work.graph.objects(work.resource, SCHEMA + "contributor")
        ]

    def added_row(self):
        """The form of a new contributor, as the Add button of the page asks for it."""
        params = [(f, v) for f, v in self.fields(WORK, [ADA]) if f not in ("save",)] + [
            ("sequence", "resource/contributor"),
            ("action", "add"),
        ]
        return self.web.post("/refs/work/sequence", params=params, headers=POSTING).text


class RoleFormTest(ContributorCase):
    def test_a_contributor_has_a_role_to_choose_by_name_with_the_icon_of_the_shape_and_a_reference(
        self,
    ):
        page = self.web.get(WORK, headers=ADMIN).text
        self.assertIn('id="nodes-contributor"', page)
        self.assertIn("Add contributor", page)

        row = self.added_row()
        select = uri_select(row, "roleName")
        self.assertEqual([v for v, _ in select.items], ["", *[ROLE + r for r in ROLES]])
        labels = dict(select.items)
        self.assertEqual(labels[ROLE + "ThesisAdvisor"], "Thesis advisor")
        self.assertEqual(labels[ROLE + "CoSupervisor"], "Co-supervisor")
        self.assertEqual(labels[ROLE + "Funder"], "Funder")
        self.assertIn('class="reference input-group', row)

        row = self.added_row()
        icons = dict(re.findall(r'data-value="([^"]+)"[^>]*data-icon="([^"]*)"', row))
        self.assertEqual(icons[ROLE + "Funder"], "cash-coin")
        self.assertEqual(icons[ROLE + "DegreeGrantor"], "mortarboard")
        self.assertTrue(all(icons[ROLE + r] for r in ROLES))

    def test_the_roles_that_are_for_theses_only_say_so_to_the_menu(self):
        row = self.added_row()
        only = dict(
            re.findall(r'data-value="([^"]+)"[^>]*data-only-for="([^"]*)"', row)
        )
        self.assertEqual(set(only), {ROLE + r for r in THESIS_ROLES})
        for role in THESIS_ROLES:
            self.assertEqual(
                json.loads(html.unescape(only[ROLE + role])), [SCHEMA + "Thesis"], role
            )


class FollowTest(ContributorCase):
    def follow(self):
        row = self.added_row()
        found = must_search(r'data-follow="([^"]*)"', row)
        return json.loads(html.unescape(found[1]))

    def test_the_reference_follows_the_role_a_person_or_an_organization_and_a_new_record_of_it(
        self,
    ):
        follow = self.follow()
        self.assertEqual(follow["name"], "roleName")
        self.assertEqual(set(follow["map"]), {ROLE + r for r in ROLES})

        found = self.follow()["map"]
        for role in ROLES:
            with self.subTest(role=role):
                classes = found[ROLE + role]["classes"]
                if role in PEOPLE_ROLES:
                    self.assertEqual(classes, [PERSON])
                elif (
                    role == "Contributor"
                ):  # with no more said: a person or an organization
                    self.assertEqual(len(classes), 7)
                    self.assertIn(PERSON, classes)
                else:
                    self.assertEqual(len(classes), 6)
                    self.assertIn(SCHEMA + "CollegeOrUniversity", classes)

        found = self.follow()["map"]
        funder, advisor = found[ROLE + "Funder"], found[ROLE + "ThesisAdvisor"]
        self.assertEqual([n["label"] for n in funder["new"]], ["Organization"])
        self.assertEqual([n["label"] for n in advisor["new"]], ["Person"])
        self.assertTrue(funder["new"][0]["url"].endswith("/organization/add?embed=1"))
        self.assertTrue(advisor["new"][0]["url"].endswith("/person/add?embed=1"))
        self.assertEqual((funder["icon"], advisor["icon"]), ("building", "person"))

    def test_before_a_role_is_chosen_it_is_a_person_or_an_organization(self):
        row = self.added_row()
        classes = json.loads(
            html.unescape(must_search(r'data-classes="([^"]*)"', row)[1])
        )
        self.assertEqual(len(classes), 7)  # the six of an organization, and a person


class SaveTest(ContributorCase):
    def test_a_contributor_is_a_role_node_that_has_the_role_and_who_it_is_for(self):
        response = self.post_work([("Funder", DELFT), ("ThesisAdvisor", ADA)])
        self.assertEqual(response.status_int, 302, response.text[:600])
        work = self.work_graph()
        self.assertCountEqual(
            self.roles_of(work),
            [
                ([ROLE + "Funder"], [DELFT], [SCHEMA + "Role"]),
                ([ROLE + "ThesisAdvisor"], [ADA], [SCHEMA + "Role"]),
            ],
        )
        self.assertEqual(work.validate(), [])

        self.assertEqual(self.post_work().status_int, 302)  # none is needed
        self.assertEqual(self.roles_of(self.work_graph(2)), [])

    def test_a_role_needs_somebody_it_is_for_and_a_role_that_is_none_of_them_is_refused(
        self,
    ):
        response = self.post_work([("Funder", "")])
        self.assertEqual(response.status_int, 200)
        self.assertIn("Required", response.text)

        params = []
        for field, value in self.fields(WORK, [ADA]):
            if (field, value) == ("__end__", "resource:mapping"):
                params += [
                    ("__start__", "contributor:sequence"),
                    ("__start__", "item:mapping"),
                    ("roleName", ROLE + "Janitor"),
                    ("contributor", ADA),
                    ("__end__", "item:mapping"),
                    ("__end__", "contributor:sequence"),
                ]
            params.append((field, value))
        response = self.web.post(
            WORK, params=params, headers=POSTING, expect_errors=True
        )
        self.assertEqual(response.status_int, 200)

    def test_the_form_shows_the_role_and_the_label_of_who_it_is_for_and_the_bar_not_the_uris(
        self,
    ):
        self.post_work([("Funder", DELFT)])
        page = self.web.get("/refs/work/1/edit", headers=ADMIN).text
        select = uri_select(page, "roleName")
        self.assertEqual((select.chosen, select.selected), (ROLE + "Funder", 1))
        tags = re.findall(r"<input[^>]*reference-input[^>]*>", page)
        self.assertTrue(any('value="Delft University"' in t for t in tags))

        self.post_work([("Funder", DELFT)])
        page = self.web.get("/refs/work/1/edit", headers=ADMIN).text
        bar = must_search(
            r'(?s)<summary class="accordion-button[^>]*>.*?</summary>',
            page.split('id="nodes-contributor"')[1],
        )[0]
        badge = must_search(
            r'<span class="badge text-bg-secondary[^>]*>([^<]*)</span>', bar
        )[1]
        text = must_search(r'<span class="bar-text[^>]*>([^<]*)</span>', bar)[1]
        self.assertEqual((badge, text), ("Funder", "Delft University"))
        self.assertNotIn("http://example.org/vocab", bar)
        self.assertNotIn("ark:/", bar)


def whom(role: str) -> str:
    """Somebody the role can be for: an organization for those of organizations."""
    return DELFT if role in ("Funder", "Sponsor", "DegreeGrantor") else ADA


class OnlyForTest(ContributorCase):
    LABELS: ClassVar[dict[str, str]] = {
        "ThesisSupervisor": "Thesis supervisor",
        "CoSupervisor": "Co-supervisor",
        "ThesisAdvisor": "Thesis advisor",
        "DegreeGrantor": "Degree grantor",
    }

    def test_the_roles_of_a_thesis_are_for_a_thesis_and_the_other_roles_for_every_work(
        self,
    ):
        for role in THESIS_ROLES:
            with self.subTest(role=role):
                response = self.post_work([(role, whom(role))], "Thesis")
                self.assertEqual(response.status_int, 302, response.text[:300])

        others = [r for r in ROLES if r not in THESIS_ROLES]
        self.assertEqual(
            others,
            [
                "Contributor",
                "ExternalReviewer",
                "Expert",
                "Editor",
                "Funder",
                "Sponsor",
            ],
        )
        for role in others:
            with self.subTest(role=role):
                response = self.post_work([(role, whom(role))], "Book")
                self.assertEqual(response.status_int, 302, response.text[:300])

    def test_a_work_that_is_not_a_thesis_is_refused_them_and_so_is_a_record_by_the_shape(
        self,
    ):
        for role in THESIS_ROLES:
            for kind in ("Book", "ScholarlyArticle", "Dataset"):
                with self.subTest(role=role, kind=kind):
                    response = self.post_work([(role, whom(role))], kind)
                    self.assertEqual(response.status_int, 200)
                    self.assertIn(
                        f"{self.LABELS[role]} is only for Thesis", response.text
                    )

        for role in THESIS_ROLES:
            with self.subTest(role=role):
                self.post_work([(role, whom(role))], "Thesis", name=f"Thesis {role}")
                work = self.work_graph(THESIS_ROLES.index(role) + 1)
                self.assertEqual(work.validate(), [])
                work.graph.replace(
                    work.resource, forms.RDF_TYPE, ox.NamedNode(SCHEMA + "Book")
                )
                problems = " ".join(map(str, work.validate()))
                self.assertIn("are only for a thesis", problems)


PREFIXES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix schema: <https://schema.org/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix dash: <http://datashapes.org/dash#> .
@prefix ex: <http://example.org/shapes#> .
@prefix role: <http://example.org/vocab/role/> .
"""


class ShapeSaysTest(unittest.TestCase):
    """The form reads what follows from a choice out of the shape, which is SHACL (and
    validates the same): there are no terms of its own for it."""

    @staticmethod
    def graph(text: str) -> Graph:
        return Graph.parse(PREFIXES + text, "turtle")

    @staticmethod
    def prop(graph, shape, path):
        return next(
            p
            for found, p in forms.properties(graph, ox.NamedNode(shape))
            if found == path
        )

    ROLE_SHAPE = """
    ex:Role a sh:NodeShape ;
      sh:property [ sh:path schema:roleName ;
                    sh:in ( role:Funder role:Expert role:Editor ) ] ,
                  [ sh:path schema:contributor ; sh:nodeKind sh:IRI ] ;
      sh:or (
        [ sh:property [ sh:path schema:roleName ; sh:in ( role:Expert role:Editor ) ] ;
          sh:property [ sh:path schema:contributor ; sh:node ex:PersonShape ] ]
        [ sh:property [ sh:path schema:roleName ; sh:hasValue role:Funder ] ;
          sh:property [ sh:path schema:contributor ; sh:node ex:OrganizationShape ] ]
      ) .
    """

    def test_the_alternatives_of_a_shape_say_what_a_reference_points_at(self):
        graph = self.graph(self.ROLE_SHAPE)
        prop = self.prop(graph, EX + "Role", SCHEMA + "contributor")
        found = forms.class_follow(graph, ox.NamedNode(EX + "Role"), prop)
        assert found is not None
        follows, by_value = found
        self.assertEqual(follows, SCHEMA + "roleName")
        self.assertEqual(
            {k.removeprefix(ROLE): [s.value for s in v] for k, v in by_value.items()},
            {
                "Expert": [EX + "PersonShape"],
                "Editor": [EX + "PersonShape"],
                "Funder": [EX + "OrganizationShape"],
            },
        )

        graph = self.graph(self.ROLE_SHAPE)
        shape = ox.NamedNode(EX + "Role")
        prop = self.prop(graph, EX + "Role", SCHEMA + "contributor")
        found = forms.reference_shapes(graph, prop, shape)
        self.assertCountEqual(
            [s.value for s in found], [EX + "PersonShape", EX + "OrganizationShape"]
        )
        self.assertEqual(
            forms.reference_shapes(graph, prop), []
        )  # the property says none

        graph = self.graph(self.ROLE_SHAPE)
        prop = self.prop(graph, EX + "Role", SCHEMA + "roleName")
        self.assertIsNone(forms.class_follow(graph, ox.NamedNode(EX + "Role"), prop))

    def test_the_alternatives_of_a_shape_say_for_which_resources_a_value_is(self):
        graph = self.graph(
            """
            ex:Kind a sh:NodeShape ;
              sh:property [ sh:path schema:name ] , [ sh:path schema:url ] ;
              sh:or ( [ sh:property [ sh:path schema:name ; sh:minCount 1 ] ]
                      [ sh:property [ sh:path schema:url ; sh:minCount 1 ] ] ) .
            """
        )
        shape = ox.NamedNode(EX + "Kind")
        prop = self.prop(graph, EX + "Kind", SCHEMA + "url")
        self.assertIsNone(forms.class_follow(graph, shape, prop))
        self.assertEqual(forms.only_for_rules(graph, shape), {})

        rules = forms.only_for_rules(
            self.graph(self.WORK_SHAPE), ox.NamedNode(EX + "Work")
        )
        self.assertEqual(
            rules,
            {
                (SCHEMA + "contributor", SCHEMA + "roleName"): {
                    ROLE + "ThesisAdvisor": [SCHEMA + "Thesis"],
                    ROLE + "Editor": [SCHEMA + "Book", SCHEMA + "Report"],
                    ROLE + "Expert": [SCHEMA + "Book", SCHEMA + "Report"],
                }
            },
        )

    WORK_SHAPE = """
    ex:Work a sh:NodeShape ;
      sh:or ( [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Thesis ] ]
              [ sh:property [ sh:path ( schema:contributor schema:roleName ) ;
                              sh:not [ sh:hasValue role:ThesisAdvisor ] ] ] ) ;
      sh:or ( [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Book ] ]
              [ sh:property [ sh:path rdf:type ; sh:hasValue schema:Report ] ]
              [ sh:property [ sh:path ( schema:contributor schema:roleName ) ;
                              sh:not [ sh:in ( role:Editor role:Expert ) ] ] ] ) .
    """

    def test_the_work_shape_does_not_use_terms_of_its_own_for_any_of_this(self):
        text = (
            Path(forms.__file__).parent / "assets" / "shacl" / "work-schemaorg-01.ttl"
        ).read_text()
        for term in ("refersTo", "classesFollow", "onlyFor"):
            self.assertNotIn(f"gracia:{term}", text)
        self.assertFalse(
            any(
                hasattr(uri, name)
                for name in ("REFERS_TO", "ONLY_FOR", "CLASSES_FOLLOW")
            )
        )


class MenuInARowTest(unittest.TestCase):
    """A menu that opens in a row of an accordion (the role of a contributor, the
    lookup of a reference) goes past the end of the row. The accordion clips what is
    outside it, and the row is isolated (so the fields after it paint over what hangs
    out): only the first rows of the menu could be seen. It is the style that lets the
    menu out while it is open; the browser is what shows it, so this keeps the rules
    from being lost."""

    css = (
        Path(forms.__file__).parent / "assets" / "static" / "css" / "gracia.css"
    ).read_text()

    def rule(self, selector: str) -> str:
        return must_search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)[1]

    def test_a_menu_in_a_row_is_let_out_of_the_accordion_while_it_is_open(self):
        found = self.rule(".blank-nodes > .accordion:has(.dropdown-menu.show)")
        self.assertIn("overflow: visible", found)

        found = self.rule(".blank-node:has(.dropdown-menu.show)")
        self.assertRegex(found, r"z-index:\s*\d+")
        self.assertGreater(
            int(must_search(r"z-index:\s*(\d+)", found)[1]), 6
        )  # the icon of a reference

        self.assertIn("overflow: hidden", self.rule(".blank-nodes > .accordion"))


class DefaultRoleTest(ContributorCase):
    """A new contributor is a Contributor: nothing has to be chosen for it to work."""

    def test_a_new_contributor_is_a_contributor_who_can_be_a_person_or_an_organization(
        self,
    ):
        select = uri_select(self.added_row(), "roleName")
        self.assertEqual(select.chosen, ROLE + "Contributor")
        self.assertEqual(select.selected, 1)
        self.assertIn('<span class="uri-select-label">Contributor</span>', select.html)

        for who in (ADA, DELFT):
            with self.subTest(who=who):
                self.assertEqual(self.post_work([("Contributor", who)]).status_int, 302)
        for work in (1, 2):
            self.assertEqual(self.work_graph(work).validate(), [])

    def test_the_role_that_was_saved_is_shown_again_and_can_be_taken_away(self):
        self.post_work([("Funder", DELFT)])
        page = self.web.get("/refs/work/1/edit", headers=ADMIN).text
        self.assertEqual(
            uri_select(page, "roleName").chosen, ROLE + "Funder"
        )  # not the default

        response = self.post_work([("", ADA)])
        self.assertEqual(response.status_int, 200)
        select = uri_select(response.text, "roleName")
        self.assertEqual(select.chosen, "")  # what was posted, not the default again
        self.assertIn("Required", response.text)


class ErrorMessageTest(ContributorCase):
    """The message of a field that is wrong is shown, whatever the widget: the choice
    of the class, the role, the reference."""

    def failed(self):
        """A form that is wrong in the type, the role and the contributor."""
        params = []
        for field, value in self.fields(WORK, [ADA], "A work"):
            if (field, value) == ("__end__", "resource:mapping"):
                params += [
                    ("__start__", "contributor:sequence"),
                    ("__start__", "item:mapping"),
                    ("roleName", ""),
                    ("contributor", ""),
                    ("__end__", "item:mapping"),
                    ("__end__", "contributor:sequence"),
                ]
            params.append((field, "" if field == "type" else value))
        return self.web.post(WORK, params=params, headers=POSTING, expect_errors=True)

    def test_the_message_of_a_wrong_field_is_shown_whatever_the_widget(self):
        page = self.failed().text
        for label in ("type", "role", "contributor"):
            with self.subTest(label=label):
                item = must_search(
                    rf'(?s)<div\s+class="mb-3[^"]*"[^>]*>\s*<label[^>]*>{label}</label>.*?</p>',
                    page,
                )[0]
                self.assertRegex(item, r'<p class="invalid-feedback"[^>]*>\s*Required')
                self.assertIn("is-invalid", item)

        item = must_search(
            r"(?s)<label[^>]*>role</label>(?:\s*<!--.*?-->)?\s*"
            r'<div class="uri-select dropdown">.*?</ul>\s*</div>\s*'
            r'<p[^>]*class="invalid-feedback"',
            self.failed().text,
        )
        self.assertTrue(item)

        rule = must_search(
            r"([^{}]*:has\(\.is-invalid\) ~ p\.invalid-feedback)\s*\{([^}]*)\}",
            self.css(),
        )
        for wrapper in (".uri-select", ".reference", ".date-input"):
            self.assertIn(wrapper, rule[1])
        self.assertIn("display: block", rule[2])

    def css(self):
        return (
            Path(forms.__file__).parent / "assets" / "static" / "css" / "gracia.css"
        ).read_text()


class NodeErrorTest(ContributorCase):
    """A role that the type of the work does not allow is an error in the node that
    has it, at the role: the node is marked and opened, and the message is there."""

    def failing(self, rows, kind="ScholarlyArticle"):
        response = self.post_work(rows, kind)
        self.assertEqual(response.status_int, 200)
        return response.text

    @staticmethod
    def nodes(page):
        """The nodes of the contributors, each as its html."""
        mid = page.split('id="nodes-contributor"')[1].split(
            'value="contributor:sequence"'
        )[1]
        return re.split(r'(?=<div\s+class="accordion-item blank-node")', mid)[1:]

    def test_the_node_that_has_the_role_is_marked_and_open_with_the_message_at_the_role(
        self,
    ):
        rows = self.nodes(self.failing([("ThesisSupervisor", ADA)]))
        self.assertEqual(len(rows), 1)
        self.assertIn('class="badge text-bg-danger me-3">errors</span>', rows[0])
        self.assertRegex(rows[0], r"<details\s[^>]*\bopen[\s>]")

        row = self.nodes(self.failing([("ThesisSupervisor", ADA)]))[0]
        found = must_search(
            r'(?s)<label[^>]*>role</label>.*?<p[^>]*class="invalid-feedback"[^>]*>'
            r"\s*([^<]*?)\s*</p>",
            row,
        )
        self.assertEqual(found[1], "Thesis supervisor is only for Thesis")
        toggle = must_search(r"<button[^>]*uri-select-toggle[^>]*>", row)[0]
        self.assertIn("is-invalid", toggle)

    def test_only_the_nodes_that_are_wrong_have_the_error_and_the_list_counts_them(
        self,
    ):
        rows = self.nodes(
            self.failing(
                [
                    ("Funder", DELFT),
                    ("CoSupervisor", ADA),
                    ("Expert", ADA),
                    ("DegreeGrantor", DELFT),
                ]
            )
        )
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            ["errors</span>" in row for row in rows], [False, True, False, True]
        )
        said = [re.findall(r"is only for ([A-Za-z]+)", row) for row in rows]
        self.assertEqual(said, [[], ["Thesis"], [], ["Thesis"]])

        page = self.failing([("Funder", DELFT), ("CoSupervisor", ADA)])
        self.assertIn("<strong>1</strong> of 2 with errors", page)

    def test_the_error_is_that_of_the_list_of_the_node_of_the_role(self):
        self.post_work([("Funder", DELFT)], "Thesis")
        content = self.work_graph().edit_schema()["resource"]
        value = {
            "type": SCHEMA + "Book",
            "contributor": [
                {"roleName": ROLE + "Funder", "contributor": DELFT},
                {"roleName": ROLE + "ThesisAdvisor", "contributor": ADA},
            ],
        }
        with self.assertRaises(colander.Invalid) as caught:
            content.validator(content, value)
        (listed,) = caught.exception.children  # one list is wrong
        self.assertEqual(listed.pos, content.children.index(content["contributor"]))
        (row,) = listed.children  # one node in it: the second
        self.assertEqual(row.pos, 1)
        self.assertEqual(
            (row.children[0].node.name, row.children[0].msg),
            ("roleName", "Thesis advisor is only for Thesis"),
        )

        self.assertEqual(
            self.post_work([("ThesisAdvisor", ADA)], "Thesis").status_int, 302
        )


class DialogOnDialogTest(unittest.TestCase):
    """A form in a dialog that opens a dialog of its own (a work, its journal, the
    publisher): the backdrop of the one on top covers its own page, the iframe, and not
    the header of the dialog it is in. The browser is what shows that, so this keeps the
    pieces from being lost: the dialog on top tells the page around it, which dims the
    header of its dialog."""

    static = Path(forms.__file__).parent / "assets" / "static"

    def script(self):
        return (self.static / "scripts" / "reference.js").read_text()

    def css(self):
        return (self.static / "css" / "gracia.css").read_text()

    def test_a_dialog_in_a_form_says_so_and_the_page_around_it_dims_the_header(self):
        script = self.script()
        self.assertIn('postMessage({ gracia: "dialog", open }', script)
        self.assertRegex(script, r"tellParent\(true\)")
        self.assertRegex(script, r"tellParent\(false\)")

        script = self.script()
        self.assertIn('said.gracia === "dialog"', script)
        self.assertIn('classList.toggle("covered"', script)
        self.assertIn(
            'classList.remove("covered")', script
        )  # when it closes, or the form is new

    def test_the_styles_of_the_dialogs_and_the_bar_of_a_node_with_an_error(self):
        css = self.css()
        header = must_search(
            r"\.modal\.covered \.modal-header::after\s*\{([^}]*)\}", css
        )[1]
        self.assertIn("rgba(255, 255, 255", header)
        self.assertIn("backdrop-filter: blur(", header)

        found = must_search(r"\n\.modal-backdrop\s*\{([^}]*)\}", self.css())[1]
        self.assertIn("--bs-backdrop-bg: rgba(255, 255, 255", found)
        self.assertIn("backdrop-filter: blur(", found)

        css = self.css()
        found = must_search(r"\n\.modal\s*\{([^}]*)\}", css)[1]
        self.assertRegex(
            found, r"--bs-modal-box-shadow:\s*0 1rem 3rem rgba\(0, 0, 0, \.25\)"
        )
        # Bootstrap names the shadow but does not put it on the dialog: this does
        applied = must_search(r"\n\.modal-content\s*\{([^}]*)\}", css)[1]
        self.assertIn("box-shadow: var(--bs-modal-box-shadow)", applied)

        css = self.css()
        found = must_search(
            r"\.blank-node:has\(> details > summary \.badge\.text-bg-danger\)"
            r" > details > summary\s*\{([^}]*)\}",
            css,
        )[1]
        self.assertIn("var(--bs-danger)", found)
