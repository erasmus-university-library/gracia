import atexit
import base64
import re
import shutil
import tempfile
from pathlib import Path

import pyoxigraph as ox
from conftest import prepared
from support import must_search
from test_web import WebCase

from gracia import init, uri
from gracia import uri as gracia_uri
from gracia.interfaces import IGraciaType
from gracia.main import make_config
from gracia.resources import Record
from gracia.types.user import UserRecord

ADMIN = ("admin", "correct horse")
EDITOR = ("ed", "editor password")
FOAF = "http://xmlns.com/foaf/0.1/"


def basic(user: str, password: str) -> dict:
    return {
        "Authorization": "Basic "
        + base64.b64encode(f"{user}:{password}".encode()).decode()
    }


# a type that is not a system type: people, with a name and a mail address
PEOPLE = """
@prefix gracia: <https://gracia.pub/ns/> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
<rec:repository:1> a gracia:Record ;
    gracia:resource [ a gracia:Repository ; gracia:title "test_repo" ;
        gracia:types ( gracia:Repository gracia:Type gracia:User gracia:UserGroup
          foaf:Person ) ] .
<rec:type:5> a gracia:Record ; gracia:resource_uri foaf:Person ;
    gracia:resource [ a gracia:Type ; gracia:name "Person" ; gracia:slug "person" ;
        gracia:class foaf:Person ; gracia:assets "@ASSETS@" ;
        gracia:schemaTemplate "Person-full-01.ttl" ] .
<rec:person:1> a gracia:Record ;
    gracia:resource [ a foaf:Person ; foaf:name "Ada" ; foaf:knows
      <ark:/99999/ir/person/2> ] .
"""


PERSON_PREFIXES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix dash: <http://datashapes.org/dash#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
"""
# two profiles of the person type: everything, and just the name
PERSON_SCHEMAS = {
    "Person-full-01.ttl": PERSON_PREFIXES
    + """<urn:test:PersonShape> a sh:NodeShape ; sh:targetClass foaf:Person ;
    sh:property [ sh:path foaf:name ; sh:name "Name" ; sh:order 1 ;
        dash:propertyRole dash:LabelRole ;
        sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ,
    [ sh:path foaf:mbox ; sh:name "Mail" ; sh:order 2 ;
        sh:datatype xsd:string ; sh:maxCount 1 ] .""",
    "Person-minimal-01.ttl": PERSON_PREFIXES
    + """<urn:test:MinimalPersonShape> a sh:NodeShape ; sh:targetClass foaf:Person ;
    sh:property [ sh:path foaf:name ; sh:name "Name" ; sh:order 1 ;
        dash:propertyRole dash:LabelRole ;
        sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] .""",
}


# The schema files of the person type, in a directory that is made once in a process and
# written
# clean at the
# start of every test (a test may add a file or change one), so that its path is the
# same in every
# test and the
# database a test starts from can be built once and copied (`conftest.prepared`).
# Workers are
# processes, and the
# tests of one run one after the other.
_PERSON_FILES = Path(tempfile.mkdtemp(prefix="gracia_test_person_"))
atexit.register(shutil.rmtree, _PERSON_FILES, ignore_errors=True)


def person_files() -> str:
    shutil.rmtree(_PERSON_FILES / "shacl", ignore_errors=True)
    (_PERSON_FILES / "shacl").mkdir()
    for name, text in PERSON_SCHEMAS.items():
        (_PERSON_FILES / "shacl" / name).write_text(text)
    return str(_PERSON_FILES) + "/"


class EditCase(WebCase):
    def setUp(self):
        super().setUp()
        self.person_assets = (
            person_files()
        )  # the type's files are on disk, named by an absolute asset spec
        prepared(self.application, "edit-case", self.build_fixture)
        self.admin, self.editor = basic(*ADMIN), basic(*EDITOR)

    def build_fixture(self, application):
        application.create_repository("test_repo", admin=ADMIN)
        with application.new_store("test_repo") as store:
            people = PEOPLE.replace("@ASSETS@", self.person_assets)
            store.put(ox.Dataset(ox.parse(people.encode(), format=ox.RdfFormat.TURTLE)))
            store.put(
                init.user_dataset(
                    "rec:user:2", EDITOR[0], EDITOR[1], ["ark:/99999/ir/group/2"]
                )
            )

    def status(self, path, headers=None, config=None):
        web = self.client(config)
        return web.get(path, headers=headers, expect_errors=True).status_int

    def number_of(self, slug: str, res_type: str = "person") -> str:
        with self.application.new_store("test_repo") as store:
            return str(store.slug_lookup(res_type, slug)[0] % 10_000_000)  # ty: ignore[not-subscriptable]

    def values(self, uri):
        with self.application.new_store("test_repo") as store:
            data = store.get(uri)
        stamps = {
            gracia_uri.CREATED_AT,
            gracia_uri.MODIFIED_AT,
        }  # the time of the test run: test_timestamps.py
        return sorted(
            (q.predicate.value, q.object.value)
            for q in data
            if not isinstance(q.object, ox.BlankNode)
            and q.predicate.value not in stamps
        )


class RegistryTest(EditCase):
    def record_class(self, slug):
        config = make_config(self.settings)
        config.commit()
        return config.registry.queryUtility(IGraciaType, slug).record_class  # ty: ignore[missing-union-attribute]

    def context_class(self, path):
        seen = []
        config = make_config(self.settings)
        config.add_view(
            lambda request: seen.append(type(request.context)) or {},
            context=Record,
            name="class",
            renderer="json",
        )
        self.client(config).get(path + "/class", headers=self.admin)
        return seen[0]

    def test_a_type_gets_the_class_registered_for_it_else_record(self):
        self.assertIs(
            self.context_class("/test_repo/user/1"), self.record_class("user")
        )
        self.assertIs(self.context_class("/test_repo/person/1"), Record)
        self.assertIs(self.context_class("/test_repo/type/2"), Record)

    def test_a_type_package_is_included_by_its_dotted_name(self):
        import sys

        with tempfile.TemporaryDirectory() as root:
            package = Path(root) / "extra_types"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from gracia.resources import Record\n"
                "class PersonRecord(Record): ...\n"
                "def includeme(config):\n"
                "    config.add_record_class('http://xmlns.com/foaf/0.1/Person', "
                "PersonRecord)\n"
            )
            sys.path.insert(0, root)
            self.addCleanup(sys.path.remove, root)
            self.addCleanup(sys.modules.pop, "extra_types", None)
            settings = {**self.settings, "gracia.types": "extra_types"}
            seen = []
            config = make_config(settings)
            config.add_view(
                lambda request: seen.append(type(request.context).__name__) or {},
                context=Record,
                name="class",
                renderer="json",
            )
            self.client(config).get("/test_repo/person/1/class", headers=self.admin)
        self.assertEqual(seen, ["PersonRecord"])

    def test_a_class_can_be_registered_by_an_extension(self):
        class UserGroupRecord(Record):
            pass

        config = make_config(self.settings)
        config.add_record_class(uri.GROUP_CLASS, UserGroupRecord)
        seen = []
        config.add_view(
            lambda request: seen.append(type(request.context)) or {},
            context=UserGroupRecord,
            name="hello",
            renderer="json",
        )
        web = self.client(config)
        web.get("/test_repo/group/1/hello", headers=self.admin)
        self.assertEqual(seen, [UserGroupRecord])
        # a view for that class is not reached for records of other types
        self.assertEqual(
            self.status("/test_repo/user/1/hello", self.admin, config), 404
        )

    def test_a_view_for_the_class_overrides_the_generic_one(self):
        config = make_config(self.settings)
        config.add_view(
            lambda request: {"mine": True},
            context=self.record_class("user"),
            name="edit",
            renderer="json",
            permission="edit",
        )
        self.assertEqual(
            self.client(config).get("/test_repo/user/1/edit", headers=self.admin).json,
            {"mine": True},
        )
        self.assertIn(
            "<form",
            self.client().get("/test_repo/person/1/edit", headers=self.admin).text,
        )


class SubclassedViewTest(EditCase):
    def test_a_subclass_changes_the_heading_and_leaves_the_form_the_source_and_saving(
        self,
    ):
        from gracia.views.record import RecordView

        class Shouting(RecordView):
            def __init__(self, request):
                super().__init__(request)
                self.heading = self.record.title.upper()

        config = make_config(self.settings)
        config.add_view(
            Shouting, attr="edit", context=UserRecord, name="edit", permission="edit"
        )
        web = self.client(config)
        page = web.get("/test_repo/user/1/edit", headers=self.admin)
        self.assertIn(">ADMIN<", page.text)  # its own heading
        self.assertIn('name="userid"', page.text)  # the form is the base's
        form = page.forms[0]
        form["userid"] = "admin"
        self.assertEqual(
            form.submit("save", headers=self.admin).status_int, 302
        )  # and so is saving
        generic = self.client().get("/test_repo/user/1/edit", headers=self.admin).text
        self.assertNotIn(">ADMIN<", generic)  # the other pages are as they were


class EditViewTest(EditCase):
    def edit(self, path, headers, **fields):
        web = self.client()
        form = web.get(path, headers=headers).forms[0]
        for name, text in fields.items():
            form[name] = text
        return form.submit("save", headers=headers)

    def test_the_form_comes_from_the_shape_of_the_type(self):
        response = self.client().get("/test_repo/person/1/edit", headers=self.admin)
        self.assertIn('name="name"', response.text)
        self.assertIn('value="Ada"', response.text)
        self.assertIn('name="mbox"', response.text)
        self.assertIn('name="resource_uri"', response.text)

    def test_saving_changes_the_content_and_keeps_what_the_form_cannot_show(self):
        response = self.edit(
            "/test_repo/person/1/edit",
            self.admin,
            name="Ada Lovelace",
            mbox="ada@example.org",
        )
        self.assertEqual(response.status_int, 302)
        self.assertEqual(
            self.values("rec:person:1"),
            sorted(
                [
                    (f"{FOAF}name", "Ada Lovelace"),
                    (f"{FOAF}mbox", "ada@example.org"),
                    (
                        f"{FOAF}knows",
                        "ark:/99999/ir/person/2",
                    ),  # no field for it: untouched
                    (
                        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                        f"{FOAF}Person",
                    ),
                    (
                        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                        uri.RECORD_CLASS,
                    ),
                    (
                        uri.RESOURCE_URI,
                        "ark:/99999/ir/person/1",
                    ),  # the default, saved with the form
                    (uri.LABEL, "Ada Lovelace"),  # copied from the content
                    (uri.SLUG, "ada-lovelace"),
                    (uri.IS_PUBLISHED, "true"),
                ]
            ),
        )

    def test_an_empty_value_removes_the_property_and_a_required_one_is_refused(self):
        self.edit("/test_repo/person/1/edit", self.admin, mbox="x@example.org")
        self.edit("/test_repo/person/1/edit", self.admin, mbox="")
        self.assertNotIn(f"{FOAF}mbox", [p for p, _ in self.values("rec:person:1")])
        response = self.edit("/test_repo/person/1/edit", self.admin, name="")
        self.assertEqual(response.status_int, 200)  # the form again, with the error
        self.assertIn(
            ("http://xmlns.com/foaf/0.1/name", "Ada"), self.values("rec:person:1")
        )

    def test_the_resource_uri_is_the_ark_until_one_is_given_and_again_when_it_is_emptied(  # noqa: E501
        self,
    ):
        # the record was stored before the resource uri was required: nothing is shown
        # for it
        # until it is saved, and what saves it (the SQL of the predicate) makes the ARK
        form = (
            self.client().get("/test_repo/person/1/edit", headers=self.admin).forms[0]
        )
        self.assertEqual(form["resource_uri"].value, "")
        self.assertEqual(form.submit("save", headers=self.admin).status_int, 302)
        ark = (uri.RESOURCE_URI, "ark:/99999/ir/person/1")
        self.assertIn(ark, self.values("rec:person:1"))
        orcid = "https://orcid.org/0000-0002-1825-0097"
        self.edit("/test_repo/person/1/edit", self.admin, resource_uri=orcid)
        self.assertEqual(
            self.client()
            .get(
                "/test_repo/person/1",
                headers={**self.admin, "Accept": "application/json"},
            )
            .json["resource_uri"],
            [orcid],
        )
        self.assertIn(
            f'value="{orcid}"',
            self.client().get("/test_repo/person/1/edit", headers=self.admin).text,
        )
        self.assertEqual(
            self.edit(
                "/test_repo/person/1/edit", self.admin, resource_uri="not an uri"
            ).status_int,
            200,
        )
        self.edit("/test_repo/person/1/edit", self.admin, resource_uri="")
        self.assertIn(ark, self.values("rec:person:1"))  # the default again

    def test_who_may_edit(self):
        self.assertEqual(
            self.status("/test_repo/person/1/edit"), 401
        )  # anonymous: asked to log in
        self.assertEqual(
            self.status("/test_repo/person/1/edit", self.editor), 200
        )  # editor group
        self.assertEqual(self.status("/test_repo/person/1/edit", self.admin), 200)
        self.assertEqual(
            self.status("/test_repo/user/1/edit", self.editor), 403
        )  # users: admin only
        self.assertEqual(
            self.status("/test_repo/type/5/edit", self.editor), 403
        )  # structure too
        self.assertEqual(
            self.status("/test_repo/type/5", self.editor), 200
        )  # but it can be read
        self.assertEqual(self.status("/test_repo/type/5"), 200)
        self.assertEqual(self.status("/test_repo/manage", self.editor), 403)

    def test_the_form_has_a_tab_for_the_person_and_one_for_the_record(self):
        response = self.client().get("/test_repo/person/1/edit", headers=self.admin)
        tabs = re.findall(r'class="tab-title">([^<]+)', response.text)
        self.assertEqual(tabs, ["Person", "Record"])

    def test_a_record_key_goes_to_the_address_of_the_record(self):
        web = self.client()
        response = web.get("/test_repo/rec:person:1", headers=self.admin)
        self.assertEqual(response.status_int, 302)
        self.assertEqual(response.location, "http://localhost/test_repo/person/1/")
        for key in ("rec:person:99", "rec:nothing:1", "rec:person:x", "rec:person"):
            self.assertEqual(self.status(f"/test_repo/{key}", self.admin), 404, key)

    def test_an_editor_edits_a_person(self):
        response = self.edit("/test_repo/person/1/edit", self.editor, name="Ada King")
        self.assertEqual(response.status_int, 302)
        self.assertIn((f"{FOAF}name", "Ada King"), self.values("rec:person:1"))


class UserEditTest(EditCase):
    def edit(self, **fields):
        form = self.client().get("/test_repo/user/2/edit", headers=self.admin).forms[0]
        for name, text in fields.items():
            form[name] = text
        return form.submit("save", headers=self.admin)

    def test_the_credentials_are_not_in_the_form(self):
        response = self.client().get("/test_repo/user/1/edit", headers=self.admin)
        self.assertIn('name="userid"', response.text)
        self.assertIn('name="password"', response.text)
        self.assertNotIn("scrypt", response.text)
        self.assertNotIn('name="credentials"', response.text)

    def test_saving_without_a_password_keeps_the_old_one(self):
        self.assertEqual(self.edit(userid="ed2").status_int, 302)
        self.assertEqual(
            self.status("/test_repo/user", basic("ed2", EDITOR[1])), 403
        )  # logs in
        self.assertEqual(
            self.status("/test_repo/user", basic("ed", EDITOR[1])), 401
        )  # the old id is gone

    def test_a_new_password_replaces_the_hash(self):
        self.assertEqual(self.edit(password="a brand new password").status_int, 302)
        self.assertEqual(
            self.status("/test_repo/user", basic("ed", "a brand new password")), 403
        )
        self.assertEqual(self.status("/test_repo/user", basic(*EDITOR)), 401)
        with self.application.new_store("test_repo") as store:
            self.assertNotIn("a brand new password", str(store.get("rec:user:2")))

    def test_a_short_password_is_refused(self):
        self.assertEqual(self.edit(password="short").status_int, 200)
        self.assertEqual(
            self.status("/test_repo/user", basic(*EDITOR)), 403
        )  # unchanged

    def test_a_user_id_that_is_taken_is_refused(self):
        response = self.edit(userid="admin")
        self.assertEqual(response.status_int, 200)
        self.assertIn("is taken", response.text)
        self.assertEqual(
            self.status("/test_repo/user", basic(*EDITOR)), 403
        )  # still "ed"

    def test_group_membership_survives_an_edit(self):
        self.edit(userid="ed")
        self.assertIn(
            (uri.MEMBER_OF, "ark:/99999/ir/group/2"), self.values("rec:user:2")
        )


class AddViewTest(EditCase):
    def add(self, res_type, headers, **fields):
        form = self.client().get(f"/test_repo/{res_type}/add", headers=headers).forms[0]
        for name, text in fields.items():
            form[name] = text
        return form.submit("save", headers=headers)

    def numbers(self, res_type):
        return (
            self.client()
            .get(f"/test_repo/{res_type}", headers=self.admin)
            .json["records"]
        )

    def test_the_form_is_the_edit_form_without_values(self):
        response = self.client().get("/test_repo/person/add", headers=self.admin)
        for name in ("name", "mbox", "resource_uri"):
            self.assertIn(f'name="{name}"', response.text)
        self.assertNotIn('value="Ada"', response.text)

    def test_adding_mints_the_next_number_and_stores_the_record(self):
        response = self.add(
            "person", self.admin, name="Grace Hopper", mbox="grace@example.org"
        )
        self.assertEqual(response.status_int, 302)
        self.assertEqual(
            response.location,
            "http://localhost/test_repo/person/grace-hopper/edit?saved=1",
        )
        self.assertEqual(self.numbers("person"), [1, 2])
        self.assertEqual(
            self.values("rec:person:2"),
            sorted(
                [
                    (f"{FOAF}name", "Grace Hopper"),
                    (f"{FOAF}mbox", "grace@example.org"),
                    (
                        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                        f"{FOAF}Person",
                    ),
                    (
                        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                        uri.RECORD_CLASS,
                    ),
                    (uri.RESOURCE_URI, "ark:/99999/ir/person/2"),
                    (uri.LABEL, "Grace Hopper"),
                    (uri.SLUG, "grace-hopper"),
                    (uri.IS_PUBLISHED, "true"),
                ]
            ),
        )
        self.assertEqual(
            self.client().get("/test_repo/person/2", headers=self.admin).status_int, 200
        )

    def test_a_refused_form_does_not_use_up_a_number(self):
        self.assertEqual(self.add("person", self.admin, name="").status_int, 200)
        self.assertEqual(self.numbers("person"), [1])
        self.add("person", self.admin, name="Grace")
        self.add("person", self.admin, name="Barbara")
        self.assertEqual(self.numbers("person"), [1, 2, 3])  # no gap

    def test_the_resource_uri_can_be_given_when_adding(self):
        orcid = "https://orcid.org/0000-0002-1825-0097"
        self.add("person", self.admin, name="Josiah", resource_uri=orcid)
        body = (
            self.client()
            .get(
                "/test_repo/person/2",
                headers={**self.admin, "Accept": "application/json"},
            )
            .json
        )
        self.assertEqual(body["resource_uri"], [orcid])

    def test_who_may_add(self):
        self.assertEqual(self.status("/test_repo/person/add"), 401)
        self.assertEqual(self.status("/test_repo/person/add", self.editor), 200)
        self.assertEqual(self.status("/test_repo/user/add", self.editor), 403)
        self.assertEqual(self.status("/test_repo/group/add", self.editor), 403)
        self.assertEqual(self.status("/test_repo/user/add", self.admin), 200)
        response = self.add("person", self.editor, name="By an editor")
        self.assertEqual(response.status_int, 302)

    def test_a_user_needs_a_password_and_can_then_log_in(self):
        self.assertEqual(
            self.add("user", self.admin, userid="newbie", password="").status_int, 200
        )
        self.assertEqual(self.numbers("user"), [1, 2])
        response = self.add(
            "user", self.admin, userid="newbie", password="long enough pw"
        )
        self.assertEqual(response.status_int, 302)
        self.assertEqual(self.numbers("user"), [1, 2, 3])
        self.assertEqual(
            self.status("/test_repo/manage", basic("newbie", "long enough pw")), 403
        )
        self.assertEqual(
            self.status("/test_repo/manage", basic("newbie", "wrong")), 401
        )
        self.assertNotIn("long enough pw", str(self.values("rec:user:3")))

    def test_a_user_id_that_is_taken_cannot_be_added(self):
        response = self.add(
            "user", self.admin, userid="admin", password="long enough pw"
        )
        self.assertEqual(response.status_int, 200)
        self.assertIn("is taken", response.text)
        self.assertEqual(self.numbers("user"), [1, 2])

    def test_a_type_the_form_cannot_fill_in_is_refused_not_half_made(self):
        response = self.client().get(
            "/test_repo/type/add", headers=self.admin, expect_errors=True
        )
        self.assertEqual(response.status_int, 501)
        self.assertIn("cannot fill in", response.text)
        self.assertEqual(
            self.client().get("/test_repo/type", headers=self.admin).json["records"],
            [1, 2, 3, 4, 5],
        )

    def test_adding_to_a_type_that_does_not_exist_is_404(self):
        self.assertEqual(self.status("/test_repo/nope/add", self.admin), 404)


class SwitchingSchemaTest(EditCase):
    def fields(self, path="/test_repo/person/1/edit"):
        response = self.client().get(path, headers=self.admin)
        return {n for n in ("name", "mbox") if f'name="{n}"' in response.text}

    def switch(self, schema, by_hand=False):
        form = (
            self.client()
            .get("/test_repo/person/manage", headers=self.admin)
            .forms["form-type-settings"]
        )
        if by_hand:
            form["schemaTemplate"].force_value(schema)
        else:
            form["schemaTemplate"] = schema
        return form.submit(
            "save", headers={**self.admin, "Origin": "http://localhost:80"}
        )

    def test_the_type_starts_on_the_full_profile(self):
        self.assertEqual(self.fields(), {"name", "mbox"})

    def test_switching_to_the_minimal_profile_changes_the_forms(self):
        self.assertEqual(self.switch("Person-minimal-01.ttl").status_int, 302)
        self.assertEqual(self.fields(), {"name"})
        self.assertEqual(self.fields("/test_repo/person/add"), {"name"})
        self.switch("Person-full-01.ttl")  # and back
        self.assertEqual(self.fields(), {"name", "mbox"})

    def test_what_the_minimal_form_cannot_show_is_kept(self):
        self.client().get("/test_repo/person/1/edit", headers=self.admin)
        form = (
            self.client().get("/test_repo/person/1/edit", headers=self.admin).forms[0]
        )
        form["mbox"] = "ada@example.org"
        form.submit("save", headers=self.admin)
        self.switch("Person-minimal-01.ttl")
        form = (
            self.client().get("/test_repo/person/1/edit", headers=self.admin).forms[0]
        )
        form["name"] = "Ada King"
        form.submit("save", headers=self.admin)
        stored = self.values("rec:person:1")
        self.assertIn(
            (f"{FOAF}mbox", "ada@example.org"), stored
        )  # not in the form, still there
        self.assertIn((f"{FOAF}name", "Ada King"), stored)

    def test_the_version_of_the_type_follows_the_schema_it_is_on(self):
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        full = node["person"].assets_version
        self.switch("Person-minimal-01.ttl")
        other = self.application["test_repo"]
        self.addCleanup(other.close)
        self.assertNotEqual(other["person"].assets_version, full)

    def test_the_form_offers_the_schema_files_of_the_type_and_no_others(self):
        # the field of `gracia:schemaTemplate` is a select of the files of the directory
        # `gracia:assets` names
        page = self.client().get("/test_repo/person/manage", headers=self.admin).text
        select = must_search(r'<select name="schemaTemplate".*?</select>', page, re.S)[
            0
        ]
        self.assertEqual(
            re.findall(r'<option[^>]*value="([^"]+)"', select),
            ["Person-full-01.ttl", "Person-minimal-01.ttl"],
        )
        self.assertIn('<option selected="selected" value="Person-full-01.ttl">', select)

    def test_a_schema_that_is_not_a_file_of_the_type_is_refused(self):
        for schema in ("Person-full-09.ttl", "Work-full-01.ttl", "../../etc/passwd"):
            response = self.switch(schema, by_hand=True)
            self.assertEqual(
                response.status_int, 200, schema
            )  # the form again, with the error
            self.assertEqual(self.fields(), {"name", "mbox"}, schema)  # unchanged
