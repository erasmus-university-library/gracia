import colander

from gracia import uri
from gracia.predicates import HookContext, Predicate, predicate
from gracia.store import literal


@predicate(uri.RESOURCE_URI)
class ResourceUriPredicate(Predicate):
    """`gracia:resource_uri` of a record: the name its resource is published under. It
    is required, and when nobody says another (an ORCID, a permanent URI) it is made
    when the record is saved: for now the ARK of the record, from the settings of the
    `ark` extension. Once it has one it keeps it."""

    def schema_node(self, record, request, schema_node=None):
        return colander.SchemaNode(
            colander.String(),
            name="resource_uri",
            title="Resource URI",
            description=(
                "The URI the resource is published under (an ORCID, a permanent URI); "
                "empty: the ARK of this record"
            ),
            missing="",
            validator=colander.Regex(
                r"^$|^[A-Za-z][A-Za-z0-9+.-]*:\S+$", msg="Not a URI"
            ),
        )

    def on_write_sql(self, context: HookContext) -> list[str]:
        settings = context.repository.extension("ark").settings  # ty: ignore[missing-attribute]
        ark = f"ark:/{settings['naan']}/{settings['root_prefix']}/{context.type.slug}/"  # ty: ignore[missing-attribute]
        flat, path = context.flat, literal(uri.RESOURCE_URI)
        return [
            f"""INSERT INTO {flat}
                SELECT r.s, []::VARCHAR[], {path}, {literal(ark)} || split_part(r.s,
                  ':', 3),
                       NULL, NULL, NULL
                FROM (SELECT DISTINCT s FROM {flat} WHERE {context.of_type}) r
                WHERE NOT EXISTS (SELECT 1 FROM {flat} f
                                  WHERE f.s = r.s AND len(f.loc) = 0 AND f.p =
                                    {path})"""
        ]
