import re

import pyoxigraph as ox
from pyramid.exceptions import ConfigurationConflictError
from support import must_search
from test_edit import EditCase

from gracia import predicates, uri
from gracia.main import make_config
from gracia.predicates import (
    Predicate,
    gracia_label,
    gracia_resource_uri,
    gracia_schema_template,
    predicate,
)
from gracia.utils import assets

FOAF = "http://xmlns.com/foaf/0.1/"
SH = "http://www.w3.org/ns/shacl#"


@predicate(FOAF + "mbox")
class MboxPredicate(Predicate):
    """The code for a property of the content of a person: another description, and what
    is stored is the address in lower case."""

    def schema_node(self, record, request, schema_node=None):
        schema_node.description = "in lower case"  # ty: ignore[invalid-assignment]
        return schema_node

    def on_write_sql(self, context):
        return [
            f"UPDATE {context.flat} SET lit = lower(lit) WHERE {context.of_type} AND p "
            "= "
            f"'{FOAF}mbox'"
        ]


class PredicateCase(EditCase):
    predicates = ()

    def setUp(self):
        super().setUp()
        self.config = make_config(self.settings)
        for cls in self.predicates:
            self.config.add_gracia_predicate(cls)
        self.config.commit()

    def repo(self):
        self.config.begin()
        self.addCleanup(self.config.end)
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        return node

    def page(self, path="/test_repo/person/1/edit"):
        return self.client(self.config).get(path, headers=self.admin)


class RegistryTest(PredicateCase):
    def test_a_predicate_is_registered_under_the_uri_of_its_property(self):
        self.config.begin()
        self.addCleanup(self.config.end)
        for property_uri, module in (
            (uri.RESOURCE_URI, gracia_resource_uri),
            (uri.SCHEMA_TEMPLATE, gracia_schema_template),
            (uri.LABEL, gracia_label),
        ):
            self.assertEqual(
                type(predicates.find(property_uri)).__module__, module.__name__
            )
        self.assertIsNone(predicates.find(FOAF + "mbox"))  # nothing for it
        self.assertIsNone(predicates.find(uri.NAME))

    def test_two_predicates_for_one_property_are_a_conflict_at_commit(self):
        class One(Predicate):
            uri = FOAF + "nick"

        class Two(Predicate):
            uri = FOAF + "nick"

        config = make_config(self.settings)
        config.add_gracia_predicate(One)
        config.add_gracia_predicate(Two)
        with self.assertRaises(ConfigurationConflictError):
            config.commit()

    def test_the_application_can_replace_one_that_comes_with_gracia(self):
        class Mine(Predicate):
            uri = uri.LABEL

        config = make_config(self.settings)
        config.add_gracia_predicate(Mine)
        config.commit()
        config.begin()
        self.addCleanup(config.end)
        self.assertIsInstance(predicates.find(uri.LABEL), Mine)


class RecordShapeTest(PredicateCase):
    def test_the_shape_of_a_record_embeds_the_shape_of_its_content_on_gracia_resource(
        self,
    ):
        self.config.begin()
        self.addCleanup(self.config.end)
        data, node = self.repo()["person"].record_shape()
        self.assertEqual(node.value, uri.RECORD_SHAPE)
        properties = {
            data.objects(prop, ox.NamedNode(SH + "path"))[0].value: prop
            for prop in data.objects(node, ox.NamedNode(SH + "property"))
        }
        self.assertEqual(
            set(properties),
            {uri.RESOURCE_URI, uri.LABEL, uri.SLUG, uri.RESOURCE}
            | {uri.IS_PUBLISHED, uri.CREATED_AT, uri.MODIFIED_AT},
        )  # what every record has: and nothing else (the shape is closed)
        embedded = data.objects(properties[uri.RESOURCE], ox.NamedNode(SH + "node"))
        self.assertEqual(len(embedded), 1)  # the shape of a person, on gracia:resource
        classes = data.objects(embedded[0], ox.NamedNode(SH + "targetClass"))
        self.assertEqual([c.value for c in classes], [FOAF + "Person"])
        required = data.objects(properties[uri.RESOURCE], ox.NamedNode(SH + "class"))
        self.assertEqual(
            [c.value for c in required], [FOAF + "Person"]
        )  # and it has to be one

    def test_a_type_without_a_shape_still_has_the_record_shape_and_its_class(self):
        data, node = assets.record_shape(None, FOAF + "Person")
        properties = data.objects(node, ox.NamedNode(SH + "property"))
        self.assertEqual(len(properties), 7)
        classes = [
            c for p in properties for c in data.objects(p, ox.NamedNode(SH + "class"))
        ]
        self.assertEqual([c.value for c in classes], [FOAF + "Person"])

    def test_the_fields_follow_sh_order_with_the_content_on_a_tab_of_its_own(self):
        record = self.repo()["person"]["1"]
        self.assertEqual(
            [c.name for c in record.edit_schema().children],
            [
                "resource_uri",
                "label",
                "slug",
                "resource",
                "is_published",
                "created_at",
                "modified_at",
            ],
        )
        tabs = re.findall(r'data-bs-toggle="tab"[^>]*>([^<]+)', self.page().text)
        self.assertEqual(
            tabs, ["Person", "Record"]
        )  # the content is a tab, loose fields another

    def test_every_node_knows_the_property_it_is_for(self):
        record = self.repo()["person"]["1"]
        schema = record.edit_schema()
        self.assertEqual(
            [c.predicate for c in schema.children],
            [
                uri.RESOURCE_URI,
                uri.LABEL,
                uri.SLUG,
                uri.RESOURCE,
                uri.IS_PUBLISHED,
                uri.CREATED_AT,
                uri.MODIFIED_AT,
            ],
        )
        content = schema["resource"]
        self.assertEqual(
            [c.predicate for c in content.children], [FOAF + "name", FOAF + "mbox"]
        )


class PropertyCodeTest(PredicateCase):
    predicates = (MboxPredicate,)

    def test_a_predicate_changes_the_field_the_shape_made_and_stores_the_value_itself(
        self,
    ):
        client = self.client(self.config)
        form = client.get("/test_repo/person/1/edit", headers=self.admin).forms[0]
        self.assertIn(
            "in lower case",
            client.get("/test_repo/person/1/edit", headers=self.admin).text,
        )
        form["mbox"] = "ADA@Example.ORG"
        self.assertEqual(form.submit("save", headers=self.admin).status_int, 302)
        self.assertIn((FOAF + "mbox", "ada@example.org"), self.values("rec:person:1"))

    def test_a_property_with_no_predicate_is_stored_as_it_is(self):
        client = self.client(self.config)
        form = client.get("/test_repo/person/1/edit", headers=self.admin).forms[0]
        form["name"] = "Ada KING"
        form.submit("save", headers=self.admin)
        self.assertIn((FOAF + "name", "Ada KING"), self.values("rec:person:1"))


class ResourceUriTest(PredicateCase):
    """`gracia:resource_uri`: all of its code is in
    `predicates/gracia_resource_uri.py`."""

    def test_a_new_record_gets_the_ark_from_the_predicate(self):
        client = self.client(self.config)
        form = client.get("/test_repo/person/add", headers=self.admin).forms[0]
        form["name"] = "Grace"
        form.submit("save", headers=self.admin)
        self.assertIn(
            (uri.RESOURCE_URI, "ark:/99999/ir/person/2"), self.values("rec:person:2")
        )

    def test_the_core_and_the_extension_have_no_code_for_it(self):
        from gracia import resources
        from gracia.extensions import ark

        self.assertFalse(hasattr(resources.Record, "default_resource_uri"))
        self.assertFalse(hasattr(ark, "ResourceUriPredicate"))


class SchemaTest(PredicateCase):
    """`gracia:schemaTemplate`: a select of the files of the directory `gracia:assets`
    names."""

    def test_the_type_record_offers_the_files_of_its_directory(self):
        page = self.page("/test_repo/person/manage").text
        select = must_search(r'<select name="schema".*?</select>', page, re.S)[0]
        self.assertEqual(
            re.findall(r'<option[^>]*value="([^"]+)"', select),
            ["Person-full-01.ttl", "Person-minimal-01.ttl"],
        )

    def test_the_other_fields_of_the_type_record_stay_text_fields(self):
        page = self.page("/test_repo/person/manage").text
        for name in ("name", "slug", "assets"):
            self.assertRegex(page, rf'<input type="text" name="{name}"')

    def test_the_predicate_is_the_one_for_gracia_schema_wherever_the_shape_has_it(self):
        self.config.begin()
        self.addCleanup(self.config.end)
        self.assertIsInstance(
            predicates.find(uri.SCHEMA_TEMPLATE),
            gracia_schema_template.SchemaTemplatePredicate,
        )


class RecordMark(Predicate):
    uri = uri.RESOURCE_URI

    def on_write_sql(self, context):
        return ["SELECT 1 /* record */"]


class ContentMark(Predicate):
    # a property the shape of a person has (the shape decides which hooks there are)
    uri = FOAF + "mbox"

    def on_write_sql(self, context):
        return ["SELECT 1 /* content */"]


class HookTest(PredicateCase):
    predicates = (RecordMark, ContentMark)

    def test_the_sql_of_the_content_comes_before_the_sql_of_the_record(self):
        statements = self.repo()["person"].sql_hooks()
        order = [m for s in statements for m in re.findall(r"/\* (\w+) \*/", s)]
        self.assertEqual(order, ["content", "record"])


class BatchHookTest(PredicateCase):
    """The SQL of a predicate works on all the records of a batch at once, and can be
    run again."""

    predicates = (MboxPredicate,)

    def people(self) -> ox.Dataset:
        text = "".join(
            f"<rec:person:{n}> a <{uri.RECORD_CLASS}> ; "
            f'<{uri.RESOURCE}> [ a <{FOAF}Person> ; <{FOAF}name> "P{n}" ; <{FOAF}mbox> '
            f'"A{n}@X.ORG" ] .\n'
            for n in (1, 2, 3)
        )
        return ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE))

    def test_every_record_of_the_batch_goes_through_the_sql_and_nothing_is_stored(self):
        node = self.repo()
        before = node.store.counts()
        done = node.with_hooks(self.people())
        mailboxes = {q.object.value for q in done if q.predicate.value == FOAF + "mbox"}
        self.assertEqual(mailboxes, {"a1@x.org", "a2@x.org", "a3@x.org"})
        labels = {q.object.value for q in done if q.predicate.value == uri.LABEL}
        self.assertEqual(labels, {"P1", "P2", "P3"})  # the predicates of gracia run too
        self.assertEqual(node.store.counts(), before)  # a trial leaves nothing behind

    def test_the_same_sql_again_gives_the_same_rows(self):
        node = self.repo()
        once = node.with_hooks(self.people())
        twice = node.with_hooks(once)
        # modified_at is the time of the run: a text that differs from what is stored is
        # modified
        # now

        def same(graph):
            return {str(q) for q in graph if q.predicate.value != uri.MODIFIED_AT}

        self.assertEqual(same(once), same(twice))


class SaveTest(PredicateCase):
    def test_saving_a_record_runs_the_hooks_whoever_saves_it(self):
        record = self.repo()["person"]["1"]
        record.save()
        self.assertIn((uri.LABEL, "Ada"), self.values("rec:person:1"))
        self.assertIn(
            (uri.RESOURCE_URI, "ark:/99999/ir/person/1"), self.values("rec:person:1")
        )
