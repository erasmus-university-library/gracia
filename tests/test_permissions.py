import contextlib
from unittest import mock

import pyoxigraph as ox
from pyramid.authorization import ACLHelper, Allow, Everyone
from test_edit import EditCase

from gracia import uri
from gracia.main import make_config
from gracia.types import type as record_module
from gracia.types.group import UserGroupRecord
from gracia.types.user import UserRecord


class PrincipalsPolicy:
    challenge = False  # a refusal is a plain 403

    def authenticated_userid(self, request):
        return request.headers.get("X-Principals") and "someone"

    identity = authenticated_userid

    def permits(self, request, context, permission):
        principals = {
            Everyone,
            *filter(None, request.headers.get("X-Principals", "").split(",")),
        }
        return ACLHelper().permits(context, principals, permission)

    def remember(self, request, userid, **kw):
        return []

    def forget(self, request, **kw):
        return []


def as_(*principals):
    return {"X-Principals": ",".join(principals)}


class PermissionsCase(EditCase):
    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = PrincipalsPolicy()  # ty: ignore[invalid-assignment]

    def can(self, path, *principals):
        response = self.client().get(path, headers=as_(*principals), expect_errors=True)
        self.assertIn(
            response.status_int, (200, 403, 404), path
        )  # 404: not there for them
        return response.status_int == 200


class EditorsTest(PermissionsCase):
    def test_editors_edit_and_add_ordinary_records_and_cannot_touch_users_or_groups_and_admins_can_do_all_of_it(
        self,
    ):
        for path in ("/test_repo/person/1/edit", "/test_repo/person/add"):
            self.assertTrue(self.can(path, "group:editor"), path)
            self.assertFalse(self.can(path), path)  # the public may not
            self.assertFalse(self.can(path, "group:somebody-else"), path)

        for path in (
            "/test_repo/user/1/edit",
            "/test_repo/user/add",
            "/test_repo/group/1/edit",
            "/test_repo/group/add",
            "/test_repo/user",
            "/test_repo/user/1",
            "/test_repo/group",
            "/test_repo/group/1",
        ):
            self.assertFalse(self.can(path, "group:editor"), path)

        for path in (
            "/test_repo/person/1/edit",
            "/test_repo/user/1/edit",
            "/test_repo/user/add",
            "/test_repo/group/1/edit",
            "/test_repo/group/add",
        ):
            self.assertTrue(self.can(path, "group:admin"), path)


class SpecialPermissionsTest(PermissionsCase):
    def acls(self, **extra):
        classes = {"USER_CLASS": UserRecord, "GROUP_CLASS": UserGroupRecord}
        patched = contextlib.ExitStack()
        for name, entries in extra.items():
            patched.enter_context(mock.patch.object(classes[name], "acl", entries))
        return patched

    def test_super_edit_alone_opens_the_users_and_nothing_else_and_edit_is_not_enough(
        self,
    ):
        grant = [
            (Allow, "group:useradmin", ["view", "super_edit"]),
            *UserRecord.acl,
        ]
        with self.acls(USER_CLASS=grant):
            self.assertTrue(self.can("/test_repo/user/1/edit", "group:useradmin"))
            self.assertTrue(self.can("/test_repo/user/add", "group:useradmin"))
            self.assertFalse(self.can("/test_repo/group/1/edit", "group:useradmin"))
            self.assertFalse(
                self.can("/test_repo/person/1/edit", "group:useradmin")
            )  # not `edit`
            self.assertFalse(self.can("/test_repo/manage", "group:useradmin"))
        # even where an ACL gives editors everything on the users' type, the edit view
        # of a user asks for super_edit, which `edit` does not include
        grant = [
            (Allow, "group:editor", ["view", "edit", "add"]),
            *UserRecord.acl,
        ]
        with self.acls(USER_CLASS=grant):
            self.assertFalse(self.can("/test_repo/user/1/edit", "group:editor"))
            self.assertFalse(self.can("/test_repo/user/add", "group:editor"))
            self.assertTrue(
                self.can("/test_repo/user/1", "group:editor")
            )  # reading is `view`

    def test_manage_alone_opens_the_groups(self):
        grant = [
            (Allow, "group:groupadmin", ["view", "manage"]),
            *UserGroupRecord.acl,
        ]
        with self.acls(GROUP_CLASS=grant):
            self.assertTrue(self.can("/test_repo/group/1/edit", "group:groupadmin"))
            self.assertTrue(self.can("/test_repo/group/add", "group:groupadmin"))
            self.assertFalse(self.can("/test_repo/user/1/edit", "group:groupadmin"))

    def test_an_extension_can_name_its_own_permissions(self):
        class Person(record_module.Record):
            edit_permission = "edit_people"

        config = make_config(self.settings)
        config.add_record_class("http://xmlns.com/foaf/0.1/Person", Person)
        web = self.client(config)
        # `edit` no longer opens it: the class asks for edit_people, which nobody has
        # yet
        response = web.get(
            "/test_repo/person/1/edit", headers=as_("group:editor"), expect_errors=True
        )
        self.assertEqual(response.status_int, 403)
        self.assertEqual(
            web.get("/test_repo/person/1/edit", headers=as_("group:admin")).status_int,
            200,
        )
        # adding is still `add`
        self.assertEqual(
            web.get("/test_repo/person/add", headers=as_("group:editor")).status_int,
            200,
        )


class NeverPublicTest(PermissionsCase):
    PRIVATE = (
        "/test_repo/user",
        "/test_repo/user/1",
        "/test_repo/group",
        "/test_repo/group/1",
    )

    def test_users_and_groups_are_closed_to_the_public_and_to_everybody_else_and_the_rest_stays_public(
        self,
    ):
        for path in self.PRIVATE:
            for who in ((), ("group:editor",), ("user:someone",), ("group:other",)):
                self.assertFalse(self.can(path, *who), (path, who))
            self.assertTrue(self.can(path, "group:admin"), path)
        self.assertTrue(
            self.can("/test_repo/")
        )  # the repository and the ordinary types
        self.assertTrue(self.can("/test_repo/person"))  # stay public
        self.assertTrue(self.can("/test_repo/person/1"))

    def test_the_type_records_that_describe_them_are_closed_too_and_the_type_list_does_not_name_them(
        self,
    ):
        for path in (
            "/test_repo/type/1",
            "/test_repo/type/3",
            "/test_repo/type/4",
        ):  # Repository, User, UserGroup
            self.assertFalse(self.can(path), path)
            self.assertFalse(self.can(path, "group:editor"), path)
            self.assertTrue(self.can(path, "group:admin"), path)
        self.assertTrue(
            self.can("/test_repo/type/2")
        )  # Type itself and other types stay public
        self.assertTrue(self.can("/test_repo/type/5"))

        public = (
            self.client()
            .get("/test_repo/type", headers={**as_(), "Accept": "application/json"})
            .json["records"]
        )
        self.assertEqual(public, [2, 5])  # Type itself and Person
        admin = (
            self.client()
            .get(
                "/test_repo/type",
                headers={**as_("group:admin"), "Accept": "application/json"},
            )
            .json["records"]
        )
        self.assertEqual(admin, [1, 2, 3, 4, 5])

    def test_a_record_cannot_open_itself_up(self):
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:user:1")
            public = ox.Quad(
                ox.NamedNode("rec:user:1"),
                ox.NamedNode(uri.GRACIA_NS + "public"),
                ox.Literal("true"),
                ox.DefaultGraph(),
            )
            data.add(public)
            store.put_multi(data)
        self.assertFalse(self.can("/test_repo/user/1"))
        self.assertFalse(self.can("/test_repo/user/1", "group:editor"))


class ManagersTest(PermissionsCase):
    def test_managers_edit_and_add_like_editors_and_add_and_change_and_make_users(self):
        for path in ("/test_repo/person/1/edit", "/test_repo/person/add"):
            self.assertTrue(self.can(path, "group:manager"), path)
        for path in (
            "/test_repo/user",
            "/test_repo/user/1",
            "/test_repo/user/1/edit",
            "/test_repo/user/add",
        ):
            self.assertTrue(self.can(path, "group:manager"), path)
        web = self.client()
        form = web.get("/test_repo/user/add", headers=as_("group:manager")).forms[0]
        form["userid"], form["password"] = "made-by-manager", "long enough pw"
        self.assertEqual(
            form.submit("save", headers=as_("group:manager")).status_int, 302
        )

    def test_managers_cannot_touch_groups_or_the_repository(self):
        for path in (
            "/test_repo/group",
            "/test_repo/group/1",
            "/test_repo/group/1/edit",
            "/test_repo/group/add",
            "/test_repo/manage",
            "/test_repo/type/4",  # the type record that describes the groups
        ):
            self.assertFalse(self.can(path, "group:manager"), path)
        # the type record of the users follows the ACL of the users: managers may read
        # it
        self.assertTrue(self.can("/test_repo/type/3", "group:manager"))


class TheClosedNodesDoNotAnswerForUnknownThingsTest(PermissionsCase):
    def status(self, path, *principals):  # ty: ignore[incompatible-method-override]
        return (
            self.client()
            .get(path, headers=as_(*principals), expect_errors=True)
            .status_int
        )

    def test_unknown_records_of_users_and_groups_look_like_known_ones(self):
        # (a record that exists, one that does not, what the public is told about both)
        for known, unknown, public in (
            ("/test_repo/user/1", "/test_repo/user/99", 403),
            ("/test_repo/user/1", "/test_repo/user/x", 403),
            ("/test_repo/user/1/edit", "/test_repo/user/99/edit", 403),
            ("/test_repo/group/1", "/test_repo/group/99", 403),
            # the Type type is public, so a hidden type record is simply not there
            ("/test_repo/type/3", "/test_repo/type/99", 404),
            ("/test_repo/type/3/edit", "/test_repo/type/99/edit", 404),
        ):
            for who in ((), ("group:editor",)):
                answers = (self.status(known, *who), self.status(unknown, *who))
                self.assertEqual(answers[0], answers[1], (known, unknown, who))
                self.assertEqual(answers[0], public, (known, who))

    def test_for_those_who_may_see_them_unknown_is_still_404_and_public_things_are_404_for_everybody(
        self,
    ):
        self.assertEqual(self.status("/test_repo/user/99", "group:admin"), 404)
        self.assertEqual(self.status("/test_repo/user/99", "group:manager"), 404)
        self.assertEqual(self.status("/test_repo/group/99", "group:admin"), 404)
        self.assertEqual(self.status("/test_repo/person/99"), 404)
        self.assertEqual(self.status("/test_repo/nope"), 404)
        self.assertEqual(self.status("/nope/"), 404)
