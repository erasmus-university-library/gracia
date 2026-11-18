"""What the shapes say about a property besides how to validate it.

A property shape can carry, next to `sh:path`, two kinds of hint that SHACL ignores:

* a role, `dash:propertyRole` with `dash:LabelRole`, `dash:KeyInfoRole` or `dash:DescriptionRole`
  (the DASH vocabulary, http://datashapes.org/dash): the values of the properties that have the
  role make the label of a record (in the order of `sh:order`), its `identifiers`, and its
  `text`;
* `dash:uniqueValueForClass <class>`: only one record of the class may have a given value of
  this property (a userid). The database enforces it (`unique_values`).

`roles(shapes, shape, class_uri)` reads them from the shape of a type's records (the record shape
with the shape of the content embedded), including what is below `sh:node`.
"""

import pyoxigraph as ox

from gracia import forms
from gracia.graph import Graph

DASH = "http://datashapes.org/dash#"
PROPERTY_ROLE = DASH + "propertyRole"
UNIQUE_VALUE_FOR_CLASS = DASH + "uniqueValueForClass"
ROLES = {
    DASH + "LabelRole": "label",
    DASH + "KeyInfoRole": "key_info",
    DASH + "DescriptionRole": "description",
}


def paths(shapes: Graph, shape) -> set[str]:
    """Every property that `shape` describes, the ones below it (`sh:node`) too."""
    found: set[str] = set()
    for path, prop in forms.properties(shapes, shape):
        found.add(path)
        nested = forms.nested_shape(shapes, prop)
        if nested is not None:
            found |= paths(shapes, nested)
    return found


def names(shapes: Graph, shape) -> dict[str, str]:
    """What a person calls every property `shape` describes (`sh:name`), by property uri, the ones
    below `sh:node` too; a property that has no name is called by the last part of its uri."""
    found: dict[str, str] = {}

    def walk(node) -> None:
        for path, prop in forms.properties(shapes, node):
            title = forms.property_title(shapes, prop, path)
            found.setdefault(path, forms.local_name(path) if title == path else title)
            nested = forms.nested_shape(shapes, prop)
            if nested is not None:
                walk(nested)

    walk(shape)
    return found


def roles(shapes: Graph, shape, class_uri: str | None = None) -> list[tuple[str, str, int]]:
    """The hints of the properties of `shape`, the ones below it too, as (role, property uri,
    place): the outer properties first, each level in `sh:order`; `place` counts them. `role` is
    `label`, `key_info`, `description` or, for `dash:uniqueValueForClass <class_uri>`, `unique`."""
    found: list[tuple[str, str, int]] = []

    def walk(node) -> None:
        for path, prop in forms.properties(shapes, node):
            place = len(found)
            for role in shapes.objects(prop, PROPERTY_ROLE):
                if isinstance(role, ox.NamedNode) and role.value in ROLES:
                    found.append((ROLES[role.value], path, place))
            if class_uri is not None:
                for target in shapes.objects(prop, UNIQUE_VALUE_FOR_CLASS):
                    if isinstance(target, ox.NamedNode) and target.value == class_uri:
                        found.append(("unique", path, place))
            nested = forms.nested_shape(shapes, prop)
            if nested is not None:
                walk(nested)

    walk(shape)
    return found
