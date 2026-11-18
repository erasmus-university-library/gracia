import unittest

import pyoxigraph as ox
from support import validate

from gracia import forms
from gracia.utils import assets

FOAF = "http://xmlns.com/foaf/0.1/"
SHAPES = "gracia.types:person/shacl/"


def person(**props) -> ox.Dataset:
    """A foaf:Person with the given properties: text for a string, `<...>` for a URI."""
    lines = [
        "@prefix foaf: <http://xmlns.com/foaf/0.1/> .",
        "<urn:p:1> a foaf:Person ;",
    ]
    for name, value in props.items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            lines.append(
                f"  foaf:{name} "
                f"{v if v.startswith('<') else repr(v).replace(chr(39), chr(34))} ;"
            )
    lines[-1] = lines[-1][:-1] + "."
    return ox.Dataset(ox.parse("\n".join(lines).encode(), format=ox.RdfFormat.TURTLE))


def conforms(profile: str, **props) -> bool:
    path = assets.resolve(f"{SHAPES}Person-{profile}-01.ttl")
    return validate(person(**props), path).conforms


GOOD = {"name": "Ada Lovelace", "givenName": "Ada", "familyName": "Lovelace"}


class ProfilesTest(unittest.TestCase):
    def test_both_profiles_are_there_and_have_a_shape_for_foaf_person(self):
        for profile in ("minimal", "full"):
            with self.subTest(profile=profile):
                path = assets.resolve(f"{SHAPES}Person-{profile}-01.ttl")
                assets.load_shape(
                    path, f"{FOAF}Person"
                )  # a ValueError when there is none
        self.assertEqual(
            assets.schemas_of("gracia.types:person/"),
            ["Person-full-01.ttl", "Person-minimal-01.ttl"],
        )

    def test_a_complete_person_follows_both(self):
        self.assertTrue(conforms("minimal", **GOOD))
        self.assertTrue(conforms("full", **GOOD, nick="ada", title="Countess"))

    def test_the_full_profile_is_closed_and_the_minimal_is_not(self):
        # foaf:age is a property neither profile describes
        self.assertTrue(conforms("minimal", **GOOD, age="7"))
        self.assertFalse(conforms("full", **GOOD, age="7"))

    def test_a_name_or_both_parts_it_is_made_of_and_never_two_names(self):
        for profile in ("minimal", "full"):
            with self.subTest(profile=profile):
                # the write hook of foaf:name makes the name of the two parts
                self.assertTrue(
                    conforms(profile, givenName="Ada", familyName="Lovelace")
                )
                self.assertFalse(
                    conforms(profile, **{**GOOD, "name": ["Ada", "Lady Ada"]})
                )
                self.assertFalse(conforms(profile, **{**GOOD, "name": ""}))
                self.assertFalse(conforms(profile))  # nothing
        self.assertFalse(conforms("minimal", givenName="Ada"))  # one part is not a name
        self.assertFalse(conforms("minimal", familyName="Lovelace"))
        self.assertTrue(conforms("minimal", name="Ada Lovelace", givenName="Ada"))

    def test_the_places_a_person_can_be_reached_are_checked_and_may_repeat(self):
        ok = {
            "mbox": ["<mailto:ada@example.org>", "<mailto:a@work.example>"],
            "phone": "<tel:+31-20-5551234>",
            "homepage": "<https://example.org/ada>",
            "depiction": "<https://example.org/ada.jpg>",
            "knows": "<urn:p:2>",
        }
        self.assertTrue(conforms("full", **GOOD, **ok))
        for bad in (
            {"mbox": "<http://example.org/not-a-mailbox>"},
            {"mbox": "<mailto:no-at-sign>"},
            {"phone": "<mailto:ada@example.org>"},
            {"phone": "<tel:call-me>"},
            {"homepage": "<ftp://example.org/ada>"},
            {"depiction": "<file:///etc/passwd>"},
            {"knows": "somebody"},
        ):  # a string is not a URI
            with self.subTest(bad=bad):
                self.assertFalse(conforms("full", **GOOD, **bad))

    def test_the_edit_form_has_the_fields_that_are_one_plain_string_in_the_order_of_the_shape(  # noqa: E501
        self,
    ):
        path = assets.resolve(f"{SHAPES}Person-full-01.ttl")
        schema = forms.schema_from_shape(*assets.load_shape(path, f"{FOAF}Person"))
        self.assertEqual(
            [n.name for n in schema.children],
            ["name", "givenName", "familyName", "nick", "title"],
        )
        self.assertEqual(
            [n.title for n in schema.children][:3],
            ["Name", "Given name", "Family name"],
        )
        required = [n.name for n in schema.children if n.required]
        self.assertEqual(
            required, []
        )  # a name, or both parts: the shape says it, not a field
