import colander
import deform
import deform.widget

from gracia import uri
from gracia.predicates import HookContext, Predicate, predicate
from gracia.store import literal


@predicate(uri.IS_PUBLISHED)
class IsPublishedPredicate(Predicate):
    def schema_node(self, record, request, schema_node=None):
        if schema_node is None:
            return schema_node
        node = colander.SchemaNode(
            colander.String(),
            name="is_published",
            title=schema_node.title,
            description=schema_node.description,
            missing="false",
            widget=deform.widget.CheckboxWidget(true_val="true", false_val="false"),
        )
        node.initial = "true"
        return node

    def on_write_sql(self, context: HookContext) -> list[str]:
        flat, path = context.flat, literal(uri.IS_PUBLISHED)
        work = f"{flat}_published"
        return [
            f"""CREATE OR REPLACE TABLE {work} AS
                SELECT r.s, coalesce(bool_or(lower(trim(f.lit)) IN ('true', '1')), true)
                  AS published
                FROM (SELECT DISTINCT s FROM {flat} WHERE {context.of_type}) r
                LEFT JOIN {flat} f ON f.s = r.s AND len(f.loc) = 0 AND f.p = {path}
                GROUP BY r.s""",
            f"DELETE FROM {flat} WHERE {context.of_type} AND len(loc) = 0 AND p = "
            f"{path}",
            f"""INSERT INTO {flat}
                SELECT s, []::VARCHAR[], {path}, NULL, CAST(published AS VARCHAR), NULL,
                  {literal(uri.XSD_NS.boolean)}
                FROM {work}""",
            f"DROP TABLE IF EXISTS {work}",
        ]
