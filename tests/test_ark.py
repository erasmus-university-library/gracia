import unittest

from extension_support import put_extension_state
from test_edit import ADMIN, EditCase
from test_web import WebCase

from gracia import uri
from gracia.main import make_config
from gracia.utils.validate import validate_ark


class ModelTest(unittest.TestCase):
    def test_the_ark_of_a_record_is_made_from_its_type_and_number(self):
        self.assertEqual(
            uri.default_resource_uri("rec:person:34"), "ark:/99999/ir/person/34"
        )
        self.assertEqual(
            uri.default_resource_uri("rec:person:34", "12345", "x"),
            "ark:/12345/x/person/34",
        )

    def test_what_is_not_a_record_is_named_with_a_fragment(self):
        self.assertEqual(
            uri.default_resource_uri("rec:repository:library"),
            "rec:repository:library#resource",
        )

    def test_the_settings_are_letters_and_digits(self):
        self.assertEqual(validate_ark("12345", "ir"), ("12345", "ir"))
        for naan, root in (
            ("", "ir"),
            ("9 9", "ir"),
            ("99999", ""),
            ("99999", "a/b"),
            ("99999", "a:b"),
        ):
            with self.assertRaises(ValueError, msg=(naan, root)):
                validate_ark(naan, root)


class RepositoryTest(WebCase):
    def settings_of(self, repo):
        self.config = make_config(self.settings)
        self.config.commit()
        self.config.begin()
        self.addCleanup(self.config.end)
        node = self.application[repo]
        self.addCleanup(node.close)
        return node.extension("ark").settings

    def resource_uris(self, repo):
        with self.application.open_repository(repo) as store:
            rows = store.con.execute(
                "SELECT s, o FROM triples WHERE p = ? AND len(loc) = 0 ORDER BY s",
                [uri.RESOURCE_URI],
            ).fetchall()
        return dict(rows)

    def test_a_new_repository_has_the_test_authority_and_ir(self):
        self.application.create_repository("library", admin=ADMIN)
        self.assertEqual(
            self.settings_of("library"), {"naan": "99999", "root_prefix": "ir"}
        )

    def test_the_system_records_get_their_arks_and_the_types_their_classes(self):
        self.application.create_repository("library", admin=ADMIN)
        self.assertEqual(
            self.resource_uris("library"),
            {
                "rec:group:1": "ark:/99999/ir/group/1",
                "rec:group:2": "ark:/99999/ir/group/2",
                "rec:group:3": "ark:/99999/ir/group/3",
                "rec:repository:1": "ark:/99999/ir/repository/1",
                # a type is published as its class
                "rec:type:1": "https://gracia.pub/ns/Repository",
                "rec:type:2": "https://gracia.pub/ns/Type",
                "rec:type:3": "https://gracia.pub/ns/User",
                "rec:type:4": "https://gracia.pub/ns/UserGroup",
                "rec:user:1": "ark:/99999/ir/user/1",
            },
        )

    def test_a_repository_can_be_made_with_its_own_authority(self):
        self.application.create_repository("library", admin=ADMIN, ark=("12345", "lib"))
        self.assertEqual(
            self.settings_of("library"), {"naan": "12345", "root_prefix": "lib"}
        )
        self.assertEqual(
            self.resource_uris("library")["rec:user:1"], "ark:/12345/lib/user/1"
        )

    def test_wrong_settings_make_nothing(self):
        with self.assertRaises(ValueError):
            self.application.create_repository("library", ark=("99999", "a/b"))
        self.assertFalse(self.application.has_repository("library"))

    def test_a_type_added_later_is_published_as_its_class_not_as_an_ark(self):
        from gracia import types

        self.application.create_repository("library", ark=("12345", "lib"))
        with self.application.open_repository("library") as store:
            person = types.available()["person"]
            record = types.add_content_type(store, "library", person)
            self.assertEqual(record, "rec:type:5")
        self.assertEqual(
            self.resource_uris("library")["rec:type:5"], person.class_uri
        )  # the settings are for the other records


class ResolveTest(EditCase):
    def get(self, path, **kw):
        return self.client().get(path, headers=self.admin, expect_errors=True, **kw)

    def test_the_ark_of_a_record_goes_to_the_record(self):
        response = self.get("/library/ark:/99999/ir/person/1")
        self.assertEqual(response.status_int, 302)
        self.assertEqual(response.location, "http://localhost/library/person/1/")

    def test_what_is_not_there_is_not_found(self):
        for ark in (
            "99999/ir/person/99",  # no such record
            "99999/ir/nothing/1",  # no such type
            "99999/ir/person/x",  # not a number
            "99999/other/person/1",  # not this repository's prefix
            "12345/ir/person/1",  # not this repository's authority
            "99999/ir/person",
            "99999",
        ):
            self.assertEqual(self.get(f"/library/ark:/{ark}").status_int, 404, ark)
        self.assertEqual(self.get("/library/ark:").status_int, 404)

    def test_a_record_that_says_its_own_name_is_found_by_it(self):
        orcid = "https://orcid.org/0000-0002-1825-0097"
        form = self.client().get("/library/person/1/edit", headers=self.admin).forms[0]
        form["resource_uri"] = orcid
        form.submit("save", headers=self.admin)
        # an ark that is not in the form the repository makes, given to a record
        form = self.client().get("/library/person/1/edit", headers=self.admin).forms[0]
        form["resource_uri"] = "ark:/12345/own/ada"
        form.submit("save", headers=self.admin)
        response = self.get("/library/ark:/12345/own/ada")
        self.assertEqual(
            (response.status_int, response.location),
            (302, "http://localhost/library/person/ada/"),
        )
        self.assertEqual(self.get("/library/ark:/12345/own/nobody").status_int, 404)

    def test_the_settings_of_the_repository_decide_what_a_new_record_is_called(self):
        put_extension_state(
            self.application, "ark", {"naan": "12345", "root_prefix": "lib"}
        )
        form = self.client().get("/library/person/add", headers=self.admin).forms[0]
        form["name"] = "Grace"
        self.assertEqual(form.submit("save", headers=self.admin).status_int, 302)
        self.assertIn(
            (uri.RESOURCE_URI, "ark:/12345/lib/person/2"), self.values("rec:person:2")
        )
        self.assertEqual(
            self.get("/library/ark:/12345/lib/person/2").location,
            "http://localhost/library/person/grace/",
        )
        # the record made before keeps the name it was given, and is not found by
        # another one
        self.assertEqual(self.get("/library/ark:/99999/ir/person/1").status_int, 404)
