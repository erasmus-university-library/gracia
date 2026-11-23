import colander

from gracia.predicates import HookContext, Predicate, predicate
from gracia.store import literal

FOAF = "http://xmlns.com/foaf/0.1/"
GIVEN, FAMILY = FOAF + "givenName", FOAF + "familyName"


@predicate(FOAF + "name")
class FoafNamePredicate(Predicate):
    """`foaf:name`: the name as shown; made of the given and family name when empty."""

    def sibling_validator(self, nodes):
        by_property = {getattr(node, "predicate", None): node for node in nodes}
        name = by_property.get(FOAF + "name")
        if name is None:
            return None
        has_parts = GIVEN in by_property and FAMILY in by_property

        def validate(mapping, value):
            def filled(node):
                return bool((value.get(node.name) or "").strip())

            if filled(name) or (
                has_parts and filled(by_property[GIVEN]) and filled(by_property[FAMILY])
            ):
                return
            error = colander.Invalid(mapping)
            error[name.name] = (
                "Give a name, or both a given name and a family name"
                if has_parts
                else "Give a name"
            )
            raise error

        return validate

    def on_write_sql(self, context: HookContext) -> list[str]:
        flat, name = context.flat, literal(FOAF + "name")
        given, family = literal(GIVEN), literal(FAMILY)
        return [
            f"""INSERT INTO {flat}
                SELECT s, loc, {name}, NULL, full_name, NULL, NULL FROM (
                    SELECT s, loc,
                           concat_ws(' ', max(lit) FILTER (WHERE p = {given}),
                                          max(lit) FILTER (WHERE p = {family})) AS
                                            full_name
                    FROM {flat}
                    WHERE {context.of_type} AND p IN ({given}, {family}) AND lit IS NOT
                      NULL
                    GROUP BY s, loc HAVING count(DISTINCT p) = 2
                ) n
                WHERE NOT EXISTS (SELECT 1 FROM {flat} f
                                  WHERE f.s = n.s AND f.loc = n.loc AND f.p = {name})"""
        ]
