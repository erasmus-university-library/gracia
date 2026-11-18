"""The code behind `gracia:` predicates.

The fields of a form come from SHACL: a record is described by the record shape
(`model/record.shacl.ttl`) with the shape of its content embedded on `gracia:resource`, and
`Record.edit_schema` makes one Colander node per property, in `sh:order`. A predicate is the code
for one property, registered under the URI of that property, and it is what a property needs that
SHACL cannot say: another widget, a validator that asks the database, a default that is computed,
a value that follows from the rest. A property with no predicate is a plain text field that is
stored as it is. Each predicate is a module of this package, named for it (`gracia_label.py`):

    @predicate(uri.RESOURCE_URI)
    class ResourceUriPredicate(Predicate):
        def schema_node(self, record, request, schema_node=None): ...  # its Colander node
        def on_write_sql(self, context): ...      # SQL that makes what is stored for the property

and `config.scan` finds it (or `config.add_gracia_predicate(cls)`). Every part is optional.
`schema_node` is given the node the shape made (`schema_node`, None when the shape's property is not one text value: a URI, a nested shape) and returns what the form should have, or None for no field. `sibling_validator(nodes)` can add a rule that is about several properties together (see `foaf_name`); what SHACL can say (`sh:or`) is said in the shape, and the validator is its counterpart in the form.

`on_write_sql` is how a value is made when a record is saved, whoever saves it (a form, an import):
it returns the SQL statements (a list of strings, or None) that the store runs on the rows of
the batch that is being stored, between flattening them and inserting them (`Store.load`). The rows
are in the table `@FLAT@`, with the columns of the triples table (`s, loc, p, o, lit, lang, dt`), and
a statement adds, replaces or removes rows of it; it can read the triples table (`@TRIPLES@`: what is
stored now, before the batch) too. One batch is
all the records at once, so the SQL works on sets, not on records. A statement has to give the same
answer when it is run twice on the same rows, and has to leave the records of other types alone
(`context.of_type` is the condition for the ones of its type). What is embedded runs first, so the
properties of the record follow from its content as it will be stored. SQL belongs here, in Python,
where it can be tested and versioned, and what a form edits is only what the SQL is made from
(roles on the property shapes, `hints.py`). A property with no predicate (or none with
`on_write_sql`) is read and written as plain text by `Record`, at the record or at its content,
wherever the shape has it.
"""

from dataclasses import dataclass

import colander
import pyoxigraph as ox
import venusian
from pyramid.threadlocal import get_current_registry

from gracia.interfaces import IPredicate
from gracia.store import literal

_N = ox.NamedNode


def read_text(record, subject, predicate_uri: str) -> str | None:
    """What a property says now, as text (the stored value of a property with no predicate)."""
    found = record.graph.value(subject, predicate_uri) if subject is not None else None
    return found.value if found is not None else None


def write_text(record, subject, predicate_uri: str, text: str | None, as_uri: bool = False) -> None:
    """Store text as the one value of a property, a URI or a literal (nothing for an empty text)."""
    term = (_N(text) if as_uri else ox.Literal(text)) if text else None
    record.replace(subject, predicate_uri, term)


class Predicate:
    """The base class: every part is optional (see the module)."""

    uri: str | None = None

    def schema_node(self, record, request, schema_node=None) -> colander.SchemaNode | None:
        return schema_node

    def sibling_validator(self, nodes: list):
        """A validator for the mapping this property's node is in, or None: `nodes` are all the
        nodes of that mapping (their `predicate` says which property each is for). It is called
        as `validator(mapping_node, value)` and raises `colander.Invalid`; an error set on a
        child (`error["name"] = message`) shows by that field. This is for a rule that is about
        several properties together, such as a name or the two parts it is made of."""
        return None

    def on_write_sql(self, context: "HookContext") -> list[str] | None:
        """The SQL that makes what is stored for the property this is the code for (see the
        module); None, or nothing, to leave the rows as they are."""
        return None


@dataclass
class HookContext:
    """What `on_write_sql` is told: the repository, the type whose records the SQL is for, and the
    property."""

    repository: object
    type: object
    path: str  # the property
    flat: str = "@FLAT@"  # the rows of the batch; the store puts the name of the table here
    triples: str = "@TRIPLES@"  # the stored rows, as they are before the batch (the store names it)

    @property
    def of_type(self) -> str:
        """The SQL condition that the subject `s` is a record of the type."""
        return f"starts_with(s, {literal('rec:' + self.type.slug + ':')})"


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
    """Config directive: register an instance of `cls` under the URI of its property. Two for one
    property are a conflict, and one of the application replaces one of an include."""

    def register():
        config.registry.registerUtility(cls(), IPredicate, name=cls.uri)

    config.action(("gracia predicate", cls.uri), register)


def find(predicate_uri: str) -> Predicate | None:
    """The predicate for a property, when there is one."""
    return get_current_registry().queryUtility(IPredicate, name=predicate_uri)


def all_predicates() -> list[Predicate]:
    return [p for _name, p in get_current_registry().getUtilitiesFor(IPredicate)]
