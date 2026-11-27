import shutil
import tempfile
import unittest

import pyoxigraph as ox
from harness import BUNDLED, ConfiguredCase, open_application

from gracia import forms
from gracia.graph import Graph
from gracia.utils import assets

SCHEMA = "https://schema.org/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
SCHEMAORG = {
    "collection": "https://schema.org/Collection",
    "organization": "https://schema.org/Organization",
    "series": "https://schema.org/CreativeWorkSeries",
    "work": "https://schema.org/CreativeWork",
}
PARENTS = """
<rec:organization:1> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/organization/1> ;
  gracia:resource [ a schema:Organization ; schema:name "University" ] .
<rec:organization:2> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/organization/2> ;
  gracia:resource [ a schema:Organization ; schema:name "Library" ;
    schema:parentOrganization <ark:/99999/ir/organization/1> ] .
<rec:organization:3> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/organization/3> ;
  gracia:resource [ a schema:Organization ; schema:name "Archive" ;
    schema:parentOrganization [ a schema:Organization ; schema:name "Library" ;
      schema:parentOrganization [ a schema:Organization ; schema:name "University" ] ] ] .
<rec:organization:4> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/organization/4> ;
  gracia:resource [ a schema:Organization ; schema:name "Nameless parent" ;
    schema:parentOrganization [ a schema:Organization ] ] .
<rec:collection:1> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/collection/1> ;
  gracia:resource [ a schema:Collection ; schema:name "Theses" ] .
<rec:collection:2> a gracia:Record ; gracia:resource_uri <ark:/99999/ir/collection/2> ;
  gracia:resource [ a schema:Collection ; schema:name "Theses 2026" ;
    schema:publisher <ark:/99999/ir/organization/1> ;
    schema:isPartOf <ark:/99999/ir/collection/1> ] .
"""


class TypesTest(unittest.TestCase):
    def test_the_schemaorg_types_are_registered_each_with_a_shape_for_its_class(self):
        for slug, class_uri in SCHEMAORG.items():
            with self.subTest(slug=slug):
                content_type = BUNDLED[slug]
                self.assertEqual(content_type.class_uri, class_uri)
                self.assertEqual(content_type.name, slug.capitalize())
                path = assets.resolve(
                    assets.schema_spec(
                        content_type.assets, content_type.schema, content_type.slug
                    )
                )
                assets.load_shape(path, class_uri)  # a ValueError when there is none

    def test_the_name_is_the_label_and_comes_first_and_everything_has_a_name_and_order(
        self,
    ):
        sh_node_shape = ox.NamedNode("http://www.w3.org/ns/shacl#NodeShape")
        rdf_type = ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
        order = "http://www.w3.org/ns/shacl#order"
        for slug, class_uri in {
            **SCHEMAORG,
            "person": "https://schema.org/Person",
        }.items():
            with self.subTest(slug=slug):
                spec = f"gracia:assets/shacl/{slug}-schemaorg-01.ttl"
                data, node = assets.load_shape(assets.resolve(spec), class_uri)
                shapes = [
                    q.subject
                    for q in data.dataset.quads_for_object(sh_node_shape)
                    if q.predicate == rdf_type
                ]
                self.assertTrue(shapes)
                labels = [
                    (role, path)
                    for role, path, _place in forms.property_roles(
                        data, node, class_uri
                    )
                    if role == "label"
                ]
                self.assertEqual(
                    labels, [("label", SCHEMA + "name")]
                )  # only the main shape has one
                for shape in shapes:
                    self.assertTrue(
                        data.value(shape, "http://www.w3.org/ns/shacl#name"), shape
                    )  # the label of its class
                    for path, prop in forms.properties(data, shape):
                        if path.startswith(SCHEMA):
                            self.assertNotEqual(
                                forms.property_title(data, prop, path), path
                            )
                    found = [
                        (path, data.value(prop, order))
                        for path, prop in forms.properties(data, shape)
                        if path.startswith(SCHEMA)
                    ]
                    numbers = [float(n.value) for _path, n in found]  # an order each
                    self.assertEqual(numbers, sorted(set(numbers)), shape)  # in order
                # (the type of a resource that can have several is before the name)
                first = [
                    path
                    for path, _prop in forms.properties(data, node)
                    if path != RDF_TYPE
                ]
                self.assertEqual(first[0], SCHEMA + "name")


class RecursiveShapesTest(ConfiguredCase):
    def test_types_whose_shapes_refer_to_themselves_can_be_added_and_used_and_end(self):
        # an organization has a parent organization, a collection a parent collection
        self.application.create_repository(
            "test_repo", content_types=[BUNDLED[slug] for slug in SCHEMAORG]
        )
        repository = self.application["test_repo"]
        for slug in SCHEMAORG:
            with self.subTest(slug=slug):
                res_type = repository.types[slug]
                self.assertEqual(
                    res_type.property_role_paths("label"), [SCHEMA + "name"]
                )
                self.assertTrue(res_type.sql_hooks is not None)
                fields = res_type.blank_record().fields
                self.assertIn(SCHEMA + "name", [f.predicate for f in fields])
        # the paths and names of a shape that refers to itself end
        data, node = assets.load_shape(
            assets.resolve("gracia:assets/shacl/organization-schemaorg-01.ttl"),
            SCHEMAORG["organization"],
        )
        self.assertIn(SCHEMA + "parentOrganization", forms.property_paths(data, node))
        self.assertIn(SCHEMA + "name", forms.property_names(data, node))

    def put(self, text: str):
        record_graph = Graph(
            ox.Dataset(
                ox.parse(
                    (
                        "@prefix gracia: <https://gracia.pub/ns/> .\n"
                        "@prefix schema: <https://schema.org/> .\n" + text
                    ).encode(),
                    format=ox.RdfFormat.TURTLE,
                )
            )
        )
        self.application["test_repo"].store.put_multi(record_graph)

    def test_a_parent_is_a_link_or_a_node_written_in_the_record(self):
        self.application.create_repository(
            "test_repo", content_types=[BUNDLED[slug] for slug in SCHEMAORG]
        )
        self.config.commit()
        self.put(PARENTS)
        repository = self.application["test_repo"]
        for slug, number in (
            ("organization", "1"),
            ("organization", "2"),
            ("organization", "3"),
            ("collection", "1"),
            ("collection", "2"),
        ):
            with self.subTest(record=f"{slug}:{number}"):
                self.assertEqual(
                    repository.types[slug].find_record(number).validate(), []
                )
        self.assertTrue(repository.types["organization"].find_record("4").validate())


class TwoApplicationsTest(unittest.TestCase):
    def test_two_applications_in_a_process_can_share_a_cache_directory(self):
        # a command opens more than one (the store and the repository of an import):
        # reading the types must not open the LMDB stores of the cache, which a
        # process can have open once
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        db = f"{directory}/gracia.db"
        first = open_application(self, db, cache_dir=f"{directory}/cache")
        first.create_repository("test_repo")
        second = open_application(self, db, cache_dir=f"{directory}/cache")
        self.assertIn("repository", first["test_repo"].types)
        self.assertIn("repository", second["test_repo"].types)
