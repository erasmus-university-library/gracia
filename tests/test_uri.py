import unittest

from gracia import uri
from gracia.uri import NS


class NSTest(unittest.TestCase):
    def test_attribute_and_item_make_the_same_plain_string_and_dunder_names_are_not_terms(
        self,
    ):
        ns = NS("http://example.org/ns/")
        self.assertEqual(ns.label, "http://example.org/ns/label")
        self.assertEqual(ns["label"], ns.label)
        self.assertIs(type(ns.label), str)
        self.assertEqual(str(ns), "http://example.org/ns/")

        ns = NS("http://example.org/ns/")
        self.assertEqual(ns.title, "http://example.org/ns/title")
        self.assertEqual(ns["class"], "http://example.org/ns/class")

        with self.assertRaises(AttributeError):
            NS("x:").__deepcopy__  # noqa: B018


class UriModuleTest(unittest.TestCase):
    def test_the_terms_are_the_ones_that_are_stored(self):
        self.assertEqual(uri.LABEL, "https://gracia.pub/ns/label")
        self.assertEqual(uri.RESOURCE, "https://gracia.pub/ns/resource")
        self.assertEqual(uri.RESOURCE_URI, "https://gracia.pub/ns/resource_uri")
        self.assertEqual(uri.CLASS, "https://gracia.pub/ns/class")
        self.assertEqual(
            uri.RDF_TYPE, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
        )
