from gracia import uri
from gracia.forms import ReadOnlyWidget
from gracia.predicates import HookContext, Predicate, predicate
from gracia.store import literal


@predicate(uri.LABEL)
class LabelPredicate(Predicate):
    """`gracia:label`: a short text for a record, made from its label values on save."""

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
