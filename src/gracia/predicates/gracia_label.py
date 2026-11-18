from gracia import uri
from gracia.forms import ReadOnlyWidget
from gracia.predicates import HookContext, Predicate, predicate
from gracia.store import literal


@predicate(uri.LABEL)
class LabelPredicate(Predicate):
    """`gracia:label` of a record: a short text for it, used for its title in the forms.
    It is not edited: the field is read-only, and it is made from the content every time
    the record is saved: the values of the properties that have the role
    `dash:LabelRole` in the shape of its type, in the order of `sh:order`, separated by
    a space (of a property that has several, the shallowest one, then the first in
    alphabetical order). No such property, or no value: no label (a form calls such a
    record "(Unnamed)")."""

    def schema_node(self, record, request, schema_node=None):
        if schema_node is None:
            return schema_node
        schema_node.widget = ReadOnlyWidget()
        schema_node.validator = None
        return schema_node

    def on_write_sql(self, context: HookContext) -> list[str]:
        flat, label = context.flat, literal(uri.LABEL)
        paths = context.type.role_paths("label")  # ty: ignore[missing-attribute]
        clear = (
            f"DELETE FROM {flat} WHERE len(loc) = 0 AND p = {label} "
            f"AND {context.of_type}"
        )
        if not paths:
            return [clear]
        listed = "[" + ", ".join(literal(p) for p in paths) + "]"
        return [
            clear,
            f"""INSERT INTO {flat}
                SELECT s, []::VARCHAR[], {label}, NULL, label, NULL, NULL FROM (
                    SELECT s, trim(string_agg(v, ' ' ORDER BY place)) AS label FROM (
                        SELECT s, list_position({listed}, p) AS place,
                               arg_min(lit, (len(loc), loc, lit)) AS v
                        FROM {flat}
                        WHERE {context.of_type} AND p IN {listed} AND lit IS NOT NULL
                        GROUP BY s, p
                    ) GROUP BY s
                ) WHERE label <> ''""",
        ]
