import unittest

import duckdb
import pyoxigraph as ox
from support import must_search
from test_web import HeaderPolicy, WebCase

from gracia import types, uri
from gracia.main import make_config
from gracia.predicates import HookContext, gracia_label

ADMIN = {"X-User": "admin"}
POSTING = {**ADMIN, "Origin": "http://localhost:80"}


class LabelCase(WebCase):
    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()  # the registry, for code that runs outside a request
        self.addCleanup(self.config.end)
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse battery"),
            content_types=[types.available()["person"]],
        )
        self.web = self.client(self.config)

    def add_person(self, name="Ada Lovelace"):
        form = self.web.get("/test_repo/person/add", headers=ADMIN).forms[0]
        form["name"] = name
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 302, response.text[:200])
        return response

    def labels(self, record):
        with self.application.new_store("test_repo") as store:
            data = store.get(record)
        return [
            o.value for o in data.objects(ox.NamedNode(record), ox.NamedNode(uri.LABEL))
        ]

    def edit_type(self, **fields):
        form = self.web.get("/test_repo/person/manage", headers=ADMIN).forms[
            "form-type-settings"
        ]
        for name, text in fields.items():
            form[name] = text
        return form.submit("save", headers=POSTING)


class LabelTest(LabelCase):
    def test_the_label_is_copied_from_the_content_by_the_label_role_of_the_shape(self):
        self.add_person("Ada Lovelace")
        self.assertEqual(
            self.labels("rec:person:1"), ["Ada Lovelace"]
        )  # one, on the record

    def test_the_label_follows_the_content(self):
        self.add_person("Ada Lovelace")
        form = self.web.get("/test_repo/person/1/edit", headers=ADMIN).forms[0]
        form["name"] = "Ada King"
        form.submit("save", headers=POSTING)
        self.assertEqual(self.labels("rec:person:1"), ["Ada King"])

    def test_the_title_of_the_edit_form_is_the_label(self):
        self.add_person("Ada Lovelace")
        page = self.web.get("/test_repo/person/1/edit", headers=ADMIN).text
        self.assertRegex(page, r'<h1 class="h3 mb-0">\s*Ada Lovelace\s*</h1>')
        self.assertIn("rec:person:1", page)  # the key stays as the subtitle

    def test_the_label_is_a_read_only_field_that_is_shown_and_not_taken_from_the_form(
        self,
    ):
        self.add_person("Ada Lovelace")
        page = self.web.get("/test_repo/person/1/edit", headers=ADMIN).text
        field = must_search(r'<input[^>]*name="label"[^>]*>', page)[0]
        self.assertIn("readonly", field)
        self.assertIn('value="Ada Lovelace"', field)
        form = self.web.get("/test_repo/person/1/edit", headers=ADMIN).forms[0]
        form.fields["label"][0].force_value("Somebody else")  # posted by hand
        form.submit("save", headers=POSTING)
        self.assertEqual(
            self.labels("rec:person:1"), ["Ada Lovelace"]
        )  # made from the content

    def test_the_json_of_a_record_has_its_label(self):
        self.add_person("Ada Lovelace")
        body = self.web.get(
            "/test_repo/person/1", headers={**ADMIN, "Accept": "application/json"}
        ).json
        self.assertEqual(body["record"][uri.LABEL], ["Ada Lovelace"])

    def test_a_record_without_a_label_is_called_unnamed(self):
        with self.application.new_store("test_repo") as store:
            text = (
                b"<rec:person:7> a <https://gracia.pub/ns/Record> ; "
                b"<https://gracia.pub/ns/type> <rec:type:5> ."
            )
            store.put(ox.Dataset(ox.parse(text, format=ox.RdfFormat.TURTLE)))
        self.assertIn(
            "(Unnamed)", self.web.get("/test_repo/person/7/edit", headers=ADMIN).text
        )


class EveryTypeTest(LabelCase):
    """Each type has a query, so each kind of record has a label."""

    def test_the_system_types_say_how_their_records_are_labelled(self):
        self.add_person("Ada Lovelace")
        self.assertEqual(self.labels("rec:repository:1"), ["test_repo"])  # the title
        self.assertEqual(self.labels("rec:user:1"), ["admin"])  # the user id
        self.assertEqual(
            [self.labels(f"rec:group:{n}")[0] for n in (1, 2, 3)],
            ["admin", "editor", "manager"],
        )
        self.assertEqual(
            [self.labels(f"rec:type:{n}")[0] for n in (1, 2, 3, 4, 5)],
            ["Repository", "Type", "User", "UserGroup", "Person"],  # their names
        )

    def test_editing_a_user_a_group_and_a_type_keeps_their_labels_right(self):
        form = self.web.get("/test_repo/group/2/edit", headers=ADMIN).forms[0]
        form["name"] = "editors"
        form.submit("save", headers=POSTING)
        self.assertEqual(self.labels("rec:group:2"), ["editors"])
        form = self.web.get("/test_repo/user/1/edit", headers=ADMIN).forms[0]
        form["userid"] = "root"
        form["password"] = ""
        form.submit("save", headers=POSTING)
        self.assertEqual(self.labels("rec:user:1"), ["root"])

    def test_the_label_of_the_repository_follows_its_title(self):
        form = self.web.get("/test_repo/manage", headers=ADMIN).forms[0]
        form["title"] = "The Library"
        self.assertEqual(form.submit("save", headers=POSTING).status_int, 302)
        self.assertEqual(self.labels("rec:repository:1"), ["The Library"])


class NameTest(LabelCase):
    """`foaf:name` of a person: a name, or both parts it is made of (and then it is made
    of them)."""

    def submit(self, **fields):
        form = self.web.get("/test_repo/person/add", headers=ADMIN).forms[0]
        for name, text in fields.items():
            form[name] = text
        return form.submit("save", headers=POSTING)

    def test_an_empty_name_follows_from_the_two_parts_and_so_does_the_label(self):
        self.assertEqual(
            self.submit(name="", givenName="Ada", familyName="Lovelace").status_int, 302
        )
        self.assertEqual(self.labels("rec:person:1"), ["Ada Lovelace"])

    def test_a_name_that_is_there_is_kept(self):
        self.submit(name="Countess of Lovelace", givenName="Ada", familyName="Lovelace")
        self.assertEqual(self.labels("rec:person:1"), ["Countess of Lovelace"])
        self.submit(name="Ada")  # a name alone is fine
        self.assertEqual(self.labels("rec:person:2"), ["Ada"])

    def test_nothing_or_one_part_is_refused_at_the_name_and_nothing_is_stored(self):
        for fields in ({}, {"givenName": "Ada"}, {"familyName": "Lovelace"}):
            with self.subTest(fields=fields):
                response = self.submit(name="", **fields)
                self.assertEqual(
                    response.status_int, 200
                )  # the form again, with the error
                self.assertIn(
                    "Give a name, or both a given name and a family name", response.text
                )
        self.assertEqual(self.labels("rec:person:1"), [])  # no record was made

    def test_a_record_checked_against_the_shape_says_it_in_the_words_of_the_shape(self):
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        record = node["person"].blank_record()
        record.begin(1)
        foaf = "http://xmlns.com/foaf/0.1/"
        record.add(record.resource, foaf + "givenName", ox.Literal("Ada"))
        problems = record.shape_problems()
        self.assertIn("Give a name, or both a given name and a family name", problems)
        record.add(record.resource, foaf + "familyName", ox.Literal("Lovelace"))
        self.assertNotIn(
            "Give a name, or both a given name and a family name",
            record.shape_problems(),
        )


class LabelSqlTest(unittest.TestCase):
    """The SQL of the label predicate, on the rows of a batch (`flat`)."""

    NAME, TITLE = "http://xmlns.com/foaf/0.1/name", "http://purl.org/dc/terms/title"

    class Type:
        slug = "person"

        def __init__(self, paths):
            self.paths = paths

        def role_paths(self, role):
            return self.paths if role == "label" else []

    def labels(self, paths, rows) -> dict[str, str]:
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE flat (s VARCHAR, loc VARCHAR[], p VARCHAR, o VARCHAR, lit "
            "VARCHAR, "
            "lang VARCHAR, dt VARCHAR)"
        )
        for s, loc, p, lit in rows:
            con.execute(
                "INSERT INTO flat VALUES (?, ?, ?, NULL, ?, NULL, NULL)",
                [s, loc, p, lit],
            )
        context = HookContext(None, self.Type(paths), uri.LABEL, flat="flat")
        for statement in gracia_label.LabelPredicate().on_write_sql(context):
            con.execute(statement)
        found = con.execute(
            "SELECT s, lit FROM flat WHERE p = ? AND len(loc) = 0", [uri.LABEL]
        ).fetchall()
        return dict(found)

    def test_the_values_in_the_order_of_the_roles_joined_by_a_space(self):
        rows = [
            ("rec:person:1", ["g:resource", "_:1"], self.NAME, "Lovelace"),
            ("rec:person:1", ["g:resource", "_:1"], self.TITLE, "Countess"),
        ]
        self.assertEqual(
            self.labels([self.TITLE, self.NAME], rows),
            {"rec:person:1": "Countess Lovelace"},
        )
        self.assertEqual(
            self.labels([self.NAME, self.TITLE], rows),
            {"rec:person:1": "Lovelace Countess"},
        )

    def test_of_several_values_the_shallowest_one_wins_and_a_label_is_made_again(self):
        rows = [
            (
                "rec:person:1",
                ["g:resource", "_:1", "x:knows", "_:2"],
                self.NAME,
                "Deep",
            ),
            ("rec:person:1", ["g:resource", "_:1"], self.NAME, "Near"),
            ("rec:person:1", [], uri.LABEL, "Old label"),
        ]
        self.assertEqual(self.labels([self.NAME], rows), {"rec:person:1": "Near"})

    def test_no_value_or_no_role_is_no_label_and_other_types_are_left_alone(self):
        rows = [
            ("rec:person:1", [], uri.LABEL, "Old"),
            ("rec:book:1", [], uri.LABEL, "Kept"),
            ("rec:book:1", [], self.NAME, "Book name"),
        ]
        self.assertEqual(self.labels([self.NAME], rows), {"rec:book:1": "Kept"})
        self.assertEqual(self.labels([], rows), {"rec:book:1": "Kept"})


class EditorLoadingTest(LabelCase):
    """CodeMirror is needed only by the pages with an editor field, never by public
    pages."""

    def has_editor(self, path, headers=ADMIN):
        return "codemirror" in self.web.get(path, headers=headers).text

    def test_the_extensions_tab_loads_the_editor(self):
        page = self.web.get("/test_repo/manage", headers=ADMIN).text
        self.assertIn("codemirror", page)
        self.assertIn('data-editor="json"', page)

    def test_other_pages_do_not(self):
        self.add_person()
        self.assertFalse(self.has_editor("/test_repo/person/1/edit"))
        self.assertFalse(self.has_editor("/test_repo/person/add"))
        self.assertFalse(self.has_editor("/test_repo", headers={}))


class CodeWidgetTest(LabelCase):
    def test_a_code_widget_names_its_mode_and_loads_the_editor(self):
        import colander
        import deform
        from fanstatic import get_needed, init_needed

        from gracia import static
        from gracia.forms import RENDERER, CodeWidget

        for mode in ("sparql", "json", "xml"):
            init_needed()
            field = deform.Form(
                colander.SchemaNode(
                    colander.Mapping(),
                    colander.SchemaNode(
                        colander.String(), name="q", widget=CodeWidget(mode)
                    ),
                ),
                renderer=RENDERER,
            )
            html = field.render({"q": "x"})
            self.assertIn(f'data-editor="{mode}"', html)
            self.assertIn(static.editor_js, get_needed().resources())

    def test_xml_mode_is_shipped(self):
        from gracia import static

        self.assertIn(static.codemirror_xml_js, static.editor_js.depends)  # ty: ignore[invalid-argument-type]
