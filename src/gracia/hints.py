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


def paths(shapes: Graph, shape, above: tuple = ()) -> set[str]:
    found: set[str] = set()
    for path, prop in forms.properties(shapes, shape):
        found.add(path)
        nested = forms.nested_shape(shapes, prop)
        if nested is not None and nested not in (*above, shape):  # a shape that
            found |= paths(shapes, nested, (*above, shape))  # refers to itself
    return found


def names(shapes: Graph, shape) -> dict[str, str]:
    found: dict[str, str] = {}

    def walk(node, above: tuple) -> None:
        for path, prop in forms.properties(shapes, node):
            title = forms.property_title(shapes, prop, path)
            found.setdefault(path, forms.local_name(path) if title == path else title)
            nested = forms.nested_shape(shapes, prop)
            if nested is not None and nested not in (*above, node):
                walk(nested, (*above, node))

    walk(shape, ())
    return found


def property_roles(
    shapes: Graph, shape, class_uri: str | None = None
) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []

    def walk(node, above: tuple) -> None:
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
            if nested is not None and nested not in (*above, node):
                walk(nested, (*above, node))

    walk(shape, ())
    return found
