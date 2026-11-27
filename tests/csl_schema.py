"""A check of CSL JSON against the schema in data/csl-data.json.

It knows the keywords that schema uses (`$ref`, `type`, `enum`, `required`,
`properties`, `additionalProperties`, `items`, `anyOf`, `minItems`, `maxItems`), and
no others: it says so when the schema has one it does not know, so that it cannot
pass what it does not check.
"""

import json
from pathlib import Path

CHECKED = {
    "$ref",
    "type",
    "enum",
    "required",
    "properties",
    "definitions",
    "additionalProperties",
    "items",
    "anyOf",
    "minItems",
    "maxItems",
}
# what says what a thing is, and not what it must be
NOTES = {
    "$id",
    "$schema",
    "description",
    "title",
    "examples",
    "metadata-double-checked",
    "other-ids",
    "short_id",
}
TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


DATA = Path(__file__).parent.parent / "data" / "csl-data.json"
SCHEMA = json.loads(DATA.read_text())


def _resolve(ref: str) -> dict:
    node = SCHEMA
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def check(value, schema, where="$") -> list[str]:
    """What is wrong with the value, as lines (none: it is right)."""
    unknown = set(schema) - CHECKED - NOTES
    if unknown:
        raise NotImplementedError(f"{where}: the schema has {sorted(unknown)}")
    if "$ref" in schema:
        return check(value, _resolve(schema["$ref"]), where)
    problems = []
    if "anyOf" in schema:
        tried = [check(value, option, where) for option in schema["anyOf"]]
        if all(tried):
            problems.append(f"{where}: is none of the alternatives: {tried[0][:2]}")
    if "type" in schema:
        wanted = schema["type"]
        wanted = wanted if isinstance(wanted, list) else [wanted]
        if not any(TYPES[t](value) for t in wanted):
            return [*problems, f"{where}: is {type(value).__name__}, not {wanted}"]
    if "enum" in schema and value not in schema["enum"]:
        allowed = len(schema["enum"])
        problems.append(f"{where}: {value!r} is not one of the {allowed} allowed")
    if isinstance(value, dict):
        problems += _object(value, schema, where)
    if isinstance(value, list):
        problems += _array(value, schema, where)
    return problems


def _object(value: dict, schema: dict, where: str) -> list[str]:
    problems = [
        f"{where}: lacks {n}" for n in schema.get("required", []) if n not in value
    ]
    props = schema.get("properties", {})
    for name, item in value.items():
        if name in props:
            problems += check(item, props[name], f"{where}.{name}")
        elif schema.get("additionalProperties") is False:
            problems.append(f"{where}: has {name}, which the schema does not")
    return problems


def _array(value: list, schema: dict, where: str) -> list[str]:
    problems = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        problems.append(f"{where}: has fewer than {schema['minItems']} items")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        problems.append(f"{where}: has more than {schema['maxItems']} items")
    if "items" in schema:
        for index, item in enumerate(value):
            problems += check(item, schema["items"], f"{where}[{index}]")
    return problems


def validate(items: list) -> list[str]:
    """What is wrong with a list of CSL items."""
    return check(items, SCHEMA)
