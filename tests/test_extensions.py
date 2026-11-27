import datetime
import functools
import json
import re

import colander
from base import DbTestCase
from extension_support import put_extension_state
from pyramid.exceptions import ConfigurationError
from pyramid.response import Response
from support import must_search
from test_web import HeaderPolicy, WebCase

from gracia import uri
from gracia.extensions import Extension, ExtensionError
from gracia.forms import to_json
from gracia.main import make_config
from gracia.types.repository import Repository

ADMIN = {"X-User": "admin"}
POSTING = {
    **ADMIN,
    "Origin": "http://localhost:80",
}  # a post has to come from this site


class DemoSettings(colander.MappingSchema):
    schema_type = staticmethod(functools.partial(colander.Mapping, unknown="raise"))

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
    name = "demo"
    title = "Demo"
    description = "An extension for the tests."
    settings_schema = DemoSettings


class OnlyWithPerson(Extension):
    name = "depends"
    title = "Only with gizmo"
    description = "Depends on the repository."

    @property
    def enabled(self):
        return "gizmo" in self.repository.types

    @property
    def can_disable(self):
        return False


class Child:
    def __init__(self, repository, name):
        self.__parent__, self.__name__ = repository, name


class ExtensionsCase(WebCase):
    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.application.create_repository("test_repo")
        self.config = make_config(self.settings)
        self.config.add_gracia_extension(Demo)
        self.config.add_gracia_extension(OnlyWithPerson)
        self.config.add_view(
            lambda request: Response("the demo"),
            context=Repository,
            name="demo",
            enabled_extension="demo",
        )
        self.config.add_gracia_traversable(Child, prefix="hello:")
        self.config.add_gracia_traversable(Child, prefix="hello:long")
        self.config.add_gracia_traversable(self.nothing, prefix="nope:")
        self.config.commit()
        self.web = self.client(self.config)

    @staticmethod
    def nothing(repository, name):
        raise KeyError(name)  # a factory that finds nothing: the segment is not there

    def repo(self):
        self.config.begin()
        self.addCleanup(self.config.end)
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        return node

    def ext(self, name):
        return self.repo().extension(name)

    def stored(self):
        with self.application.new_store("test_repo") as store:
            return Repository.read_extension_states(
                store.get(uri.repository_uri()), uri.repository_uri()
            )


class ExtensionTest(ExtensionsCase):
    def put_state(self, name="demo", settings=None, enabled=None, turtle=None):
        put_extension_state(self.application, name, settings, enabled, turtle)

    def test_the_extensions_are_known_by_name_and_title_and_they_decide_not_the_flag(
        self,
    ):
        repo = self.repo()
        names = [e.name for e in repo.extensions()]
        for name in ("ark", "demo", "depends"):
            self.assertIn(name, names)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(repo.extension("ark").title, "ARK")
        with self.assertRaises(KeyError):
            repo.extension("nobody")
        self.assertTrue(repo.extension("ark").enabled)  # always
        self.assertFalse(repo.extension("demo").enabled)  # off until it is enabled
        self.assertFalse(
            repo.extension("depends").enabled
        )  # the repository has no gizmo type
        self.assertIs(repo.extension("demo"), repo.extension("demo"))  # one per request
        self.assertIs(repo.extension("demo").repository, repo)
        with self.assertRaises(AttributeError):
            repo.extension("demo").enabled = True

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

    def test_the_settings_are_the_defaults_until_said_and_the_dict_is_a_copy(self):
        defaults = {"greeting": "hello", "count": 3, "since": datetime.date(2020, 1, 1)}
        self.assertEqual(self.ext("demo").settings, defaults)  # python values
        demo = self.ext("demo")
        demo.settings["greeting"] = "changed"
        self.assertEqual(demo.settings["greeting"], "hello")
        self.ext("demo").import_json('{"greeting": "hi"}')  # a setting left out
        self.assertEqual(self.ext("demo").settings["count"], 3)

    def test_the_settings_are_stored_as_json_and_a_date_as_text_that_comes_back(self):
        self.ext("demo").save_settings(
            {"greeting": "hi", "count": 5, "since": datetime.date(2021, 2, 3)}
        )
        self.assertEqual(
            self.stored()["demo"][1],
            {"greeting": "hi", "count": "5", "since": "2021-02-03"},
        )
        back = self.ext("demo").settings
        self.assertEqual(back["since"], datetime.date(2021, 2, 3))
        self.assertEqual(back["count"], 5)
        self.assertEqual(json.loads(self.ext("demo").export())["since"], "2021-02-03")
        with self.application.new_store("test_repo") as store:
            data = store.get(uri.repository_uri())
        literals = [
            q.object for q in data if q.predicate.value == uri.EXTENSION_SETTINGS
        ]
        demo = [lit for lit in literals if '"greeting"' in lit.value]
        self.assertEqual(len(demo), 1)
        self.assertEqual(demo[0].datatype.value, uri.RDF_JSON)

    def test_a_setting_that_is_not_there_is_its_default_and_nothing_older_breaks(self):
        # what an older version stored: a key the schema no longer has, a value it no
        # longer takes, and nothing of the newer settings
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
        with self.assertRaisesRegex(
            ExtensionError, "colour"
        ):  # a key the schema does not know
            demo.import_json('{"colour": "red"}')
        with self.assertRaisesRegex(ExtensionError, "count"):
            demo.save_settings(
                {"greeting": "x", "count": 0, "since": datetime.date(2020, 1, 1)}
            )
        with self.assertRaisesRegex(ExtensionError, "count"):
            demo.import_json('{"count": 0}')  # a number is checked as well: below 1
        with self.assertRaisesRegex(ExtensionError, "count"):
            demo.import_json('{"count": "many"}')
        self.assertNotIn("demo", self.stored())
        demo.import_json('{"greeting": "hi", "count": 9, "since": "2022-05-06"}')
        self.assertEqual(self.ext("demo").settings["count"], 9)  # a number is taken

    def test_an_extension_without_settings_says_so(self):
        with self.assertRaisesRegex(ExtensionError, "has no settings"):
            self.ext("depends").import_json("{}")
        self.assertEqual(self.ext("depends").settings, {})

    def test_the_ark_settings_are_the_ones_the_repository_was_made_with_for_now(self):
        ark = {"naan": "99999", "root_prefix": "ir"}
        self.assertEqual(self.ext("ark").settings, ark)
        self.assertEqual(self.stored()["ark"][1], ark)
        with self.assertRaisesRegex(ExtensionError, "Only 99999 for now"):
            self.ext("ark").save_settings({"naan": "12345", "root_prefix": "ir"})
        with self.assertRaisesRegex(ExtensionError, "Only ir for now"):
            self.ext("ark").import_json('{"naan": "99999", "root_prefix": "lib"}')
        self.ext("ark").import_json('{"naan": "99999", "root_prefix": "ir"}')
        self.ext("ark").import_json("{}")  # left out: the defaults, which are the same
        self.assertEqual(self.ext("ark").settings, ark)


class RegistryTest(ExtensionsCase):
    def test_what_is_below_a_repository_comes_from_the_registry_by_prefix(self):
        for name in (
            "jobs",
            "deform",
        ):  # the core registers its own children the same way
            status = self.web.get(
                f"/test_repo/{name}", headers=ADMIN, expect_errors=True
            ).status_int
            self.assertIn(status, (200, 404))
        self.config.begin()
        self.addCleanup(self.config.end)
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        self.assertEqual(node["hello:there"].__name__, "hello:there")
        self.assertEqual(
            node["hello:longer"].__name__, "hello:longer"
        )  # the longest prefix
        with self.assertRaises(KeyError):
            node["nope:anything"]
        with self.assertRaises(KeyError):
            node["nothing"]

    def test_a_key_that_a_type_could_have_is_refused(self):
        config = make_config(self.settings)
        with self.assertRaises(ConfigurationError):
            config.add_gracia_traversable(lambda r: None, name="person")
        config = make_config(self.settings)
        config.add_gracia_traversable(
            lambda r: None, name="ark:"
        )  # a colon: no type has it
        config.add_gracia_traversable(
            lambda r: None, name="jobs"
        )  # reserved on purpose

    def test_a_view_of_an_extension_is_there_only_where_it_is_on(self):
        self.assertEqual(
            self.web.get(
                "/test_repo/demo", headers=ADMIN, expect_errors=True
            ).status_int,
            404,
        )
        self.ext("demo").enable()
        response = self.web.get("/test_repo/demo", headers=ADMIN)
        self.assertEqual(response.text, "the demo")
        self.ext("demo").disable()
        self.assertEqual(
            self.web.get(
                "/test_repo/demo", headers=ADMIN, expect_errors=True
            ).status_int,
            404,
        )


class TabTest(ExtensionsCase):
    URL = "/test_repo/manage?tab=extensions"

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
        return must_search(
            rf'<div class="extension[^"]*" id="extension-{name}">.*?(?=<div '
            r'class="extension|<p '
            r'class="text-muted"|\Z)',
            page,
            re.S,
        )[0]

    def test_there_is_a_card_for_each_extension_and_a_disabled_one_can_be_enabled(self):
        page = self.page()
        tabs = re.findall(r'data-tab="([a-z]+)"', page)
        self.assertIn("extensions", tabs)
        self.assertEqual(tabs.index("extensions"), tabs.index("settings") + 1)
        for name, title in (
            ("ark", "ARK"),
            ("demo", "Demo"),
            ("depends", "Only with gizmo"),
        ):
            self.assertIn(f'id="extension-{name}"', page)
            self.assertIn(title, self.card(page, name))
        card = self.card(page, "demo")  # off: an enable button and no settings
        self.assertIn("disabled", card)
        button = must_search(r'<button[^>]*name="enable"[^>]*>', card)[0]
        self.assertNotIn("disabled=", button)  # it can be enabled
        self.assertNotIn('name="disable"', card)
        self.assertNotIn(">Settings</button>", card)

    def test_ark_cannot_be_turned_off_and_its_form_is_a_normal_form(self):
        card = self.card(self.page(), "ark")
        self.assertIn("enabled", card)
        button = must_search(r'<button[^>]*name="disable"[^>]*>', card)[
            0
        ]  # there, and greyed out
        self.assertRegex(button, r"\sdisabled[\s>]")
        self.assertIn("cannot be disabled", button)
        self.assertNotIn('name="enable"', card)
        self.assertIn(">Settings</button>", card)
        update = must_search(r'<button[^>]*name="update"[^>]*>', card)[0]
        self.assertNotIn("disabled=", update)
        self.assertIn('name="import_json"', card)
        for name, value in (("naan", "99999"), ("root_prefix", "ir")):
            field = must_search(rf'<input[^>]*name="{name}"[^>]*>', card)[0]
            self.assertIn(f'value="{value}"', field)
            self.assertNotIn("readonly", field)
        refused = self.post(
            "ark", "disable"
        )  # refused, and said so in the dialog's tab
        self.assertEqual(refused.status_int, 200)
        self.assertIn("cannot be disabled", refused.text)
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-ark"]
        form["naan"], form["root_prefix"] = "12345", "lib"
        response = form.submit("update", headers=POSTING)
        self.assertEqual(response.status_int, 200)
        card = self.card(response.text, "ark")
        self.assertIn('data-open="true"', card)
        self.assertIn("Only 99999 for now", card)
        self.assertIn("Only ir for now", card)
        self.assertEqual(
            self.ext("ark").settings, {"naan": "99999", "root_prefix": "ir"}
        )
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-ark"]
        self.assertEqual(
            form.submit("update", headers=POSTING).status_int, 302
        )  # as it is: fine

    def test_enabling_and_disabling_and_the_dialog_that_comes_with_it(self):
        response = self.post("demo", "enable")
        self.assertEqual(response.status_int, 302)
        self.assertEqual(
            response.location, "http://localhost/test_repo/manage?tab=extensions"
        )
        page = response.follow(headers=ADMIN).text
        self.assertIn("Demo is enabled", page)
        card = self.card(page, "demo")
        button = must_search(r'<button[^>]*name="disable"[^>]*>', card)[0]
        self.assertNotIn("disabled=", button)  # it can be disabled
        self.assertNotIn('name="enable"', card)
        self.assertIn(">Settings</button>", card)
        self.assertIn('data-confirm="Disable Demo?', card)
        self.assertIn('id="form-extension-demo"', card)
        self.assertIn('name="update"', card)
        self.assertRegex(card, r">\s*Update settings\s*<")
        self.assertIn(">Import / Export<", card)
        text = must_search(
            r'<textarea[^>]*name="json"[^>]*>(.*?)</textarea>', card, re.S
        )[1]
        self.assertEqual(
            json.loads(text.replace("&quot;", '"')),
            {"greeting": "hello", "count": "3", "since": "2020-01-01"},
        )
        self.assertIn('name="import_json"', card)
        self.assertNotIn('data-open="true"', card)
        self.assertEqual(self.post("demo", "disable").status_int, 302)
        self.assertIn("disabled", self.card(self.page(), "demo"))

    def test_updating_the_settings_with_the_form_and_a_form_that_is_not_right(self):
        self.ext("demo").enable()
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-demo"]
        form["count"] = "0"
        response = form.submit("update", headers=POSTING)
        self.assertEqual(response.status_int, 200)
        card = self.card(response.text, "demo")
        self.assertIn('data-open="true"', card)
        self.assertIn("0 is less than minimum value 1", card)
        # the form that comes back is the form: it has its button and posts to the same
        # address
        self.assertRegex(card, r'name="update"[^>]*>\s*Update settings')
        self.assertIn('action="http://localhost/test_repo/manage?tab=extensions"', card)
        self.assertEqual(self.ext("demo").settings["count"], 3)  # nothing stored
        form = self.web.get(self.URL, headers=ADMIN).forms["form-extension-demo"]
        form["greeting"], form["count"], form["date"] = (
            "hi",
            "7",
            "2022-05-06",
        )  # Deform's date widget
        response = form.submit("update", headers=POSTING)
        self.assertEqual(response.status_int, 302)
        self.assertIn(
            "The settings of Demo are updated", response.follow(headers=ADMIN).text
        )
        self.assertEqual(
            self.ext("demo").settings,
            {"greeting": "hi", "count": 7, "since": datetime.date(2022, 5, 6)},
        )

    def test_the_settings_as_json_can_be_pasted_and_json_that_is_wrong_comes_back(self):
        self.ext("demo").enable()
        response = self.post("demo", "import_json", {"json": "{broken"})
        self.assertEqual(response.status_int, 200)
        card = self.card(response.text, "demo")
        self.assertIn('data-open="true"', card)
        self.assertIn("this is not JSON", card)
        self.assertIn('id="ext-demo-json" role="tabpanel"', card)
        self.assertRegex(card, r'class="tab-pane active"\s+id="ext-demo-json"')
        self.assertIn("{broken", card)  # what was pasted is still there
        response = self.post(
            "demo", "import_json", {"json": '{"greeting": "from json", "count": "9"}'}
        )
        self.assertEqual(response.status_int, 302)
        self.assertEqual(self.ext("demo").settings["greeting"], "from json")
        self.assertEqual(self.ext("demo").settings["count"], 9)

    def test_a_post_from_another_site_is_refused_and_the_tab_needs_the_permission(self):
        response = self.web.post(
            self.URL,
            {"__formid__": "form-extension-demo", "enable": "enable"},
            headers={**ADMIN, "Origin": "http://evil.example"},
            expect_errors=True,
        )
        self.assertEqual(response.status_int, 400)
        self.assertFalse(self.ext("demo").enabled)
        self.assertEqual(self.web.get(self.URL, expect_errors=True).status_int, 403)


class IncludemeTest(WebCase):
    def test_the_includeme_of_an_extension_module_is_called_only_when_it_is_named(self):
        # the module is named in the setting: it is scanned (the class) and included
        # (its includeme)
        self.application.create_repository("test_repo")
        for named in (False, True):
            settings = {**self.settings}
            if named:
                settings = {**settings, "gracia.extensions": "ext_with_include"}
            config = make_config(settings)
            config.commit()
            config.begin()
            self.addCleanup(config.end)
            node = self.application["test_repo"]
            self.addCleanup(node.close)
            names = [e.name for e in node.extensions()]
            if named:
                self.assertIn("incl", names)
                self.assertEqual(node["incl:anything"].__name__, "incl:anything")
            else:
                self.assertNotIn("incl", names)
                with self.assertRaises(KeyError):
                    node["incl:anything"]


class CommandTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.create_repositories("test_repo")[0], 0)

    def run_cli(self, *args):
        return self.gracia("-r", "test_repo", "extensions", *args)

    def test_list_says_what_is_on_and_what_cannot_be_done_is_an_error(self):
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"ark\s+enabled\s+ARK\s+\(cannot be disabled\)")
        code, _, err = self.run_cli("disable", "ark")
        self.assertEqual(code, 1)
        self.assertIn("cannot be disabled", err)
        self.assertEqual(self.run_cli("enable", "nobody")[0], 1)
        code, _, err = self.gracia("-r", "nope", "extensions", "list")
        self.assertEqual(code, 1)
        self.assertIn("no such repository", err)

    def test_export_and_import_the_settings_and_an_import_that_is_not_right_is_refused(
        self,
    ):
        code, out, err = self.run_cli("export", "ark")
        self.assertEqual(
            (code, json.loads(out)), (0, {"naan": "99999", "root_prefix": "ir"}), err
        )
        target = self.tmp / "ark.json"
        self.assertEqual(self.run_cli("export", "ark", "-o", str(target))[0], 0)
        self.assertEqual(
            json.loads(target.read_text()), {"naan": "99999", "root_prefix": "ir"}
        )
        code, out, err = self.run_cli(
            "import", "ark", str(target)
        )  # the same: accepted
        self.assertEqual((code, out.strip()), (0, "ark: settings updated"), err)
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
        return {
            i["prefix"]: i["namespace"] for i in self.ext(name).settings["prefixes"]
        }

    def test_it_is_always_on_has_the_default_prefixes_and_a_change_is_stored(self):
        from gracia.extensions.ns_prefix import DEFAULT_PREFIXES

        ext = self.ext("ns_prefix")
        self.assertTrue(ext.enabled)
        self.assertFalse(ext.can_disable)
        self.assertEqual(ext.settings["prefixes"], DEFAULT_PREFIXES)
        self.assertIn("gracia", self.prefixes())
        self.assertEqual(ext.settings, ext.settings)  # read once for a node
        ext.save_settings(
            {
                "prefixes": [
                    *ext.settings["prefixes"],
                    {"prefix": "my", "namespace": "http://my.org/"},
                ]
            }
        )
        self.assertEqual(ext.settings["prefixes"][-1]["prefix"], "my")  # and again
        self.assertEqual(self.prefixes()["my"], "http://my.org/")  # stored

    def test_a_prefix_twice_or_a_bad_one_is_refused_and_the_form_has_them_as_a_list(
        self,
    ):
        ext = self.ext("ns_prefix")
        for items in (
            [("a", "http://a/"), ("a", "http://b/")],
            [("1x", "http://a/")],
            [("xmlx", "http://a/")],
        ):
            text = to_json(
                {"prefixes": [{"prefix": p, "namespace": n} for p, n in items]}
            )
            with self.assertRaises(ExtensionError):
                ext.import_json(text)
        page = self.web.get("/test_repo/manage?tab=extensions", headers=ADMIN).text
        self.assertIn("Namespace prefixes", page)
        self.assertIn("http://xmlns.com/foaf/0.1/", page)
        self.assertIn("jquery-sortable.js", page)  # the sequence widget's script
