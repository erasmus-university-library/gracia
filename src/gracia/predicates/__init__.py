from dataclasses import dataclass

import colander
import pyoxigraph as ox
import venusian
from pyramid.threadlocal import get_current_registry

from gracia.interfaces import IPredicate
from gracia.store import literal

_N = ox.NamedNode


def read_text(record, subject, predicate_uri: str) -> str | None:
    """What a property says now, as text (the stored value of a property with no
    predicate)."""
    found = record.graph.value(subject, predicate_uri) if subject is not None else None
    return found.value if found is not None else None


def write_text(
    record, subject, predicate_uri: str, text: str | None, as_uri: bool = False
) -> None:
    """Store text as the one value of a property, a URI or a literal (nothing for an
    empty text)."""
    term = (_N(text) if as_uri else ox.Literal(text)) if text else None
    record.replace(subject, predicate_uri, term)


class Predicate:
    """The base class: every part is optional (see the module)."""

    uri: str | None = None

    def schema_node(
        self, record, request, schema_node=None
    ) -> colander.SchemaNode | None:
        return schema_node

    def sibling_validator(self, nodes: list):
        """A validator for the mapping this property's node is in, or None: `nodes` are
        all the nodes of that mapping (their `predicate` says which property each is
        for). It is called as `validator(mapping_node, value)` and raises
        `colander.Invalid`; an error set on a child (`error["name"] = message`) shows by
        that field. This is for a rule that is about several properties together, such
        as a name or the two parts it is made of."""
        return None

    def on_write_sql(self, context: "HookContext") -> list[str] | None:
        """The SQL that makes what is stored for the property this is the code for (see
        the module); None, or nothing, to leave the rows as they are."""
        return None


@dataclass
class HookContext:
    """What `on_write_sql` is told: the repository, the type whose records the SQL is
    for, and the property."""

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
        """The SQL condition that the subject `s` is a record of the type."""
        return f"starts_with(s, {literal('rec:' + self.type.slug + ':')})"  # ty: ignore[missing-attribute]


def predicate(predicate_uri: str):
    """Class decorator: the class is the code for the property `predicate_uri` (found by
    `config.scan`)."""

    def decorate(cls):
        cls.uri = predicate_uri

        def callback(scanner, _name, _obj):
            scanner.config.add_gracia_predicate(cls)

        venusian.attach(cls, callback, category="pyramid")
        return cls

    return decorate


def add_gracia_predicate(config, cls: type) -> None:
    """Config directive: register an instance of `cls` under the URI of its property.
    Two for one property are a conflict, and one of the application replaces one of an
    include."""

    def register():
        config.registry.registerUtility(cls(), IPredicate, name=cls.uri)  # ty: ignore[missing-attribute]

    config.action(("gracia predicate", cls.uri), register)  # ty: ignore[missing-attribute]


def find(predicate_uri: str) -> Predicate | None:
    """The predicate for a property, when there is one."""
    return get_current_registry().queryUtility(IPredicate, name=predicate_uri)


def all_predicates() -> list[Predicate]:
    return [p for _name, p in get_current_registry().getUtilitiesFor(IPredicate)]
