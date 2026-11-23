from dataclasses import dataclass

import colander
import pyoxigraph as ox
import venusian

from gracia.interfaces import IPredicate
from gracia.store import literal

_N = ox.NamedNode


def read_text(record, subject, predicate_uri: str) -> str | None:
    found = record.graph.value(subject, predicate_uri) if subject is not None else None
    return found.value if found is not None else None


def write_text(
    record, subject, predicate_uri: str, text: str | None, as_uri: bool = False
) -> None:
    term = (_N(text) if as_uri else ox.Literal(text)) if text else None
    record.graph.replace(subject, predicate_uri, term)


class Predicate:
    uri: str | None = None

    def schema_node(
        self, record, request, schema_node=None
    ) -> colander.SchemaNode | None:
        return schema_node

    def sibling_validator(self, nodes: list):
        return None

    def on_write_sql(self, context: "HookContext") -> list[str] | None:
        return None


@dataclass
class HookContext:
    repository: object
    type: object
    path: str  # the property
    flat: str = (
        "@FLAT@"  # the rows of the batch; the store puts the name of the table here
    )
    # the stored rows, as they are before the batch (the store names it)
    triples: str = "@TRIPLES@"

    @property
    def of_type(self) -> str:
        return f"starts_with(s, {literal('rec:' + self.type.slug + ':')})"  # ty: ignore[missing-attribute]


def predicate(predicate_uri: str):

    def decorate(cls):
        cls.uri = predicate_uri

        def callback(scanner, _name, _obj):
            scanner.config.add_gracia_predicate(cls)

        venusian.attach(cls, callback, category="pyramid")
        return cls

    return decorate


def add_gracia_predicate(config, cls: type) -> None:

    def register():
        config.registry.registerUtility(cls(), IPredicate, name=cls.uri)  # ty: ignore[missing-attribute]

    config.action(("gracia predicate", cls.uri), register)  # ty: ignore[missing-attribute]


def find(registry, predicate_uri: str) -> Predicate | None:
    return registry.queryUtility(IPredicate, name=predicate_uri)


def all_predicates(registry) -> list[Predicate]:
    return [p for _name, p in registry.getUtilitiesFor(IPredicate)]
