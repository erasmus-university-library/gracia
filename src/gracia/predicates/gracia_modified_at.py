import colander

from gracia import uri
from gracia.forms import ReadOnlyWidget
from gracia.predicates import HookContext, Predicate, gracia_timezone, predicate
from gracia.store import literal

XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"
# the time of the transaction as `xsd:dateTime` in UTC; `now()` is the same in every statement of one
# transaction, and `records.modified_at` is read from the triple this makes (macros.sql, `record_rows`)
STAMP = "strftime(now() AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%fZ')"
# the time of the transaction, or the stored time when that is later (the clock of the server went back):
# a record is never modified earlier than it was
LATER = (
    "strftime(greatest(now() AT TIME ZONE 'UTC', try_cast(rtrim(o.modified, 'Z') AS TIMESTAMP)), "
    "'%Y-%m-%dT%H:%M:%S.%fZ')"
)


class LocalTimeWidget(ReadOnlyWidget):
    """A stored time (UTC) as text in the time zone of the repository; not edited, and not posted
    (the input has no name, so what the form says of the record never has the shown text)."""

    template = "readonly_time"

    def __init__(self, repository, **kw):
        super().__init__(**kw)
        self.repository = repository

    def serialize(self, field, cstruct, **kw):
        text = "" if cstruct is colander.null else cstruct
        return super().serialize(field, gracia_timezone.show(text, self.repository.timezone()), **kw)


def local_time_node(schema_node, request, record):
    """The read-only field of a time: the node the shape made, showing the time in the zone of the
    repository."""
    if schema_node is None:
        return schema_node
    schema_node.widget = LocalTimeWidget(record.repository)
    schema_node.validator = None
    schema_node.missing = colander.drop  # not posted: the record keeps what it has
    return schema_node


@predicate(uri.MODIFIED_AT)
class ModifiedAtPredicate(Predicate):
    """`gracia:modified_at` of a record: when its rows last changed, `xsd:dateTime` in UTC, the time
    of the transaction that saved it (the `modified_at` of the `records` table is this value). It is
    made when the record is saved and it is never set from outside: the form only shows it, in the
    time zone of the repository, and a text (an import, a restore) that has one is not believed. A
    record that is new, or stored with rows that changed, gets the time of the transaction (never
    earlier than the one it had), and one whose rows did not change keeps what it has. So the time of
    a record only goes up, which `etags.last_modified_at` and the order of the listings rely on.

    A save that changes nothing leaves it: the rows of the record (this property and `created_at`
    apart) are compared with the stored ones, so importing the same data again, or making the
    records again after a schema was switched (`Store.rederive`), is no modification.
    `created_at` is made here too (`gracia_created_at.py` only shows it): the stored one is kept."""

    def schema_node(self, record, request, schema_node=None):
        return local_time_node(schema_node, request, record)

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
                               max(lit) FILTER (WHERE len(loc) = 0 AND p = {created}) AS created,
                               max(lit) FILTER (WHERE len(loc) = 0 AND p = {modified}) AS modified
                        FROM {stored} WHERE s IN (SELECT s FROM recs) GROUP BY s),
                new_rows AS (SELECT {columns} FROM {flat} WHERE {of_type} AND NOT ({is_stamp})),
                old_rows AS (SELECT {columns} FROM {stored}
                             WHERE s IN (SELECT s FROM recs) AND NOT ({is_stamp})),
                changed AS (SELECT DISTINCT s FROM (
                    SELECT s FROM (SELECT * FROM new_rows EXCEPT ALL SELECT * FROM old_rows)
                    UNION ALL
                    SELECT s FROM (SELECT * FROM old_rows EXCEPT ALL SELECT * FROM new_rows)))
                SELECT r.s,
                       coalesce(o.created, g.created, {STAMP}) AS created,
                       CASE WHEN o.modified IS NOT NULL AND r.s NOT IN (SELECT s FROM changed)
                            THEN o.modified
                            ELSE {LATER} END AS modified
                FROM recs r LEFT JOIN old o ON o.s = r.s LEFT JOIN given g ON g.s = r.s""",
            f"DELETE FROM {flat} WHERE {of_type} AND {is_stamp}",
            f"""INSERT INTO {flat}
                SELECT s, []::VARCHAR[], {created}, NULL, created, NULL, {literal(XSD_DATETIME)} FROM {work}
                UNION ALL
                SELECT s, []::VARCHAR[], {modified}, NULL, modified, NULL, {literal(XSD_DATETIME)} FROM {work}""",
            f"DROP TABLE IF EXISTS {work}",
        ]
