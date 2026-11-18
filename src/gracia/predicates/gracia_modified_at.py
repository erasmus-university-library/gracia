import datetime

import colander

from gracia import uri
from gracia.forms import ReadOnlyWidget
from gracia.predicates import HookContext, Predicate, predicate
from gracia.store import literal

XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"
SQL_STAMP = "strftime(now() AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%fZ')"
SQL_LATER = (
    "strftime(greatest(now() AT TIME ZONE 'UTC', "
    "try_cast(rtrim(o.modified, 'Z') AS TIMESTAMP)), "
    "'%Y-%m-%dT%H:%M:%S.%fZ')"
)


class LocalTimeWidget(ReadOnlyWidget):
    template = "readonly_time"

    def __init__(self, repository, **kw):
        super().__init__(**kw)
        self.repository = repository

    def serialize(self, field, cstruct, **kw):
        text = "" if cstruct is colander.null else cstruct
        if not text:
            return ""
        try:
            parsed = datetime.datetime.fromisoformat(
                text.strip().replace("Z", "+00:00")
            )
        except ValueError:
            return ""
        local = parsed.astimezone(self.repository.timezone())
        return f"{local:%Y-%m-%d %H:%M:%S} {local.tzname()}"


@predicate(uri.MODIFIED_AT)
class ModifiedAtPredicate(Predicate):
    def schema_node(self, record, request, schema_node=None):
        if schema_node is None:
            return schema_node
        schema_node.widget = LocalTimeWidget(record.repository)
        schema_node.validator = None
        schema_node.missing = colander.drop  # not posted: the record keeps what it has
        return schema_node

    def on_write_sql(self, context: HookContext) -> list[str]:
        flat, stored, of_type = context.flat, context.triples, context.of_type
        created, modified = literal(uri.CREATED_AT), literal(uri.MODIFIED_AT)
        work = f"{flat}_stamp"
        columns = "s, loc, p, o, lit, lang, dt"
        is_stamp = f"len(loc) = 0 AND p IN ({created}, {modified})"
        return [
            f"""CREATE OR REPLACE TABLE {work} AS
                WITH recs AS (SELECT DISTINCT s FROM {flat} WHERE {of_type}),
                given AS (SELECT s, max(lit) FILTER (WHERE p = {created}) AS created
                          FROM {flat} WHERE {of_type} AND len(loc) = 0 GROUP BY s),
                old AS (SELECT s,
                               max(lit) FILTER (WHERE len(loc) = 0 AND p = {created}) AS
                                 created,
                               max(lit) FILTER (WHERE len(loc) = 0 AND p = {modified})
                                 AS modified
                        FROM {stored} WHERE s IN (SELECT s FROM recs) GROUP BY s),
                new_rows AS (SELECT {columns} FROM {flat} WHERE {of_type} AND NOT
                  ({is_stamp})),
                old_rows AS (SELECT {columns} FROM {stored}
                             WHERE s IN (SELECT s FROM recs) AND NOT ({is_stamp})),
                changed AS (SELECT DISTINCT s FROM (
                    SELECT s FROM (SELECT * FROM new_rows EXCEPT ALL SELECT * FROM
                      old_rows)
                    UNION ALL
                    SELECT s FROM (SELECT * FROM old_rows EXCEPT ALL SELECT * FROM
                      new_rows)))
                SELECT r.s,
                       coalesce(o.created, g.created, {SQL_STAMP}) AS created,
                       CASE WHEN o.modified IS NOT NULL AND r.s NOT IN (SELECT s FROM
                         changed)
                            THEN o.modified
                            ELSE {SQL_LATER} END AS modified
                FROM recs r LEFT JOIN old o ON o.s = r.s LEFT JOIN given g ON g.s =
                  r.s""",
            f"DELETE FROM {flat} WHERE {of_type} AND {is_stamp}",
            f"""INSERT INTO {flat}
                SELECT s, []::VARCHAR[], {created}, NULL, created, NULL,
                  {literal(XSD_DATETIME)} FROM {work}
                UNION ALL
                SELECT s, []::VARCHAR[], {modified}, NULL, modified, NULL,
                  {literal(XSD_DATETIME)} FROM {work}""",
            f"DROP TABLE IF EXISTS {work}",
        ]
