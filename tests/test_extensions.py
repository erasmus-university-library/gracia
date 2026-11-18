"""Extensions: what an extension decides, what is stored for it, what is below a repository because
the registry says so, and the Extensions tab."""

import datetime
import functools
import json
import re

import colander
from base import DbTestCase
from extension_support import put_extension_state
from pyramid.exceptions import ConfigurationError
from pyramid.response import Response
from test_web import HeaderPolicy, WebCase

from gracia import extensions, uri
from gracia.extensions import Extension, ExtensionError
from gracia.forms import to_json
from gracia.main import make_config
from gracia.resources import Repository

ADMIN = {"X-User": "admin"}
POSTING = {**ADMIN, "Origin": "http://localhost:80"}  # a post has to come from this site


class DemoSettings(colander.MappingSchema):
    schema_type = functools.partial(colander.Mapping, unknown="raise")

    greeting = colander.SchemaNode(
        colander.String(), title="Greeting", default="hello", missing="hello"
    )
    count = colander.SchemaNode(
        colander.Integer(),
        title="Count",
        default=3,
        missing=3,
        validator=colander.Range(min=1),
    )
    since = colander.SchemaNode(
        colander.Date(),
        title="Since",
        default=datetime.date(2020, 1, 1),
        missing=datetime.date(2020, 1, 1),
    )


class Demo(Extension):
    title = "Demo"
    description = "An extension for the tests."
    settings_schema = DemoSettings


class OnlyWithPerson(Extension):
    """On only when the repository has the type `person`: the stored flag is not what decides."""

    title = "Only with person"
    description = "Depends on the repository."

    @property
    def enabled(self):
        return "person" in self.repository.res_types

    @property
    def can_disable(self):
        return False


class Child:
    def __init__(self, repository, name):
        self.__parent__, self.__name__ = repository, name


class ExtensionsCase(WebCase):
    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = HeaderPolicy()
        self.application.create_repository("library")
        self.config = make_config(self.settings)
        self.config.add_gracia_extension("demo", Demo)
        self.config.add_gracia_extension("depends", OnlyWithPerson)
        self.config.add_view(
            lambda request: Response("the demo"),
            context=Repository,
            name="demo",
            enabled_extension="demo",
        )
        self.config.add_gracia_repository_child("hello:", Child, prefix=True)
        self.config.add_gracia_repository_child("hello:long", Child, prefix=True)
        self.config.add_gracia_repository_child("nope:", self.nothing, prefix=True)
        self.config.commit()
        self.web = self.client(self.config)

    @staticmethod
    def nothing(repository, name):
        raise KeyError(name)  # a factory that finds nothing: the segment is not there

    def repo(self):
        """The repository node as the next request has it: nothing cached."""
        self.config.begin()
        self.addCleanup(self.config.end)
        node = self.application["library"]
        self.addCleanup(node.close)
        return node

    def ext(self, name):
        return self.repo().extension(name)

    def stored(self):
        with self.application.open_repository("library") as store:
            return extensions.read_extension_states(
                store.get(uri.repository_uri()), uri.repository_uri()
            )


class ExtensionTest(ExtensionsCase):
    def test_every_extension_there_is_is_known_by_title(self):
        self.assertEqual(
            [e.name for e in self.repo().extensions()], ["ark", "demo", "ns_prefix", "depends", "search"]
        )
        self.assertEqual(self.repo().extension("ark").title, "ARK")

    def test_an_unknown_name_is_a_key_error(self):
        with self.assertRaises(KeyError):
            self.repo().extension("nobody")

    def test_the_extension_decides_not_the_flag(self):
        repo = self.repo()
        self.assertTrue(repo.extension("ark").enabled)  # always
        self.assertFalse(repo.extension("demo").enabled)  # off until it is enabled
        self.assertFalse(repo.extension("depends").enabled)  # the repository has no person type

    def test_one_object_per_name_for_a_request(self):
        repo = self.repo()
        self.assertIs(repo.extension("demo"), repo.extension("demo"))
        self.assertIs(repo.extension("demo").repository, repo)

    def test_enabled_is_read_only(self):
        with self.assertRaises(AttributeError):
            self.repo().extension("demo").enabled = True

    def test_an_extension_that_cannot_be_disabled_stays_on(self):
        ark = self.repo().extension("ark")
        self.assertFalse(ark.can_disable)
        with self.assertRaisesRegex(ExtensionError, "cannot be disabled"):
            ark.disable()
        ark.enable()  # nothing to do
        self.assertNotIn("ark", {k for k, v in self.stored().items() if v[0] is False})

    def test_enabling_and_disabling_is_stored_and_the_settings_are_kept(self):
        self.ext("demo").enable()
        self.assertTrue(self.ext("demo").enabled)
        self.assertEqual(self.stored()["demo"][0], True)
        self.ext("demo").save_settings(
            {"greeting": "hi", "count": 5, "since": datetime.date(2021, 2, 3)}
        )
        self.ext("demo").disable()
        demo = self.ext("demo")
        self.assertFalse(demo.enabled)
        self.assertEqual(demo.settings["greeting"], "hi")  # kept
        demo.enable()
        self.assertEqual(self.ext("demo").settings["count"], 5)

    def test_the_settings_are_the_defaults_until_said_and_python_values(self):
        self.assertEqual(
            self.ext("demo").settings,
            {"greeting": "hello", "count": 3, "since": datetime.date(2020, 1, 1)},
        )

    def test_the_settings_dict_is_a_copy(self):
        demo = self.ext("demo")
        demo.settings["greeting"] = "changed"
        self.assertEqual(demo.settings["greeting"], "hello")

    def test_a_date_is_stored_as_text_and_comes_back_as_a_date(self):
        self.ext("demo").save_settings(
            {"greeting": "hi", "count": 5, "since": datetime.date(2021, 2, 3)}
        )
        self.assertEqual(
            self.stored()["demo"][1], {"greeting": "hi", "count": "5", "since": "2021-02-03"}
        )
        back = self.ext("demo").settings
        self.assertEqual(back["since"], datetime.date(2021, 2, 3))
        self.assertEqual(back["count"], 5)
        self.assertEqual(json.loads(self.ext("demo").export())["since"], "2021-02-03")

    def test_the_settings_are_stored_as_json_with_the_json_datatype(self):
        self.ext("demo").save_settings(
            {"greeting": "hi", "count": 5, "since": datetime.date(2021, 2, 3)}
        )
        with self.application.open_repository("library") as store:
            data = store.get(uri.repository_uri())
        literals = [q.object for q in data if q.predicate.value == uri.EXTENSION_SETTINGS]
        demo = [lit for lit in literals if '"greeting"' in lit.value]
        self.assertEqual(len(demo), 1)
        self.assertEqual(demo[0].datatype.value, uri.RDF_JSON)

    def put_state(self, name="demo", settings=None, enabled=None, turtle=None):
        put_extension_state(self.application, name, settings, enabled, turtle)

    def test_a_setting_that_is_not_there_is_its_default_and_nothing_older_breaks(self):
        # what an older version stored: a key the schema no longer has, a value it no longer takes,
        # and nothing of the newer settings
        self.put_state("demo", {"colour": "red", "count": "not a number"}, True)
        self.assertEqual(
            self.ext("demo").settings,
            {"greeting": "hello", "count": 3, "since": datetime.date(2020, 1, 1)},
        )

    def test_text_that_is_not_json_counts_as_no_settings(self):
        self.put_state(
            turtle="@prefix gracia: <https://gracia.pub/ns/> .\n"
            '<urn:gracia:content> gracia:extension [ gracia:extension_name "demo" ; '
            'gracia:extension_settings "{broken" ] .\n'
        )
        self.assertEqual(self.ext("demo").settings["greeting"], "hello")

    def test_settings_that_are_not_valid_are_refused_and_not_stored(self):
        demo = self.ext("demo")
        with self.assertRaises(ExtensionError):
            demo.import_json("not json")
        with self.assertRaisesRegex(ExtensionError, "JSON object"):
            demo.import_json("[1]")
        with self.assertRaisesRegex(ExtensionError, "count"):
            demo.import_json('{"count": "0"}')
        with self.assertRaisesRegex(ExtensionError, "colour"):  # a key the schema does not know
            demo.import_json('{"colour": "red"}')
        with self.assertRaisesRegex(ExtensionError, "count"):
            demo.save_settings({"greeting": "x", "count": 0, "since": datetime.date(2020, 1, 1)})
        self.assertNotIn("demo", self.stored())

    def test_json_may_leave_a_setting_out_and_it_is_the_default(self):
        self.ext("demo").import_json('{"greeting": "hi"}')
        self.assertEqual(self.ext("demo").settings["count"], 3)

    def test_an_extension_without_settings_says_so(self):
        with self.assertRaisesRegex(ExtensionError, "has no settings"):
            self.ext("depends").import_json("{}")
        self.assertEqual(self.ext("depends").settings, {})

    def test_the_ark_settings_are_the_ones_the_repository_was_made_with(self):
        self.assertEqual(self.ext("ark").settings, {"naan": "99999", "root_prefix": "ir"})
        self.assertEqual(self.stored()["ark"][1], {"naan": "99999", "root_prefix": "ir"})

    def test_for_now_only_99999_and_ir_are_taken_for_the_ark_settings(self):
        with self.assertRaisesRegex(ExtensionError, "Only 99999 for now"):
            self.ext("ark").save_settings({"naan": "12345", "root_prefix": "ir"})
        with self.assertRaisesRegex(ExtensionError, "Only ir for now"):
            self.ext("ark").import_json('{"naan": "99999", "root_prefix": "lib"}')
        self.ext("ark").import_json('{"naan": "99999", "root_prefix": "ir"}')
        self.ext("ark").import_json("{}")  # left out: the defaults, which are the same
        self.assertEqual(self.ext("ark").settings, {"naan": "99999", "root_prefix": "ir"})


class RegistryTest(ExtensionsCase):
    def test_what_is_below_a_repository_comes_from_the_registry_by_prefix(self):
        for name in ("jobs", "deform"):  # the core registers its own children the same way
            self.assertEqual(
                self.web.get(f"/library/{name}", headers=ADMIN, expect_errors=True).status_int
                in (200, 404),
                True,
            )
        self.config.begin()
        self.addCleanup(self.config.end)
        node = self.application["library"]
        self.addCleanup(node.close)
        self.assertEqual(node["hello:there"].__name__, "hello:there")
        self.assertEqual(node["hello:longer"].__name__, "hello:longer")  # the longest prefix
        with self.assertRaises(KeyError):
            node["nope:anything"]
        with self.assertRaises(KeyError):
            node["nothing"]
        self.assertEqual(node["person"].slug if "person" in node.res_types else "x", "x")

    def test_a_key_that_a_type_could_have_is_refused(self):
        config = make_config(self.settings)
        with self.assertRaises(ConfigurationError):
            config.add_gracia_repository_child("person", lambda r, n: None)
        config = make_config(self.settings)
        config.add_gracia_repository_child("ark:", lambda r, n: None)  # a colon: no type has it
        config.add_gracia_repository_child("jobs", lambda r, n: None)  # reserved on purpose

    def test_a_view_of_an_extension_is_there_only_where_it_is_on(self):
        self.assertEqual(
            self.web.get("/library/demo", headers=ADMIN, expect_errors=True).status_int, 404
        )
        self.ext("demo").enable()
        response = self.web.get("/library/demo", headers=ADMIN)
        self.assertEqual(response.text, "the demo")
        self.ext("demo").disable()
        self.assertEqual(
            self.web.get("/library/demo", headers=ADMIN, expect_errors=True).status_int, 404
        )


class TabTest(ExtensionsCase):
    URL = "/library/manage?tab=extensions"

    def page(self, url=None, **kw):
        return self.web.get(url or self.URL, headers=ADMIN, **kw).text

    def post(self, name, button, fields=None, **kw):
        return self.web.post(
            self.URL,
            {"__formid__": f"form-extension-{name}", button: button, **(fields or {})},
            headers=POSTING,
            expect_errors=True,
            **kw,
        )

    def card(self, page, name):
        return re.search(
            rf'<div class="extension[^"]*" id="extension-{name}">.*?(?=<div class="extension|<p class="text-muted"|\Z)',
            page,
            re.S,
        )[0]

    def test_there_is_a_card_for_each_extension_and_the_tab_is_after_settings(self):
        page = self.page()
        tabs = re.findall(r'data-tab="([a-z]+)"', page)
        self.assertEqual(tabs, ["types", "settings", "extensions", "search", "predicates", "stats", "jobs"])
        for name, title in (("ark", "ARK"), ("demo", "Demo"), ("depends", "Only with person")):
            self.assertIn(f'id="extension-{name}"', page)
            self.assertIn(title, self.card(page, name))

    def test_ark_cannot_be_turned_off_and_its_form_is_a_normal_form(self):
        card = self.card(self.page(), "ark")
        self.assertIn("enabled", card)
        button = re.search(r'<button[^>]*name="disable"[^>]*>', card)[0]  # there, and greyed out
        self.assertIn('disabled="disabled"', button)
        self.assertIn("cannot be disabled", button)
        self.assertNotIn('name="enable"', card)
        self.assertIn(">Settings</button>", card)
        update = re.search(r'<button[^>]*name="update"[^>]*>', card)[0]
        self.assertNotIn("disabled=", update)
        self.assertIn('name="import_json"', card)
        for name, value in (("naan", "99999"), ("root_prefix", "ir")):
            field = re.search(rf'<input[^>]*name="{name}"[^>]*>', card)[0]
            self.assertIn(f'value="{value}"', field)
            self.assertNotIn("readonly", field)
        refused = self.post("ark", "disable")  # refused, and said so in the dialog's tab
        self.assertEqual(refused.status_int, 200)
        self.assertIn("cannot be disabled", refused.text)

    def test_what_is_typed_for_the_ark_settings_is_refused_unless_it_is_99999_and_ir(self):
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-ark"]
        form["naan"], form["root_prefix"] = "12345", "lib"
        response = form.submit("update", headers=POSTING)
        self.assertEqual(response.status_int, 200)
        card = self.card(response.text, "ark")
        self.assertIn('data-open="true"', card)
        self.assertIn("Only 99999 for now", card)
        self.assertIn("Only ir for now", card)
        self.assertEqual(self.ext("ark").settings, {"naan": "99999", "root_prefix": "ir"})
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-ark"]
        self.assertEqual(form.submit("update", headers=POSTING).status_int, 302)  # as it is: fine

    def test_a_disabled_extension_has_an_enable_button_and_no_settings(self):
        card = self.card(self.page(), "demo")
        self.assertIn("disabled", card)
        button = re.search(r'<button[^>]*name="enable"[^>]*>', card)[0]
        self.assertNotIn("disabled=", button)  # it can be enabled
        self.assertNotIn('name="disable"', card)
        self.assertNotIn(">Settings</button>", card)

    def test_enabling_and_disabling(self):
        response = self.post("demo", "enable")
        self.assertEqual(response.status_int, 302)
        self.assertEqual(response.location, "http://localhost/library/manage?tab=extensions")
        page = response.follow(headers=ADMIN).text
        self.assertIn("Demo is enabled", page)
        card = self.card(page, "demo")
        button = re.search(r'<button[^>]*name="disable"[^>]*>', card)[0]
        self.assertNotIn("disabled=", button)  # it can be disabled
        self.assertNotIn('name="enable"', card)
        self.assertIn(">Settings</button>", card)
        self.assertIn('data-confirm="Disable Demo?', card)
        self.assertEqual(self.post("demo", "disable").status_int, 302)
        self.assertIn("disabled", self.card(self.page(), "demo"))

    def test_the_dialog_has_the_form_and_the_json(self):
        self.ext("demo").enable()
        card = self.card(self.page(), "demo")
        self.assertIn('id="form-extension-demo"', card)
        self.assertIn('name="update"', card)
        self.assertRegex(card, r">\s*Update settings\s*<")
        self.assertIn(">Import / Export<", card)
        text = re.search(r'<textarea[^>]*name="json"[^>]*>(.*?)</textarea>', card, re.S)[1]
        self.assertEqual(
            json.loads(text.replace("&quot;", '"')),
            {"greeting": "hello", "count": "3", "since": "2020-01-01"},
        )
        self.assertIn('name="import_json"', card)
        self.assertNotIn('data-open="true"', card)

    def test_updating_the_settings_with_the_form(self):
        self.ext("demo").enable()
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-demo"]
        form["greeting"], form["count"], form["date"] = (
            "hi",
            "7",
            "2022-05-06",
        )  # Deform's date widget
        response = form.submit("update", headers=POSTING)
        self.assertEqual(response.status_int, 302)
        self.assertIn("The settings of Demo are updated", response.follow(headers=ADMIN).text)
        self.assertEqual(
            self.ext("demo").settings,
            {"greeting": "hi", "count": 7, "since": datetime.date(2022, 5, 6)},
        )

    def test_a_form_that_is_not_right_comes_back_with_its_dialog_open(self):
        self.ext("demo").enable()
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-demo"]
        form["count"] = "0"
        response = form.submit("update", headers=POSTING)
        self.assertEqual(response.status_int, 200)
        card = self.card(response.text, "demo")
        self.assertIn('data-open="true"', card)
        self.assertIn("0 is less than minimum value 1", card)
        # the form that comes back is the form: it has its button and posts to the same address
        self.assertRegex(card, r'name="update"[^>]*>\s*Update settings')
        self.assertIn('action="http://localhost/library/manage?tab=extensions"', card)
        self.assertEqual(self.ext("demo").settings["count"], 3)  # nothing stored

    def test_the_settings_as_json_can_be_pasted(self):
        self.ext("demo").enable()
        response = self.post(
            "demo", "import_json", {"json": '{"greeting": "from json", "count": "9"}'}
        )
        self.assertEqual(response.status_int, 302)
        self.assertEqual(self.ext("demo").settings["greeting"], "from json")
        self.assertEqual(self.ext("demo").settings["count"], 9)

    def test_json_that_is_wrong_comes_back_in_the_json_tab(self):
        self.ext("demo").enable()
        response = self.post("demo", "import_json", {"json": "{broken"})
        self.assertEqual(response.status_int, 200)
        card = self.card(response.text, "demo")
        self.assertIn('data-open="true"', card)
        self.assertIn("this is not JSON", card)
        self.assertIn('id="ext-demo-json" role="tabpanel"', card)
        self.assertRegex(card, r'class="tab-pane active"\s+id="ext-demo-json"')
        self.assertIn("{broken", card)  # what was pasted is still there

    def test_a_post_from_another_site_is_refused(self):
        response = self.web.post(
            self.URL,
            {"__formid__": "form-extension-demo", "enable": "enable"},
            headers={**ADMIN, "Origin": "http://evil.example"},
            expect_errors=True,
        )
        self.assertEqual(response.status_int, 400)
        self.assertFalse(self.ext("demo").enabled)

    def test_the_tab_needs_the_manage_permission(self):
        self.assertEqual(self.web.get(self.URL, expect_errors=True).status_int, 403)


class IncludemeTest(WebCase):
    def test_the_includeme_of_an_extension_module_is_called(self):
        # the module is named in the setting: it is scanned (the class) and included (its includeme)
        self.application.create_repository("library")
        config = make_config({**self.settings, "gracia.extensions": "ext_with_include"})
        config.commit()
        config.begin()
        self.addCleanup(config.end)
        node = self.application["library"]
        self.addCleanup(node.close)
        self.assertIn("incl", [e.name for e in node.extensions()])
        self.assertEqual(node["incl:anything"].__name__, "incl:anything")

    def test_without_the_setting_it_is_not_there(self):
        self.application.create_repository("library")
        config = make_config(self.settings)
        config.commit()
        config.begin()
        self.addCleanup(config.end)
        node = self.application["library"]
        self.addCleanup(node.close)
        self.assertNotIn("incl", [e.name for e in node.extensions()])
        with self.assertRaises(KeyError):
            node["incl:anything"]


class JsonNumbersTest(ExtensionsCase):
    def test_a_json_number_is_taken_for_a_number_field(self):
        self.ext("demo").import_json('{"greeting": "hi", "count": 9, "since": "2022-05-06"}')
        self.assertEqual(self.ext("demo").settings["count"], 9)
        with self.assertRaisesRegex(ExtensionError, "count"):  # still checked: 0 is below 1
            self.ext("demo").import_json('{"count": 0}')
        with self.assertRaisesRegex(ExtensionError, "count"):
            self.ext("demo").import_json('{"count": "many"}')


class CommandTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.create_repositories("library")[0], 0)

    def run_cli(self, *args):
        return self.gracia("-r", "library", "extensions", *args)

    def test_list_says_what_is_on(self):
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"ark\s+enabled\s+ARK\s+\(cannot be disabled\)")

    def test_ark_cannot_be_disabled(self):
        code, _, err = self.run_cli("disable", "ark")
        self.assertEqual(code, 1)
        self.assertIn("cannot be disabled", err)

    def test_an_unknown_extension_and_an_unknown_repository_are_errors(self):
        self.assertEqual(self.run_cli("enable", "nobody")[0], 1)
        code, _, err = self.gracia("-r", "nope", "extensions", "list")
        self.assertEqual(code, 1)
        self.assertIn("no such repository", err)

    def test_export_and_import_the_settings(self):
        code, out, err = self.run_cli("export", "ark")
        self.assertEqual((code, json.loads(out)), (0, {"naan": "99999", "root_prefix": "ir"}), err)
        target = self.tmp / "ark.json"
        self.assertEqual(self.run_cli("export", "ark", "-o", str(target))[0], 0)
        self.assertEqual(json.loads(target.read_text()), {"naan": "99999", "root_prefix": "ir"})
        code, out, err = self.run_cli("import", "ark", str(target))  # the same: accepted
        self.assertEqual((code, out.strip()), (0, "ark: settings updated"), err)

    def test_an_import_that_is_not_right_is_refused_with_the_reason(self):
        bad = self.tmp / "bad.json"
        bad.write_text('{"naan": "12345", "root_prefix": "ir"}')
        code, _, err = self.run_cli("import", "ark", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("Only 99999 for now", err)
        bad.write_text("{broken")
        code, _, err = self.run_cli("import", "ark", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("not JSON", err)


class NsPrefixTest(ExtensionsCase):
    def prefixes(self, name="ns_prefix"):
        return {i["prefix"]: i["namespace"] for i in self.ext(name).settings["prefixes"]}

    def test_it_is_always_on_and_a_new_repository_has_the_default_prefixes(self):
        from gracia.extensions.ns_prefix import DEFAULT_PREFIXES

        ext = self.ext("ns_prefix")
        self.assertTrue(ext.enabled)
        self.assertFalse(ext.can_disable)
        self.assertEqual(ext.settings["prefixes"], DEFAULT_PREFIXES)
        self.assertIn("gracia", self.prefixes())

    def test_the_settings_are_read_once_for_a_node_and_again_after_a_change(self):
        ext = self.ext("ns_prefix")
        self.assertEqual(ext.settings, ext.settings)
        ext.save_settings(
            {
                "prefixes": [
                    *ext.settings["prefixes"],
                    {"prefix": "my", "namespace": "http://my.org/"},
                ]
            }
        )
        self.assertEqual(ext.settings["prefixes"][-1]["prefix"], "my")
        self.assertEqual(self.prefixes()["my"], "http://my.org/")  # stored

    def test_a_prefix_twice_or_a_bad_one_is_refused(self):
        ext = self.ext("ns_prefix")
        for items in (
            [("a", "http://a/"), ("a", "http://b/")],
            [("1x", "http://a/")],
            [("xmlx", "http://a/")],
        ):
            text = to_json({"prefixes": [{"prefix": p, "namespace": n} for p, n in items]})
            with self.assertRaises(ExtensionError):
                ext.import_json(text)

    def test_the_settings_form_has_the_prefixes_as_a_list(self):
        page = self.web.get("/library/manage?tab=extensions", headers=ADMIN).text
        self.assertIn("Namespace prefixes", page)
        self.assertIn("http://xmlns.com/foaf/0.1/", page)
        self.assertIn("jquery-sortable.js", page)  # the sequence widget's script
