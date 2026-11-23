from unittest import mock

import pyoxigraph as ox
from pyramid.authorization import ACLHelper, Allow, Everyone
from test_edit import EditCase

from gracia import resources, uri
from gracia.main import make_config


class PrincipalsPolicy:
    """The caller has the principals the `X-Principals` header lists (comma
    separated)."""

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
    def test_editors_edit_and_add_ordinary_records(self):
        for path in ("/test_repo/person/1/edit", "/test_repo/person/add"):
            self.assertTrue(self.can(path, "group:editor"), path)
            self.assertFalse(self.can(path), path)  # the public may not
            self.assertFalse(self.can(path, "group:somebody-else"), path)

    def test_editors_cannot_touch_users_or_groups(self):
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

    def test_admins_can_do_all_of_it(self):
        for path in (
            "/test_repo/person/1/edit",
            "/test_repo/user/1/edit",
            "/test_repo/user/add",
            "/test_repo/group/1/edit",
            "/test_repo/group/add",
        ):
            self.assertTrue(self.can(path, "group:admin"), path)


class SpecialPermissionsTest(PermissionsCase):
    """`super_edit` is for users, `manage` for groups, and both can be granted on their
    own."""

    def acls(self, **extra):
        acls = dict(resources.TYPE_ACLS)
        for cls, entries in extra.items():
            acls[getattr(uri, cls)] = entries
        return mock.patch.object(resources, "TYPE_ACLS", acls)

    def test_super_edit_alone_opens_the_users_and_nothing_else(self):
        grant = [
            (Allow, "group:useradmin", ["view", "super_edit"]),
            *resources.TYPE_ACLS[uri.USER_CLASS],
        ]
        with self.acls(USER_CLASS=grant):
            self.assertTrue(self.can("/test_repo/user/1/edit", "group:useradmin"))
            self.assertTrue(self.can("/test_repo/user/add", "group:useradmin"))
            self.assertFalse(self.can("/test_repo/group/1/edit", "group:useradmin"))
            self.assertFalse(
                self.can("/test_repo/person/1/edit", "group:useradmin")
            )  # not `edit`
            self.assertFalse(self.can("/test_repo/manage", "group:useradmin"))

    def test_edit_is_not_enough_for_users(self):
        # even where an ACL gives editors everything on the users' type, the edit view
        # of a
        # user asks for super_edit, which `edit` does not include
        grant = [
            (Allow, "group:editor", ["view", "edit", "add"]),
            *resources.TYPE_ACLS[uri.USER_CLASS],
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
            *resources.TYPE_ACLS[uri.GROUP_CLASS],
        ]
        with self.acls(GROUP_CLASS=grant):
            self.assertTrue(self.can("/test_repo/group/1/edit", "group:groupadmin"))
            self.assertTrue(self.can("/test_repo/group/add", "group:groupadmin"))
            self.assertFalse(self.can("/test_repo/user/1/edit", "group:groupadmin"))

    def test_an_extension_can_name_its_own_permissions(self):
        class Person(resources.Record):
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

    def test_users_and_groups_are_closed_to_the_public_and_to_everybody_else(self):
        for path in self.PRIVATE:
            for who in ((), ("group:editor",), ("user:someone",), ("group:other",)):
                self.assertFalse(self.can(path, *who), (path, who))
            self.assertTrue(self.can(path, "group:admin"), path)

    def test_the_type_records_that_describe_them_are_closed_too(self):
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

    def test_the_type_list_does_not_name_them_to_the_public(self):
        public = self.client().get("/test_repo/type", headers=as_()).json["records"]
        self.assertEqual(public, [2, 5])  # Type itself and Person
        admin = (
            self.client()
            .get("/test_repo/type", headers=as_("group:admin"))
            .json["records"]
        )
        self.assertEqual(admin, [1, 2, 3, 4, 5])

    def test_a_record_cannot_open_itself_up(self):
        """Whatever a record of a private type says about itself, its own ACL stays
        closed."""
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:user:1")
            public = ox.Quad(
                ox.NamedNode("rec:user:1"),
                ox.NamedNode(uri.GRACIA_NS + "public"),
                ox.Literal("true"),
                ox.DefaultGraph(),
            )
            data.add(public)
            store.put(data)
        self.assertFalse(self.can("/test_repo/user/1"))
        self.assertFalse(self.can("/test_repo/user/1", "group:editor"))

    def test_the_repository_itself_and_ordinary_types_stay_public(self):
        self.assertTrue(self.can("/test_repo/"))
        self.assertTrue(self.can("/test_repo/person"))
        self.assertTrue(self.can("/test_repo/person/1"))


class ManagersTest(PermissionsCase):
    """Managers do what editors do, and also add and change users. Groups stay with
    admins."""

    def test_managers_edit_and_add_like_editors(self):
        for path in ("/test_repo/person/1/edit", "/test_repo/person/add"):
            self.assertTrue(self.can(path, "group:manager"), path)

    def test_managers_add_and_change_users(self):
        for path in (
            "/test_repo/user",
            "/test_repo/user/1",
            "/test_repo/user/1/edit",
            "/test_repo/user/add",
        ):
            self.assertTrue(self.can(path, "group:manager"), path)

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

    def test_editors_still_cannot_touch_users(self):
        for path in (
            "/test_repo/user",
            "/test_repo/user/1/edit",
            "/test_repo/user/add",
        ):
            self.assertFalse(self.can(path, "group:editor"), path)

    def test_a_manager_can_make_a_user_but_not_a_group(self):
        web = self.client()
        form = web.get("/test_repo/user/add", headers=as_("group:manager")).forms[0]
        form["userid"], form["password"] = "made-by-manager", "long enough pw"
        self.assertEqual(
            form.submit("save", headers=as_("group:manager")).status_int, 302
        )
        self.assertEqual(self.can("/test_repo/group/add", "group:manager"), False)


class TheClosedNodesDoNotAnswerForUnknownThingsTest(PermissionsCase):
    """404 for what is not there and 403 for what is would list the users to the
    public."""

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

    def test_for_those_who_may_see_them_unknown_is_still_404(self):
        self.assertEqual(self.status("/test_repo/user/99", "group:admin"), 404)
        self.assertEqual(self.status("/test_repo/user/99", "group:manager"), 404)
        self.assertEqual(self.status("/test_repo/group/99", "group:admin"), 404)

    def test_public_things_are_404_for_everybody(self):
        self.assertEqual(self.status("/test_repo/person/99"), 404)
        self.assertEqual(self.status("/test_repo/nope"), 404)
        self.assertEqual(self.status("/nope/"), 404)
