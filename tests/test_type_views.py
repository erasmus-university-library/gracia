"""Views of one type: a subclass of `Type` for a slug, and a view registered for it."""

from harness import BUNDLED
from test_manage import ADMIN, ManageCase

from gracia.main import make_config
from gracia.types.type import Type, TypeView

JSON = {**ADMIN, "Accept": "application/json"}


class PersonType(Type):
    slug = "person"


class PersonTypeView(TypeView):
    def as_json(self) -> dict:
        return {"mine": True, "type": self.context.name}


class TypeViewTest(ManageCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository(
            "lib",
            admin=("admin", "correct horse battery"),
            content_types=[BUNDLED["person"], BUNDLED["organization"]],
        )

    def web(self, with_view: bool = True):
        config = make_config(self.settings)
        if with_view:
            config.add_gracia_type_node(PersonType)  # what the scan does
            config.add_view(
                PersonTypeView,
                attr="as_json",
                context=PersonType,
                request_method="GET",
                accept="application/json",
                renderer="json",
            )
        return self.client(config)

    def test_the_type_with_a_class_of_its_own_has_its_view_and_the_others_have_the_generic_one(
        self,
    ):
        web = self.web()
        mine = web.get("/lib/person", headers=JSON).json
        self.assertEqual(mine, {"mine": True, "type": "Person"})
        other = web.get("/lib/organization", headers=JSON).json
        self.assertEqual(
            other["slug"], "organization"
        )  # the generic `TypeView.as_json`

    def test_without_the_registration_the_generic_view_serves(self):
        found = self.web(with_view=False).get("/lib/person", headers=JSON).json
        self.assertEqual(found["slug"], "person")

    def test_the_node_of_the_type_is_the_class_that_was_registered_for_its_slug(self):
        config = make_config(self.settings)
        config.add_gracia_type_node(PersonType)
        config.commit()
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        self.assertIsInstance(repository.types["person"], PersonType)
        self.assertIs(type(repository.types["organization"]), Type)
