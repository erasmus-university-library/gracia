import contextlib
import os
import random
import re
import secrets
import shutil
import stat
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import duckdb
import pyoxigraph as ox

from gracia import uri
from gracia.graph import Graph

_SQL = files("gracia") / "assets" / "sql"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BATCH = 50_000

# a load that loses a race with another writer is repeated this many times before it
# gives up
LOAD_ATTEMPTS = 20

TYPE_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

_STAGING_COLUMNS = (
    "s VARCHAR, s_type VARCHAR, p VARCHAR, o VARCHAR, o_type VARCHAR, "
    "o_lang VARCHAR, o_dt VARCHAR"
)
_ROW_COLUMNS = "s, s_type, p, o, o_type, o_lang, o_dt"


# the transition tables of the triggers that keep `records` (and its two list tables) up
# to date,
# and the subjects of the records that changed
_RECORD_EVENTS = {
    "insert": ("NEW TABLE AS changed", "SELECT s FROM changed"),
    "update": (
        "OLD TABLE AS before NEW TABLE AS after",
        "SELECT s FROM before UNION SELECT s FROM after",
    ),
    "delete": ("OLD TABLE AS changed", "SELECT s FROM changed"),
}
_RECORD_STEPS = 23  # triggers per event, they fire in the order of their names


def literal(value: str) -> str:
    """SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


_TYPE_NUMBER = re.compile(r"\(n_ = (\d+)\)\) THEN \('((?:[^']|'')*)'\)")


def _type_macros(types: dict[str, int], replace: bool) -> str:
    """The macros that know the numbers of the types: `record_id(uri)`, the id of a
    record uri, and `type_slug(number)`, the slug of a type. They are written out for
    these types, so that an id is one test of a prefix and one cast: `rec:person:` gives
    `5 * 10^7` plus the number after it. A uri that starts with none of the types is an
    error (`unknown record uri`), and so is a number that is not 0 to 9,999,999 (a cast
    to a UINTEGER and to a DECIMAL(7,0) refuses what is not), which would otherwise be
    the id of another type. NULL is NULL. Whoever asks about a uri that may be anything
    (a reference that is an ARK) tests for the prefix `rec:` first. A type number is 0
    to 99, and one number is one type."""
    for slug, number in types.items():
        if not TYPE_SLUG.match(slug):
            raise ValueError(f"not a type slug: {slug!r}")
        if not 0 <= number <= uri.MAX_TYPE_NUMBER:
            raise ValueError(
                f"a type number has to be between 0 and {uri.MAX_TYPE_NUMBER} to have "
                f"ids: {slug} "
                f"{number}"
            )
    if len(set(types.values())) != len(types):
        raise ValueError(f"two types with one number: {types}")
    head = "CREATE OR REPLACE MACRO" if replace else "CREATE MACRO IF NOT EXISTS"
    ordered = sorted(types.items(), key=lambda item: item[1])
    ids = " ".join(
        f"WHEN starts_with(u_, 'rec:{slug}:') THEN "
        f"CAST({number * uri.TYPE_BASE} + CAST(CAST(substr(u_, {len(slug) + 6}) AS "
        "UINTEGER) AS "
        "DECIMAL(7,0)) AS INTEGER)"
        for slug, number in ordered
    )
    slugs = " ".join(f"WHEN {number} THEN {literal(slug)}" for slug, number in ordered)
    return (
        f"{head} record_id(u_) AS CASE WHEN u_ IS NULL THEN NULL {ids} "
        f"ELSE error('unknown record uri: ' || u_) END;\n{head} type_slug(n_) AS CASE "
        f"n_ {slugs} "
        "END;"
    )


def fetch_one(cursor) -> tuple:
    """The row of a query that always gives one (an aggregate, `RETURNING`)."""
    row = cursor.fetchone()
    if row is None:
        raise LookupError("the query gave no row")
    return row


def sql_list(values: list[str]) -> str:
    return "[" + ", ".join(literal(v) for v in values) + "]"


def sql_ids(uris: list[str]) -> str:
    """SQL: the ids of record uris (`record_id`), a subquery for `record_id IN ...`; a
    uri that is not one of a record (it does not start with `rec:`) is left out; one of
    a type that is not there raises."""
    return (
        "(SELECT record_id(u) FROM "
        f"(SELECT unnest({sql_list(sorted(set(uris)))}) AS u) "
        "WHERE starts_with(u, 'rec:'))"
    )


def sql_value(value) -> str:
    """A Python value as a SQL literal: None, booleans, numbers, text, and anything else
    as the text of the value (dates and times, uuids), which DuckDB casts where a column
    wants them."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    if hasattr(value, "isoformat"):
        return literal(value.isoformat())
    return literal(str(value))


def bind(sql: str, params) -> str:
    """Replace the `?` placeholders in `sql` by SQL literals (a remote call takes no
    parameters). There must be no other `?` in the statement."""
    parts = sql.split("?")
    if len(parts) != len(params) + 1:
        raise ValueError("the number of parameters does not match the placeholders")
    out = [parts[0]]
    for value, part in zip(params, parts[1:], strict=True):
        out.append(sql_value(value) + part)
    return "".join(out)


class RemoteConnection:
    """A connection to a database that another process owns (Quack), for `Store` and
    `JobsDb`.

    Attached to a local DuckDB, the remote catalog does not behave like a local one: the
    current schema is not applied to unqualified names, statements that read and write
    it together are planned locally with the wrong defaults, and several streaming scans
    in one query are refused. So every statement is sent to the owner and runs in its
    session with `quack_query_by_name` (where `USE`, transactions and macros behave as
    in a plain DuckDB). That call takes no parameters, so the `?` of a statement are
    replaced by literals (`bind`). The local connection is kept (`.local`) for what only
    works there: appenders and the temp tables that feed them.

    `connect()` makes a new local connection with its own attachment. Every `Store`
    needs one: all cursors of one attachment share a single session on the owner, so two
    stores would overwrite each other's current schema and interrupt each other's
    queries."""

    def __init__(self, local, catalog: str, connect):
        self.local = local  # the attached catalog's alias is `catalog`
        self.catalog = catalog
        self.connect = connect

    def execute(self, sql: str, params=None):
        if params:
            sql = bind(sql, params)
        return self.local.execute(
            f"SELECT * FROM quack_query_by_name({literal(self.catalog)}, "
            f"{literal(sql)})"
        )

    def begin(self) -> None:
        self.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.execute("COMMIT")

    def rollback(self) -> None:
        self.execute("ROLLBACK")

    def cursor(self) -> "RemoteConnection":
        return RemoteConnection(self.connect(), self.catalog, self.connect)

    def close(self) -> None:
        self.local.close()


def uri_list(
    uris: "str | ox.NamedNode | Iterable[str | ox.NamedNode] | None",
) -> list[str] | None:
    """One uri, several, or None (= all), as a list of strings."""
    if uris is None:
        return None
    if isinstance(uris, str | ox.NamedNode):
        uris = [uris]
    return [u.value if isinstance(u, ox.NamedNode) else str(u) for u in uris]


def _uris_arg(uris) -> str:
    values = uri_list(uris)
    return "NULL" if values is None else sql_list(values)


DEFAULT_REPOSITORY = (
    "main"  # DuckDB's own default schema: the repository the commands use by default
)
REPOSITORY_ID = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# ids that would clash with DuckDB's own schemas or with the first path segments of the
# web app
RESERVED_REPOSITORY_IDS = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "temp",
        "system",
        "docs",
        "redoc",
        "openapi",
        "api",
        "static",
        "health",
        "repositories",
    }
)


class DuplicateValue(ValueError):
    """A value that only one record of a type may have (`dash:uniqueValueForClass`, a
    userid) is already the value of another record. The save is refused, and nothing of
    it is stored."""

    def __init__(self, rec_type: str, predicate: str, value: str, holder: str | None):
        self.rec_type, self.predicate, self.value, self.holder = (
            rec_type,
            predicate,
            value,
            holder,
        )
        name = predicate.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        by = f" (by {holder})" if holder else ""
        super().__init__(f"The {name} {value!r} is taken{by}")


# DuckDB words the unique key of `unique_values` in two ways: as the insert of a row
# finds the
# other, and as a bulk insert finds two of its own rows alike
_DUPLICATE = re.compile(
    r'Duplicate key "rec_type: (.*?), p: (.*?), value: (.*)" violates unique'
    r'|duplicate key "([a-z][a-z0-9-]*), ([^,]+), (.*)"'
)


class NoSuchRepository(ValueError):
    """The repository (a schema with the gracia tables) does not exist."""

    def __init__(self, repo: str):
        super().__init__(
            f"no such repository: {repo} (create it with `gracia repo create {repo}`)"
        )
        self.repo = repo


def validate_repository_id(repo: str) -> str:
    """A repository id is a schema name: lowercase letters, digits and underscores,
    starting with a letter, at most 63 characters, and not one of the reserved names."""
    if not isinstance(repo, str) or not REPOSITORY_ID.match(repo):
        raise ValueError(
            f"invalid repository id: {repo!r} (lowercase letters, digits and _, "
            "starting with a "
            "letter, at most 63 characters)"
        )
    if repo in RESERVED_REPOSITORY_IDS:
        raise ValueError(f"reserved repository id: {repo!r}")
    return repo


def _repository_exists(con, catalog: str, repo: str) -> bool:
    """A repository exists when its schema holds the `triples` table."""
    return bool(
        con.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE database_name = ? AND schema_name = ? AND table_name = 'triples'",
            [catalog, repo],
        ).fetchone()[0]
    )


# up to this many touched records the records made of them are found by one
# `list_contains` per
# touched record on each row (measured: 2 ms each at 100,000 records, where
# `list_has_any` takes
# 30 ms whatever the list), beyond it by unnesting the arrays and joining (a constant 7
# ms)
ETAGS_CONTAINS_UP_TO = 4
# more records made of the changed ones than this: they are not walked again when what
# the changed
# ones point at is the same, their etag is moved by the change of the local hashes
# instead
ETAGS_DELTA_ABOVE = 200
# the order `etags` is kept in: what `Store.referrers` reads, newest created first, with
# no ORDER BY
ETAGS_ORDER = "ORDER BY record_type, sort_key DESC, record_id"
PAGE_SIZE = 20  # records on a page of a listing; the last page has up to twice as many
EMPTY_TOTALS = (
    "CAST(MAP {} AS MAP(VARCHAR, STRUCT(total_all BIGINT, total_published BIGINT)))"
)


def page_window(total: int, page: int | None, size: int) -> tuple[int, int, int, int]:
    """Which records of a listing of `total` (newest first in the table) make a page:
    (page, pages, offset, limit). Page 1 is the newest `size` records and the pages go
    back in time; the last page has what is left (1 to `size`). There is always one
    page, empty when there are no records. Page 1 when `page` is None. `ValueError` for
    a page that is not there."""
    pages = max(1, -(-total // size))
    page = 1 if page is None else page
    if not 1 <= page <= pages:
        raise ValueError(f"there is no page {page}: the pages are 1 to {pages}")
    offset = (page - 1) * size
    return page, pages, offset, min(size, total - offset)


@dataclass
class Page:
    """One page of a listing (`Store.referrers`): `items` are (record uri, etag as 16
    hex digits), newest created first; `page` of `pages` (page 1 is the newest, see
    `page_window`); `total` the records in the listing as it was asked for (published
    ones, or all), `total_all` and `total_published` both, whichever was asked for;
    `size` the usual number on a page."""

    items: list[tuple[str, str]]
    page: int
    pages: int
    total: int
    total_all: int
    total_published: int
    size: int


class Store:
    """One repository of a gracia database: a `triples` table and the prefixes, all
    inside one DuckDB schema named like the repository (tables configurable).

    `Store(path)` opens the file and works in the default repository `main` (created on
    first use). `repo=` selects another repository, which must exist (`create=True`
    makes it). `connection=` uses a connection of a database that is already open: with
    `init=False` the schema, tables and macros are not run again (a new `Store` per web
    request stays cheap)."""

    def __init__(
        self,
        path: str | Path | None = "gracia.db",
        triples: str = "triples",
        repo: str = DEFAULT_REPOSITORY,
        *,
        connection=None,
        create: bool = False,
        init: bool = True,
        close_connection: bool | None = None,
        remote=None,
    ):
        # Over Quack the macros run in the owner process and cannot see a temp table of
        # ours: the
        # staging table is then a regular table (one per store, so writers do not clash)
        # that is
        # filled by pushing a local temp table with INSERT ... SELECT (appenders do not
        # work there).
        self._connect_remote = (
            remote  # a factory of attached connections, or None (local)
        )
        self.remote = remote is not None
        self.staging = (
            f"gracia_staging_{uuid.uuid4().hex[:12]}"
            if self.remote
            else "gracia_staging"
        )
        self.flat = (
            f"{self.staging}_flat"  # the staged rows flattened, what the hooks work on
        )
        self.hooks: list[str] | None = None  # SQL run on `flat`, see `configure`
        self._provider = None
        self._roles: list | None = None
        if not _IDENT.match(triples):
            raise ValueError(f"invalid table name: {triples!r}")
        if repo != DEFAULT_REPOSITORY:
            validate_repository_id(repo)
        self.triples, self.repo = triples, repo
        self.ids = "ids"
        self.records = "records"
        self._ids_ready = False
        self._type_numbers: dict[str, int] | None = None
        self._close_connection = (
            connection is None if close_connection is None else close_connection
        )
        self.con = duckdb.connect(str(path)) if connection is None else connection
        try:
            # the local view of the database decides the catalog name and whether the
            # repository
            # exists; over Quack the rest of the SQL runs on the owner (see
            # `RemoteConnection`)
            self._catalog = fetch_one(self.con.execute("SELECT current_database()"))[0]
            exists = _repository_exists(self.con, self._catalog, repo)
            if self.remote:
                self.con = RemoteConnection(self.con, self._catalog, remote)
            if not exists and repo != DEFAULT_REPOSITORY and not create:
                raise NoSuchRepository(repo)
            if not exists and not init:
                raise NoSuchRepository(repo)
            if not exists:
                self.con.execute(f'CREATE SCHEMA IF NOT EXISTS "{repo}"')
            if (
                init or not exists
            ):  # one row per repository, in the schema `main` of the database
                self.con.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._modified_table()} "
                    "(repo_id VARCHAR PRIMARY KEY, last_modified_at TIMESTAMP NOT NULL)"
                )
            self._use(self.con)
            if init or not exists:
                self.con.execute(self._sql("schema.sql"))
                self._ensure_ids()
                # the counter of the transactions, for writes outside the store too
                self.con.execute(
                    f"INSERT INTO {self.ids} (type, last) VALUES ('_transaction', 0) "
                    "ON CONFLICT "
                    "(type) DO NOTHING"
                )
                # record_rows uses the other macros, and DuckDB will not replace a macro
                # that one
                # uses: it goes first, and the triggers that use it before that
                self._drop_record_triggers()
                catalog = fetch_one(self.con.execute("SELECT current_database()"))[0]
                self.con.execute(
                    f'DROP MACRO TABLE IF EXISTS "{catalog}"."{self.repo}".record_rows'
                )
                # the numbers of the types (a repository that was made has its own,
                # kept), and the
                # ids
                # made of them, before the macros that use them
                self.con.execute(_type_macros(uri.BASE_TYPE_NUMBERS, replace=False))
                self.con.execute((_SQL / "record_ids.sql").read_text(encoding="utf-8"))
                self.con.execute((_SQL / "macros.sql").read_text(encoding="utf-8"))
                self.con.execute(self._sql("records.sql") + self._record_triggers())
                self.con.execute(
                    self._sql("etags.sql")
                )  # the etags are made with the schema, so they are always there
        except BaseException:
            self.close()
            raise

    def _modified_upsert(self) -> str:
        """The upsert of this repository's row in `repositories_last_modified_at`, with
        the time of the transaction (the time `modified_at` gets)."""
        return (
            f"INSERT INTO {self._modified_table()} VALUES ({literal(self.repo)}, now() "
            "AT TIME "
            "ZONE 'UTC') "
            "ON CONFLICT (repo_id) DO UPDATE SET last_modified_at = "
            "excluded.last_modified_at"
        )

    def _modified_table(self) -> str:
        # no catalog name: a copy of the database file has another one, and the triggers
        # keep the
        # name
        return "main.repositories_last_modified_at"

    def _record_trigger_names(self) -> list[str]:
        return [
            # they fire in the order of their names
            f"{self.triples}_records_{event}_{step:02d}"
            for event in _RECORD_EVENTS
            for step in range(1, _RECORD_STEPS + 1)
        ]

    def _record_triggers(self) -> str:
        """The triggers on the triples table that keep `records` and its list tables in
        step: for every statement that inserts, updates or deletes rows, the records of
        those rows are rewritten whole (see `records.sql`). Several statement-level
        triggers of one event fire in the order they were made; the body is one
        statement each."""
        rec, ids, refs = (
            self.records,
            f"{self.records}_identifiers",
            f"{self.records}_references",
        )
        changes = f"{self.records}_changes"
        # plain DELETE and INSERT: INSERT ... ON CONFLICT DO UPDATE is a MERGE in DuckDB
        # and cost
        # ten times as much as a DELETE and an INSERT (measured on 5,000 records)
        unique = (
            "INSERT INTO unique_values SELECT DISTINCT r.rec_type, t.p, coalesce(t.o, "
            "t.lit), t.s "
            "FROM {table} t JOIN property_roles r ON r.role = 'unique' AND r.p = t.p "
            "AND r.rec_type = regexp_extract(t.s, '^rec:([a-z][a-z0-9-]*):', 1) "
            "WHERE t.s IN ({subs})"
        )
        # the number of the transaction: the session variable `txn_id` when the store
        # set it, else
        # (a write outside the store) the statement takes the next one itself, in its
        # first trigger
        number = (
            "coalesce(getvariable('txn_id'), "
            "(SELECT last FROM {counter} WHERE type = '_transaction'))"
        )
        # the ids of the records of the statement, made once (a statement changes many
        # rows of few
        # records: the steps after it work on the ids, not on the rows)
        changed = "SELECT record_id FROM {changes}"
        bodies = [
            "UPDATE {counter} SET last = last + 1 WHERE type = '_transaction' "
            "AND getvariable('txn_id') IS NULL",
            "DELETE FROM {changes}",
            # a record of a type that has no number, or a number that is too high, has
            # no id: the
            # write is
            # refused (`record_id` raises)
            "INSERT INTO {changes} SELECT DISTINCT record_id(s) "
            "FROM (SELECT DISTINCT s FROM ({subs})) WHERE starts_with(s, 'rec:')",
            f"DELETE FROM {{ids}} WHERE record_id IN ({changed})",
            f"DELETE FROM {{refs}} WHERE record_id IN ({changed})",
            "DELETE FROM unique_values WHERE s IN ({subs})",
            f"DELETE FROM {{rec}} WHERE record_id IN ({changed})",
            unique,
            "INSERT INTO {rec} SELECT * FROM record_rows({table_literal}, "
            "(SELECT list(record_uri(record_id)) FROM {changes}))",
            # the record logic does not mix with the resource logic: the content of a
            # resource
            # (below
            # `gracia:resource`) points at the resource uris of other records, never at
            # a record
            "INSERT INTO {changes} SELECT error('the resource of ' || t.s || ' points "
            "at the "
            "record ' || t.o || "
            "': a resource points at the resource uri of a record, never at the "
            "record') "
            "FROM {table} t WHERE t.s IN ({subs}) AND len(t.loc) >= 2 AND t.loc[1] = "
            "{resource} "
            "AND starts_with(t.o, 'rec:')",
            # a resource uri names one record: the links between resources are made
            # through it, so a
            # second record with the same one would be a second thing that is pointed at
            # (nothing is
            # inserted: the statement fails when it finds the pair)
            "INSERT INTO {changes} SELECT error('the resource uri ' || r.res_uri || ' "
            "is of two "
            "records: ' || "
            "record_uri(r.record_id) || ' and ' || record_uri(o.record_id)) "
            "FROM {rec} r JOIN {rec} o ON o.res_uri = r.res_uri AND o.record_id <> "
            "r.record_id "
            "WHERE r.record_id IN (SELECT record_id FROM {changes})",
            # the slugs (predicates/gracia_slug.py): a record that has one now has it
            # for good; one
            # that was deleted and is there again has its slugs back; one that is gone
            # has them
            # flagged and its number as a slug of its own, so that its address is told
            # from one
            # that was never used
            "INSERT INTO slugs SELECT DISTINCT regexp_extract(t.s, "
            "'^rec:([a-z][a-z0-9-]*):', 1), "
            "t.lit, t.s, false FROM {table} t "
            "WHERE t.s IN ({subs}) AND len(t.loc) = 0 AND t.p = {slug_uri} AND t.lit "
            "IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM slugs g WHERE g.record_uri = t.s AND g.slug "
            "= t.lit)",
            "UPDATE slugs SET is_deleted = false WHERE is_deleted "
            f"AND record_uri IN (SELECT record_uri(record_id) FROM {{rec}} WHERE "
            "record_id IN "
            f"({changed}))",
            "UPDATE slugs SET is_deleted = true WHERE NOT is_deleted AND record_uri IN "
            "{gone}",
            "INSERT INTO slugs SELECT regexp_extract(s, '^rec:([a-z][a-z0-9-]*):', 1), "
            "regexp_extract(s, ':([0-9]+)$', 1), s, true FROM {gone} g(s) "
            "WHERE NOT EXISTS (SELECT 1 FROM slugs k WHERE k.record_uri = g.s "
            "AND k.slug = regexp_extract(g.s, ':([0-9]+)$', 1))",
            # one row of the history for a record and a transaction: what an earlier
            # statement of
            # the transaction wrote for it (a replace is a delete and an insert) is
            # replaced
            f"DELETE FROM history WHERE txn_id = {number} AND record_id IN ({changed})",
            # a record of the statement that has no row in `records` is one that has no
            # rows left:
            # it goes to the history as deleted
            f"INSERT INTO history SELECT c.record_id, {number}, "
            "coalesce(r.modified_at, now() AT TIME ZONE 'UTC'), r.record_id IS NULL, "
            "coalesce(r.source, '') "
            "FROM {changes} c LEFT JOIN {rec} r ON r.record_id = c.record_id",
            # the same for the queue of what the followers have to do: one row for a
            # record and a
            # transaction, so a replace (a delete and an insert) is the one `update` it
            # is to them.
            # A
            # record that is there is new when it was not alive before this transaction
            f"DELETE FROM actions_queue WHERE transaction_id = {number} AND record_id "
            "IN "
            f"({changed})",
            f"INSERT INTO actions_queue SELECT {number}, c.record_id, "
            "CASE WHEN r.record_id IS NULL THEN 'delete' WHEN p.alive THEN 'update' "
            "ELSE 'insert' "
            "END "
            "FROM {changes} c LEFT JOIN {rec} r ON r.record_id = c.record_id "
            "LEFT JOIN (SELECT record_id, NOT arg_max(is_deleted, txn_id) AS alive "
            "FROM history "
            f"WHERE txn_id < {number} AND record_id IN ({changed}) GROUP BY record_id) "
            "p "
            "ON p.record_id = c.record_id",
            "INSERT INTO {ids} SELECT DISTINCT unnest(identifiers), record_id FROM "
            "{rec} "
            f"WHERE record_id IN ({changed})",
            'INSERT INTO {refs} SELECT DISTINCT unnest("references"), record_id FROM '
            "{rec} "
            f"WHERE record_id IN ({changed})",
            # the database-wide table of when each repository was last written (in
            # `main`, not in
            # this
            # repository's schema): now() is the time of the transaction, the one
            # `modified_at` gets
            self._modified_upsert(),
            "DELETE FROM {changes}",
        ]
        assert len(bodies) == _RECORD_STEPS
        statements = []
        names = iter(self._record_trigger_names())
        for event, (referencing, subs) in _RECORD_EVENTS.items():
            for body in bodies:
                body = body.format(
                    rec=rec,
                    counter=self.ids,
                    ids=ids,
                    refs=refs,
                    changes=changes,
                    subs=subs,
                    table=self.triples,
                    table_literal=literal(self.triples),
                    record_pattern=literal(uri.RECORD_PATTERN),
                    slug_uri=literal(uri.SLUG),
                    resource=literal(uri.RESOURCE),
                    # the records of the statement that have no row in `records` any
                    # more
                    gone=(
                        f"(SELECT record_uri(record_id) AS s FROM {changes} WHERE "
                        "record_id NOT IN "
                        f"(SELECT record_id FROM {rec} WHERE record_id IN (SELECT "
                        "record_id FROM "
                        f"{changes})))"
                    ),
                )
                statements.append(
                    f"CREATE OR REPLACE TRIGGER {next(names)} AFTER {event.upper()} ON "
                    f"{self.triples} "
                    f"REFERENCING {referencing} FOR EACH STATEMENT {body};"
                )
        return "\n".join(statements)

    def _drop_record_triggers(self) -> None:
        """Drop the triggers that keep `records` (`records.sql` makes them again). The
        table has to exist for DROP TRIGGER, even with IF EXISTS."""
        exists = fetch_one(
            self.con.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE schema_name = "
                f"{literal(self.repo)} "
                f"AND table_name = {literal(self.triples)}"
            )
        )[0]
        if exists:
            for name in self._record_trigger_names():
                self.con.execute(f"DROP TRIGGER IF EXISTS {name} ON {self.triples}")

    def _use(self, con) -> None:
        """Select this repository's schema on a connection (a cursor does not inherit
        it)."""
        if self.remote:  # the owner keeps a current schema per connection, and so do we
            con.execute(f'USE "{self.repo}"')
            con = con.local
            con.execute(
                "CALL quack_clear_cache()"
            )  # the local view may predate the schema
        con.execute(f'USE "{self._catalog}"."{self.repo}"')

    def fork(self) -> "Store":
        """A new Store on its own cursor, for one thread or one request. It shares the
        database and does no setup. Close it when done."""
        return Store(
            triples=self.triples,
            repo=self.repo,
            connection=self._connect_remote() if self.remote else self.con.cursor(),  # ty: ignore[not-callable]
            init=False,
            close_connection=True,
            remote=self._connect_remote,
        )

    def close(self) -> None:
        if self._close_connection:
            self.con.close()

    def counts(self) -> dict[str, int]:
        """Cheap figures about the repository: stored rows."""

        def count(table):
            return fetch_one(self.con.execute(f"SELECT count(*) FROM {table}"))[0]

        return {
            "rows": count(self.triples),
        }

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _sql(self, name: str, **subs: str) -> str:
        text = (_SQL / name).read_text(encoding="utf-8")
        text = text.replace("@TRIPLES@", self.triples)
        text = text.replace("@STAGING@", self.staging)
        text = text.replace("@FLAT@", self.flat)
        text = text.replace("@IDS@", self.ids)
        text = text.replace("@RECORDS@", self.records)
        for key, value in subs.items():
            text = text.replace(key, value)
        return text

    # ---- staging
    # -------------------------------------------------------------------------

    def _new_staging(self) -> None:
        res_type = "TABLE" if self.remote else "TEMP TABLE"
        self.con.execute(
            f"CREATE OR REPLACE {res_type} {self.staging} ({_STAGING_COLUMNS})"
        )

    def stage_rows(self, rows: Iterable[tuple]) -> int:
        """Stream (s, s_type, p, o, o_type, o_lang, o_dt) rows into a fresh staging
        table."""
        self._new_staging()
        # remote: fill a local temp table, push it afterwards
        target = "gracia_push" if self.remote else self.staging
        if self.remote:
            self.con.local.execute(  # ty: ignore[missing-union-attribute]
                f"CREATE OR REPLACE TEMP TABLE {target} ({_STAGING_COLUMNS})"
            )
        appender = (self.con.local if self.remote else self.con).appender(target)  # ty: ignore[missing-union-attribute]
        batch: list[tuple] = []
        count = 0
        for row in rows:
            batch.append(row)
            if len(batch) >= BATCH:
                appender.append_rows(batch)
                count += len(batch)
                batch.clear()
        if batch:
            appender.append_rows(batch)
            count += len(batch)
        appender.flush()
        if self.remote:
            self.con.local.execute(  # ty: ignore[missing-union-attribute]
                "CALL quack_clear_cache()"
            )  # the staging table is new
            self.con.local.execute(  # ty: ignore[missing-union-attribute]
                f"INSERT INTO {self.staging} SELECT * FROM temp.main.{target}"
            )
            self.con.local.execute(f"DROP TABLE temp.main.{target}")  # ty: ignore[missing-union-attribute]
        return count

    def stage_json(self, json_file: str | Path) -> int:
        """Stage a json-triples file (the shape raptor's json-triples serializer
        writes)."""
        self._new_staging()
        self.con.execute(
            f"INSERT INTO {self.staging} SELECT {_ROW_COLUMNS} FROM "
            f"parse_triples({literal(str(json_file))})"
        )
        return fetch_one(self.con.execute(f"SELECT count(*) FROM {self.staging}"))[0]

    def _rollback(self) -> None:
        """Roll back after a failure. DuckDB has already ended a transaction that hit a
        conflict, and a second error here would hide the first."""
        with contextlib.suppress(duckdb.TransactionException):
            self.con.rollback()
        self._forget_number()

    # ---- the transactions of the history (history.py)
    # ---------------------------------------

    def _take_number(self) -> int:
        """The number of the transaction that is open: the counter row of `ids` is
        incremented in it (so a rollback gives the number back, and a second writer
        waits for the first to commit: the numbers are in the order of the commits), and
        the triggers are told it through the session variable `txn_id`, which they copy
        into `history`."""
        self._ensure_ids()
        (number,) = fetch_one(
            self.con.execute(
                f"INSERT INTO {self.ids} (type, last) VALUES ('_transaction', 1) "
                f"ON CONFLICT (type) DO UPDATE SET last = {self.ids}.last + 1 "
                "RETURNING last"
            )
        )
        self.con.execute(f"SET VARIABLE txn_id = {int(number)}")
        return number

    def _forget_number(self) -> None:
        """The writes of this session are not a transaction of the store any more."""
        with contextlib.suppress(duckdb.Error):
            self.con.execute("SET VARIABLE txn_id = NULL")

    def _committed(self, number: int) -> None:
        """The transaction `number` has committed: the writes of this session are not a
        transaction of the store any more."""
        self._forget_number()

    def last_transaction(self) -> int:
        """The number of the last transaction that wrote (0: none yet)."""
        self._ensure_ids()
        row = self.con.execute(
            f"SELECT last FROM {self.ids} WHERE type = '_transaction'"
        ).fetchone()
        return row[0] if row else 0

    # ---- the etags of the pages (etags.sql)
    # ----------------------------------------------------

    def type_numbers(self) -> dict[str, int]:
        """The number of every type the database knows, by its slug: what `type_slug`
        says. It is read from the macro and kept until `set_type_ids` changes it."""
        if self._type_numbers is None:
            definition = self.con.execute(
                "SELECT macro_definition FROM duckdb_functions() WHERE function_name = "
                "'type_slug' "
                "AND database_name = current_database() AND schema_name = "
                f"{literal(self.repo)}"
            ).fetchone()
            self._type_numbers = (
                {
                    slug.replace("''", "'"): int(number)
                    for number, slug in _TYPE_NUMBER.findall(definition[0])
                }
                if definition
                else {}
            )
        return self._type_numbers

    def set_type_ids(self, types: Iterable[tuple[str, int]]) -> bool:
        """Tell the database the number of every type (its slug, and the number of its
        type record): what the ids of the records are made of (`record_id`). A type that
        is new is added to the macros, which means making the triggers of the records
        again (DuckDB does not replace a macro that a trigger uses), in one transaction.
        A type never has another number: the ids are in `records` and in the history for
        good, so that is an error. Returns whether anything changed."""
        wanted = {slug: int(number) for slug, number in types}
        known = self.type_numbers()
        for slug, number in wanted.items():
            if slug in known and known[slug] != number:
                raise ValueError(
                    f"the type {slug} has the number {known[slug]}, not {number}: a "
                    "type keeps its "
                    "number"
                )
        added = {slug: number for slug, number in wanted.items() if slug not in known}
        if not added:
            return False
        macros = _type_macros(
            {**known, **added}, replace=True
        )  # checks the numbers before anything is dropped
        self._type_numbers = None
        self.con.begin()
        try:
            self._drop_record_triggers()
            self.con.execute(macros)
            self.con.execute(self._record_triggers())
            self.con.commit()
        except BaseException as err:
            self._rollback()
            self._type_numbers = None
            if isinstance(err, duckdb.TransactionException) and all(
                slug in self.type_numbers() for slug in added
            ):
                # another writer made the same types a moment ago (a conflict of the
                # triggers it
                # replaced)
                return False
            raise
        return True

    def _etags_marker(self) -> int:
        row = self.con.execute(
            f"SELECT last FROM {self.ids} WHERE type = '_etags'"
        ).fetchone()
        return row[0] if row else 0

    def _set_etags_marker(self, number: int) -> None:
        self.con.execute(
            f"INSERT INTO {self.ids} (type, last) VALUES ('_etags', {int(number)}) "
            f"ON CONFLICT (type) DO UPDATE SET last = {int(number)}"
        )

    def refresh_etags(self) -> None:
        """Make the etags of what changed since they were last made, in the transaction
        of the caller (a write calls it before it commits, so the etags are always those
        of the data).

        What changed is read from the history: the records of every transaction after
        the one the etags are made through (`_etags` in `ids`), so a write made outside
        the store is made up for by the next one. The seeds are those records, and, for
        one that is new, gone or has another resource uri, the records that point at it
        by its uri or its resource uri (they pointed at what was not there yet, or is
        not now). An etag is made again for the seeds, and for every record made of one
        (one that has a seed in its `deep_references`, found before they change):

          * a seed is walked again from the references (`_closure_etags`);
          * a record made of seeds is walked again too when there are few of them
            (`ETAGS_DELTA_ABOVE`), or when what a seed points at changed;
          * otherwise (a hub that was renamed, say) its etag is moved by the change of
            the local hashes of the seeds it is made of, which costs one update of a
            column.

        All the records are made again only when there are no etags yet (the first time,
        or the numbers of the types changed). There is no limit on how many records a
        write changes: an incremental refresh was measured at 1,000,000 records to cost
        0.4 to 1.3 s for 1,000 to 100,000 changed records, and a full rebuild 5.5 s
        whatever changed."""
        current = self.last_transaction()
        marker = self._etags_marker()
        empty = not fetch_one(self.con.execute("SELECT count(*) FROM etags"))[0]
        if marker >= current and not empty:
            return
        if empty:
            self._rebuild_etags()
            return
        refs = f"{self.records}_references"
        self.con.execute("DELETE FROM etags_seed")
        self.con.execute(
            f"""INSERT INTO etags_seed
                WITH changed AS (
                    SELECT DISTINCT record_id AS id FROM history WHERE txn_id >
                      {int(marker)}),
                ref_changers AS (
                    SELECT c.id FROM changed c
                    LEFT JOIN etags o ON o.record_id = c.id
                    LEFT JOIN {self.records} n ON n.record_id = c.id
                    WHERE o.record_id IS NULL OR n.record_id IS NULL
                          OR o.res_uri IS DISTINCT FROM n.res_uri),
                keys AS (
                    SELECT o.res_uri AS k FROM etags o
                          WHERE o.record_id IN (SELECT id FROM ref_changers)
                            AND o.res_uri IS NOT NULL
                    UNION SELECT n.res_uri FROM {self.records} n
                          WHERE n.record_id IN (SELECT id FROM ref_changers)
                            AND n.res_uri IS NOT NULL)
                SELECT id FROM changed
                UNION SELECT rr.record_id FROM {refs} rr WHERE rr.reference IN (SELECT k
                  FROM keys)"""
        )
        seeds = fetch_one(self.con.execute("SELECT count(*) FROM etags_seed"))[0]
        self._fresh_references()
        ids = "SELECT record_id AS id FROM etags_seed"
        seed_ids = [r[0] for r in self.con.execute(ids).fetchall() if r[0] is not None]
        if seeds <= ETAGS_CONTAINS_UP_TO:
            made_of = (
                " UNION SELECT e.record_id FROM etags e WHERE "
                + " OR ".join(
                    f"list_contains(e.deep_references, {int(i)})" for i in seed_ids
                )
                if seed_ids
                else ""
            )
        else:
            made_of = (
                " UNION SELECT DISTINCT e.record_id FROM (SELECT record_id, "
                "unnest(deep_references) AS d "
                "FROM etags) e JOIN s ON s.id = e.d"
            )
        self.con.execute("DELETE FROM etags_todo")
        self.con.execute(
            f"""INSERT INTO etags_todo
                WITH s AS ({ids})
                SELECT id FROM s WHERE id IS NOT NULL{made_of}"""
        )
        made_of_seeds = fetch_one(self.con.execute("SELECT count(*) FROM etags_todo"))[
            0
        ] - len(set(seed_ids))
        if made_of_seeds > ETAGS_DELTA_ABOVE:
            self._by_delta()
        else:
            self._closure_etags()
            self._replace_etags()
        self._sort_etags()
        self._queue_ref_updates(current, marker)
        self._clear_etags_work()
        self._set_etags_marker(current)

    def catch_up_etags(self) -> bool:
        """Make the etags of what was written outside the store (the DuckDB shell: the
        triggers kept the records, the history and the queue, but no refresh ran), in a
        transaction of its own. Returns whether there was anything to catch up on; two
        small queries when there was not."""
        if self._etags_marker() >= self.last_transaction() and self.count_etags():
            return False
        self.con.begin()
        try:
            self.refresh_etags()
            self.con.commit()
        except BaseException:
            self._rollback()
            raise
        return True

    def _queue_ref_updates(self, transaction: int, marker: int) -> None:
        """`ref_update` rows (see `actions_queue` in records.sql) for the records whose
        page was made again by the refresh that has just run (`etags_todo`, and the
        records whose etag the delta moved) and that no write since `marker` queued
        already: one statement, however many there are."""
        self.con.execute(
            f"""INSERT INTO actions_queue
                SELECT {int(transaction)}, id, 'ref_update' FROM (
                    SELECT record_id AS id FROM etags_todo
                    UNION SELECT record_id FROM etags_holders WHERE delta <> 0 AND NOT
                      bad)
                WHERE id NOT IN (SELECT record_id FROM actions_queue WHERE
                  transaction_id > {int(marker)})"""
        )

    def _sort_etags(self) -> None:
        """Put `etags` in the order a listing reads it (`ETAGS_ORDER`), in the
        transaction of the write: a row that was replaced is at the end of the table,
        and a record that was edited or made is somewhere else than where it was. The
        table is made again in order and takes the place of the old one (measured at
        1,000,000 records: about 110 MB written, 0.25 to 0.45 s, and a commit of 30 to
        65 ms). Its data is written to the database file again, and the blocks of the
        old table are used again: the file does not grow."""
        self.con.execute(
            f"""CREATE TABLE etags_sorted AS
                SELECT e.* REPLACE (coalesce(n.backref_totals, e.backref_totals) AS
                  backref_totals)
                FROM etags e LEFT JOIN etags_backnew n USING (record_id)
                  {ETAGS_ORDER}"""
        )
        self.con.execute("DROP TABLE etags")
        self.con.execute("ALTER TABLE etags_sorted RENAME TO etags")

    def _replace_etags(self) -> None:
        """The etags in `etags_new` instead of those of the records in `etags_todo`.
        What the seeds point at, and whether they are published, is compared first
        (their old rows are about to go): that is what changes the totals of the records
        they point at."""
        self._backref_changes()
        self.con.execute(
            "DELETE FROM etags WHERE record_id IN (SELECT record_id FROM etags_todo)"
        )
        self.con.execute("INSERT INTO etags SELECT * FROM etags_new")
        self._backref_totals()

    def _backref_changes(self) -> None:
        """Into `etags_backdelta`: for each record that a seed points at now and did
        not, one more (by the type of the seed, and in the published ones if it is), and
        one less for each that it pointed at and does not, or whose published flag it
        changed. A seed that was made counts all it points at, one that was deleted
        takes all it pointed at off. Only seeds can change what a record points at; the
        others in `etags_new` are the records made of them, with the same references."""
        seeds = "SELECT DISTINCT record_id FROM etags_seed"
        self.con.execute("DELETE FROM etags_backdelta")
        self.con.execute(
            f"""INSERT INTO etags_backdelta
                SELECT t, record_type, sum(a), sum(p) FROM (
                    SELECT unnest("references") AS t, record_type, 1 AS a,
                           CASE WHEN is_published THEN 1 ELSE 0 END AS p
                    FROM etags_new WHERE record_id IN ({seeds})
                    UNION ALL
                    SELECT unnest("references"), record_type, -1, CASE WHEN is_published
                      THEN -1 ELSE 0 END
                    FROM etags WHERE record_id IN ({seeds}))
                GROUP BY t, record_type HAVING sum(a) <> 0 OR sum(p) <> 0"""
        )

    def _backref_totals(self) -> None:
        """Into `etags_backnew`: the totals of the records in `etags_backdelta` with the
        changes put in: the totals the rows in `etags` have now (a seed keeps its own,
        see `_closure_etags`) and the changes, by type, the types with none left taken
        out. `_sort_etags` puts them in the rows."""
        self.con.execute("DELETE FROM etags_backnew")
        self.con.execute(
            f"""INSERT INTO etags_backnew
                WITH old AS (
                    SELECT record_id AS t, unnest(map_entries(backref_totals)) AS en
                      FROM etags
                    WHERE record_id IN (SELECT record_id FROM etags_backdelta)),
                merged AS (
                    SELECT t, source_type, sum(a) AS a, sum(p) AS p FROM (
                        SELECT t, en.key AS source_type, en.value.total_all AS a,
                          en.value.total_published AS p FROM old
                        UNION ALL SELECT record_id, source_type, d_all, d_pub FROM
                          etags_backdelta)
                    GROUP BY t, source_type HAVING sum(a) > 0)
                SELECT d.record_id,
                       coalesce(map_from_entries(list({{'key': m.source_type,
                                'value':
                                  {{'total_all': m.a,
                                    'total_published': m.p}}}}
                                ORDER BY m.source_type)
                                FILTER (WHERE m.t IS NOT NULL)), {EMPTY_TOTALS})
                FROM (SELECT DISTINCT record_id FROM etags_backdelta) d
                LEFT JOIN merged m ON m.t = d.record_id
                GROUP BY d.record_id"""
        )

    def _by_delta(self) -> None:
        """Many records are made of the seeds (`etags_todo` has them and those): the
        seeds are walked again, and so are the records made of a seed whose references
        changed (the set of records they are made of may be another one). The etag of
        every other record made of seeds is XORed with the change of the local hashes of
        the seeds it is made of, and its latest time can only have moved up: the time of
        a record is never set back (see `gracia_modified_at.py`)."""
        self.con.execute("DELETE FROM etags_delta")
        self.con.execute(
            f"""INSERT INTO etags_delta
                SELECT s.id, o.local_hash,
                       CASE WHEN n.record_id IS NULL THEN NULL ELSE hash(n.record_id,
                         n.modified_at) END,
                       xor(coalesce(o.local_hash, 0::UBIGINT),
                           CASE WHEN n.record_id IS NULL THEN 0::UBIGINT ELSE
                             hash(n.record_id, n.modified_at) END),
                       n.record_id IS NULL OR o.record_id IS NULL
                           OR o."references" IS DISTINCT FROM coalesce(f.refs,
                             []::INTEGER[]),
                       n.modified_at, coalesce(n.bytes, 0) - coalesce(o.bytes, 0)
                FROM (SELECT DISTINCT record_id AS id FROM etags_seed) s
                LEFT JOIN etags o ON o.record_id = s.id
                LEFT JOIN {self.records} n ON n.record_id = s.id
                LEFT JOIN etags_refs f ON f.record_id = s.id
                WHERE s.id IS NOT NULL"""
        )
        self.con.execute("DELETE FROM etags_holders")
        self.con.execute(
            """INSERT INTO etags_holders
               SELECT e.record_id, bit_xor(d.delta), bool_or(d.struct),
                 max(d.new_modified), sum(d.bytes_delta)
               FROM (SELECT record_id, unnest(deep_references) AS x FROM etags) e
               JOIN etags_delta d ON d.record_id = e.x
               WHERE e.record_id NOT IN (SELECT record_id FROM etags_delta)
               GROUP BY e.record_id"""
        )
        self.con.execute("DELETE FROM etags_todo")
        self.con.execute("INSERT INTO etags_todo SELECT record_id FROM etags_delta")
        self.con.execute(
            "INSERT INTO etags_todo SELECT record_id FROM etags_holders WHERE bad"
        )
        self._closure_etags()
        self._replace_etags()
        self.con.execute(
            """UPDATE etags
               SET etag = xor(etags.etag, h.delta),
                   last_modified_at = greatest(etags.last_modified_at, h.newest,
                       CASE WHEN h.delta <> 0 THEN now() AT TIME ZONE 'UTC' END),
                   closure_bytes = etags.closure_bytes + h.bytes_delta
               FROM etags_holders h
               WHERE etags.record_id = h.record_id AND NOT h.bad"""
        )

    def _rebuild_etags(self) -> None:
        """Every etag again, from `records` and `records_references` (in the transaction
        of the caller): what `refresh_etags` is checked against. Nothing when no type
        has a number."""
        self.con.execute("DELETE FROM etags")
        self.con.execute("DELETE FROM etags_seed")
        self.con.execute(f"INSERT INTO etags_seed SELECT record_id FROM {self.records}")
        self.con.execute("DELETE FROM etags_todo")
        self.con.execute(f"INSERT INTO etags_todo SELECT record_id FROM {self.records}")
        self._fresh_references()
        self._closure_etags()
        self.con.execute("DELETE FROM etags_backnew")
        self.con.execute(
            """INSERT INTO etags_backnew
                SELECT t, map_from_entries(list({'key': record_type,
                           'value':
                             {'total_all': a, 'total_published': p}}
                           ORDER BY record_type))
                FROM (SELECT t, record_type, count(*) AS a, count(*) FILTER (WHERE
                  is_published) AS p
                      FROM (SELECT unnest("references") AS t, record_type, is_published
                        FROM etags_new)
                      GROUP BY t, record_type)
                GROUP BY t"""
        )
        # what a rebuild makes of a page is what the write made of it: the time does not
        # go down,
        # and is
        # the time of the transaction when the etag is not the one it had
        # (`etags_before` has the
        # etags
        # and times the rows had; it is empty for a first build)
        self.con.execute(
            f"""INSERT INTO etags
                SELECT n.* REPLACE (coalesce(b.backref_totals, n.backref_totals) AS
                  backref_totals,
                                    CASE WHEN o.etag IS NOT NULL AND o.etag <> n.etag
                                         THEN greatest(n.last_modified_at,
                                           o.last_modified_at, now() AT TIME ZONE 'UTC')
                                         ELSE greatest(n.last_modified_at,
                                           o.last_modified_at) END AS last_modified_at)
                FROM etags_new n LEFT JOIN etags_backnew b USING (record_id)
                LEFT JOIN etags_before o ON o.record_uri = n.record_uri {ETAGS_ORDER}"""
        )
        self._clear_etags_work()
        self._set_etags_marker(self.last_transaction())
        # the etags may be other than they were, with no change in the triples to say so
        # (the
        # triggers
        # keep this row for those): whoever caches them (`cache.Cache`) has to hear of
        # it
        self.con.execute(self._modified_upsert())

    def rebuild_etags(self) -> list[str]:
        """Make every etag again, in one transaction. Returns the records whose etag was
        not what it is now (one that was missing, gone or different), sorted: none when
        they were all right, which is what a write that keeps them up to date leaves."""
        self.con.begin()
        try:
            self.con.execute("DELETE FROM etags_before")
            self.con.execute(
                "INSERT INTO etags_before SELECT record_uri, etag, last_modified_at "
                "FROM etags"
            )
            self._rebuild_etags()
            # the pages that are not what they were are changes to the followers
            self.con.execute(
                f"""INSERT INTO actions_queue
                    SELECT {self.last_transaction()}, a.record_id, 'ref_update' FROM
                      etags a
                    LEFT JOIN etags_before b ON b.record_uri = a.record_uri
                    WHERE b.etag IS DISTINCT FROM a.etag"""
            )
            rows = self.con.execute(
                """SELECT a.record_uri FROM etags a LEFT JOIN etags_before b ON
                  b.record_uri = a.record_uri
                   WHERE b.etag IS DISTINCT FROM a.etag
                   UNION SELECT record_uri FROM etags_before
                         WHERE record_uri NOT IN (SELECT record_uri FROM etags)
                   ORDER BY 1"""
            ).fetchall()
            self.con.execute("DELETE FROM etags_before")
            self.con.commit()
        except BaseException:
            self._rollback()
            raise
        return [r[0] for r in rows]

    def count_etags(self) -> int:
        """How many records have an etag."""
        return fetch_one(self.con.execute("SELECT count(*) FROM etags"))[0]

    def _clear_etags_work(self) -> None:
        for table in (
            "etags_seed",
            "etags_todo",
            "etags_refs",
            "etags_new",
            "etags_delta",
            "etags_holders",
            "etags_backdelta",
            "etags_backnew",
        ):
            self.con.execute(f"DELETE FROM {table}")

    def _fresh_references(self) -> None:
        """Into `etags_refs`: the direct references of the records in `etags_seed`, read
        again from `records_references` (an object of the resource that is the resource
        uri of a record, not the record itself): the ids, sorted. The other records keep
        the ones in `etags`, which are right, because a record whose references could
        have changed is a seed."""
        refs = f"{self.records}_references"
        self.con.execute("DELETE FROM etags_refs")
        self.con.execute(
            f"""INSERT INTO etags_refs
                WITH src AS (
                    SELECT r.record_id FROM {self.records} r
                    WHERE r.record_id IN (SELECT record_id FROM etags_seed)),
                hit AS (
                    SELECT rr.record_id AS s, t.record_id AS t FROM {refs} rr
                    JOIN {self.records} t ON t.res_uri = rr.reference AND t.record_id <>
                      rr.record_id
                    WHERE rr.record_id IN (SELECT record_id FROM src))
                SELECT src.record_id,
                       coalesce(list_sort(list(hit.t) FILTER (WHERE hit.t IS NOT NULL)),
                         []::INTEGER[])
                FROM src LEFT JOIN hit ON hit.s = src.record_id
                GROUP BY src.record_id"""
        )

    def _closure_etags(self) -> None:
        """Into `etags_new`: the etags of the records in `etags_todo`, each walked from
        its direct references (`etags_refs` for a seed, else those in `etags`).

        `last_modified_at` is the latest `modified_at` of the records walked, never
        earlier than the one the record has, and the time of the transaction when the
        etag changed: a page that loses a record it was made of (one that was deleted,
        or that has another resource uri) has another etag and so a new time, and one
        whose etag is the same keeps its time. So the time of a page only goes up, and
        goes up whenever the etag changes."""
        self.con.execute("DELETE FROM etags_new")
        self.con.execute(
            f"""INSERT INTO etags_new
                WITH RECURSIVE clo(root, dep) AS (
                    SELECT record_id, record_id FROM etags_todo
                    UNION
                    SELECT c.root, unnest(coalesce(f.refs, e."references")) FROM clo c
                    LEFT JOIN etags_refs f ON f.record_id = c.dep
                    LEFT JOIN etags e ON e.record_id = c.dep),
                walked(record_id, record_uri, record_type, res_uri, sort_key,
                  is_published, etag, local_hash,
                       modified_at, last_modified_at, "references", deep_references,
                         deep_types, backref_totals,
                       bytes, closure_bytes, old_etag) AS (
                SELECT c.root, record_uri(c.root), r0.rec_type, r0.res_uri,
                       epoch_us(r0.created_at), r0.is_published,
                       bit_xor(hash(r.record_id, r.modified_at)),
                       hash(c.root, r0.modified_at),
                       r0.modified_at,
                       greatest(max(r.modified_at), any_value(e0.last_modified_at)),
                       coalesce(any_value(f0.refs), any_value(e0."references"),
                         []::INTEGER[]),
                       coalesce(list(c.dep ORDER BY c.dep) FILTER (WHERE c.dep <>
                         c.root), []::INTEGER[]),
                       list(DISTINCT r.rec_type ORDER BY r.rec_type),
                       coalesce(any_value(e0.backref_totals), {EMPTY_TOTALS}),
                       r0.bytes, sum(r.bytes),
                       any_value(e0.etag) AS old_etag
                FROM clo c
                JOIN {self.records} r ON r.record_id = c.dep
                JOIN {self.records} r0 ON r0.record_id = c.root
                LEFT JOIN etags_refs f0 ON f0.record_id = c.root
                LEFT JOIN etags e0 ON e0.record_id = c.root
                GROUP BY c.root, r0.rec_type, r0.res_uri, r0.created_at, r0.modified_at,
                  r0.is_published, r0.bytes)
                SELECT * EXCLUDE (old_etag) REPLACE (
                    CASE WHEN old_etag IS NOT NULL AND old_etag <> etag
                         THEN greatest(last_modified_at, now() AT TIME ZONE 'UTC')
                         ELSE last_modified_at END AS last_modified_at)
                FROM walked"""
        )

    def record_id_of(self, rec_uri: str) -> int | None:
        """The id of a record in `etags` (`uri.record_id`), or None: not a record uri,
        or its type has no number. The numbers are read once per store."""
        parsed = uri.parse_record_uri(rec_uri)
        if parsed is None:
            return None
        number = self.type_numbers().get(parsed[0])
        if number is None:
            return None
        try:
            return uri.record_id(number, parsed[1])
        except ValueError:
            return None

    def etag_info(self, rec_uri: str) -> tuple[str, list[str]] | None:
        """The etag of a record's page and the slugs of the types it is made of, or None
        when there is none (the record is not there, or the repository has not made its
        etags yet). By id: there is no index on the uri, see etags.sql."""
        record_id = self.record_id_of(rec_uri)
        if record_id is None:
            return None
        row = self.con.execute(
            f"SELECT etag, deep_types FROM etags WHERE record_id = {record_id}"
        ).fetchone()
        return (f"{row[0]:016x}", list(row[1])) if row else None

    def closure_sources(self, record_ids) -> dict[int, tuple[str, str]]:
        """For each record that has an etag, `(the etag of its page, the N-Triples of
        the record and of every record it is made of)`: its `references` and
        `deep_references`, the record first and the others by id. All read from one
        state of the database, so that each etag belongs to its text. Two steps, because
        one statement that joins `etags` and `records` read the whole of `records` (all
        the sources) for every call: the ids of the parts come from `etags`, and the
        sources are then looked up by id."""
        wanted = list(dict.fromkeys(int(i) for i in record_ids))
        if not wanted:
            return {}
        self.con.begin()
        try:
            listed = ", ".join(map(str, wanted))
            pairs = self.con.execute(
                "SELECT record_id, etag, unnest(list_distinct(list_concat([record_id], "
                '"references", '
                f"deep_references))) FROM etags WHERE record_id IN ({listed})"
            ).fetchall()
            parts = sorted({part for _, _, part in pairs})
            sources: dict[int, str] = {}
            for start in range(
                0, len(parts), 5000
            ):  # a list of ids that is not too long to read
                chunk = ", ".join(map(str, parts[start : start + 5000]))
                sources.update(
                    self.con.execute(
                        f"SELECT record_id, source FROM {self.records} WHERE record_id "
                        f"IN ({chunk})"
                    ).fetchall()
                )
        finally:
            self.con.commit()
        etags: dict[int, int] = {}
        made: dict[int, list[int]] = {}
        for record_id, etag, part in pairs:
            etags[record_id] = etag
            made.setdefault(record_id, []).append(part)
        return {
            record_id: (
                f"{etags[record_id]:016x}",
                "\n".join(
                    sources[p]
                    for p in sorted(made[record_id], key=lambda p: (p != record_id, p))
                    if p in sources
                ),
            )
            for record_id in wanted
            if record_id in etags
        }

    def closure_source(self, record_id: int) -> tuple[str, str] | None:
        """`closure_sources` of one record: None when it has no etag."""
        return self.closure_sources([record_id]).get(record_id)

    # ---- the queue of what the followers have to do (records.sql, gracia.actions)
    # ---------------

    def take_actions(
        self, limit: int = 100, max_bytes: int | None = None
    ) -> tuple[list[tuple[int, str, str]], int]:
        """The next records the followers have to look at: `(record id, record uri,
        action)` for the records of the oldest `limit` rows of `actions_queue`, oldest
        first, with the newest action the queue has for each (a delete after an update
        is a delete), and the number of the last transaction at that moment: what
        `actions_done` needs, so that what is queued after this read is not taken off.
        Nothing is removed here.

        With `max_bytes` the records are taken until the sources of what they are made
        of (`etags.closure_bytes`) would be more than that, though the first is always
        taken: a batch is measured in bytes, so a hundred small records and a few with
        five hundred authors each are both a batch a handler can hold."""
        self.con.begin()
        try:
            bound = self.last_transaction()
            oldest = self.con.execute(
                f"SELECT q.record_id, coalesce(e.closure_bytes, 0) FROM "
                f"(SELECT record_id FROM actions_queue WHERE transaction_id <= {bound} "
                f"ORDER BY transaction_id, record_id LIMIT {int(limit)}) q "
                f"LEFT JOIN etags e ON e.record_id = q.record_id"
            ).fetchall()
            sizes = {}
            for (
                record_id,
                size,
            ) in oldest:  # in the order of the queue, each record once
                sizes.setdefault(record_id, size)
            ids, total = [], 0
            for record_id, size in sizes.items():
                if max_bytes is not None and ids and total + size > max_bytes:  # ty: ignore[unsupported-operator]
                    break
                ids.append(record_id)
                total += size  # ty: ignore[unsupported-operator]
            if not ids:
                return [], bound
            listed = ", ".join(map(str, ids))
            newest = dict(
                self.con.execute(
                    "SELECT record_id, arg_max(action, transaction_id) FROM "
                    "actions_queue "
                    f"WHERE record_id IN ({listed}) AND transaction_id <= {bound} "
                    "GROUP BY "
                    "record_id"
                ).fetchall()
            )
            uris = dict(
                self.con.execute(
                    "SELECT record_id, record_uri(record_id) FROM (SELECT "
                    f"unnest([{listed}]) AS "
                    "record_id)"
                ).fetchall()
            )
        finally:
            self.con.commit()
        return [(i, uris[i], newest[i]) for i in ids], bound

    def actions_done(self, record_ids, bound: int) -> None:
        """Take what was handled off the queue: every row of these records up to
        transaction `bound` (what `take_actions` said), since the handler read the
        records as they were at least then."""
        listed = ", ".join(str(int(i)) for i in dict.fromkeys(record_ids))
        if listed:
            self.con.execute(
                f"DELETE FROM actions_queue WHERE record_id IN ({listed}) AND "
                "transaction_id <= "
                f"{int(bound)}"
            )

    def requeue_changes_since(self, bound: int) -> int:
        """Queue again everything that changed after transaction `bound`: the records
        written (`update`, or `delete` for one that is gone) and the pages that changed
        because something they are made of did (`ref_update`). For whatever was made
        from a state of the data at `bound` (a search index built while writes went on)
        to be brought up to date by the worker. Returns how many rows were queued."""
        (since,) = fetch_one(
            self.con.execute(
                "SELECT max(modified_at) FROM history WHERE txn_id = ?", [int(bound)]
            )
        )
        transaction = self.last_transaction()
        before = fetch_one(self.con.execute("SELECT count(*) FROM actions_queue"))[0]
        self.con.execute(
            f"""INSERT INTO actions_queue
                WITH written AS (
                    SELECT record_id, arg_max(is_deleted, txn_id) AS gone FROM history
                    WHERE txn_id > {int(bound)} GROUP BY record_id)
                SELECT {transaction}, record_id, CASE WHEN gone THEN 'delete' ELSE
                  'update' END FROM written
                UNION ALL
                SELECT {transaction}, e.record_id, 'ref_update' FROM etags e
                WHERE ? IS NOT NULL AND e.last_modified_at > ?
                  AND e.record_id NOT IN (SELECT record_id FROM written)""",
            [since, since],
        )
        return (
            fetch_one(self.con.execute("SELECT count(*) FROM actions_queue"))[0]
            - before
        )

    def pending_size(self) -> tuple[int, int]:
        """How many records wait in the queue (each once), and the size of the sources
        of what they are made of in bytes (`etags.closure_bytes`): what the work of the
        queue is."""
        records, size = fetch_one(
            self.con.execute(
                "SELECT count(*), coalesce(sum(e.closure_bytes), 0) FROM "
                "(SELECT DISTINCT record_id FROM actions_queue) q LEFT JOIN etags e ON "
                "e.record_id "
                "= q.record_id"
            )
        )
        return records, int(size)

    def pending_actions(self) -> tuple[int, int | None]:
        """How many rows wait in the queue, and the number of the oldest transaction
        among them."""
        count, oldest = fetch_one(
            self.con.execute("SELECT count(*), min(transaction_id) FROM actions_queue")
        )
        return count, oldest

    def referrers(
        self,
        target_uri: str,
        *,
        source_type: str | None = None,
        page: int | None = None,
        size: int = PAGE_SIZE,
        published: bool = True,
    ) -> Page:
        """The records that point at a record, directly, a page of them: `page=N`, page
        1 the newest created and the pages going back in time (`page_window`); page 1
        when it is not given. The records on a page are newest first. Only those of
        `source_type` (a type slug) when it is given, and only the published ones unless
        `published` is False. The pages count the records asked for, but the page says
        how many there are of both kinds (`total_all`, `total_published`).

        Page numbers are the ordinary kind, and not stable: a record that is added moves
        every record down a place, so every page changes (and its etag with it). The
        other way, numbering from the oldest, keeps old pages as they are, and was given
        up because it confused people.

        `etags` is kept in the order of `ETAGS_ORDER` (see `_sort_etags`), so a page is
        read with LIMIT and OFFSET and no ORDER BY, and the scan stops after the page:
        page 1 is the cheapest, and a deep page costs a scan of the newer records of the
        listing (14 ms for the last page of 800,000 at 1,000,000 records). `ValueError`
        for a page that is not there."""
        target = self.record_id_of(target_uri)
        where = (
            [f"record_type = {literal(source_type)}"] if source_type is not None else []
        )
        where.append(
            "false" if target is None else f'list_contains("references", {int(target)})'
        )
        condition = " AND ".join(where)
        self.con.begin()  # the totals and the page from one state of the table
        try:
            # the totals are in the row of the record: no count of the listing
            found = (
                self.con.execute(
                    f"SELECT en.key, en.value.total_all, en.value.total_published FROM "
                    "(SELECT unnest(map_entries(backref_totals)) AS en FROM etags "
                    "WHERE record_id "
                    f"= {int(target)})"
                ).fetchall()
                if target is not None
                else []
            )
            kept = [
                (a, p)
                for key, a, p in found
                if source_type is None or key == source_type
            ]
            total_all, total_published = (
                sum(a for a, _ in kept),
                sum(p for _, p in kept),
            )
            total = total_published if published else total_all
            page, pages, offset, limit = page_window(total, page, size)
            shown = f"{condition} AND is_published" if published else condition
            rows = (
                self.con.execute(
                    f"SELECT record_uri, etag FROM etags WHERE {shown} LIMIT {limit} "
                    "OFFSET "
                    f"{offset}"
                ).fetchall()
                if limit
                else []
            )
        finally:
            self.con.commit()
        return Page(
            [(u, f"{e:016x}") for u, e in rows],
            page,
            pages,
            total,
            total_all,
            total_published,
            size,
        )

    def records_page(
        self,
        slug: str,
        *,
        page: int | None = None,
        size: int = PAGE_SIZE,
        published: bool = True,
    ) -> Page:
        """A page of the records of a type (its slug), numbered like `referrers`: page 1
        is the newest created, and the pages go back in time. Read from `etags`, which
        is kept in the order of `ETAGS_ORDER` (the records of a type are one run of it,
        newest created first); a repository that has no etags for the type yet is read
        from `records` instead, which has to sort them (the same order, no etag).
        `ValueError` for a page that is not there."""
        where = f"record_type = {literal(slug)}"
        counts = self.con.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_published) FROM etags WHERE "
            f"{where}"
        ).fetchone()
        stored = counts[0] > 0  # ty: ignore[not-subscriptable]
        if not stored:
            where = f"rec_type = {literal(slug)}"
            counts = self.con.execute(
                "SELECT count(*), count(*) FILTER (WHERE is_published) FROM "
                f"{self.records} WHERE "
                f"{where}"
            ).fetchone()
        total_all, total_published = counts  # ty: ignore[not-iterable]
        total = total_published if published else total_all
        page, pages, offset, limit = page_window(total, page, size)
        shown = f"{where} AND is_published" if published else where
        if not limit:
            rows = []
        elif stored:
            rows = self.con.execute(
                f"SELECT record_uri, etag FROM etags WHERE {shown} LIMIT {limit} "
                f"OFFSET {offset}"
            ).fetchall()
        else:
            rows = [
                (u, 0)
                for (u,) in self.con.execute(
                    f"SELECT record_uri(record_id) FROM {self.records} WHERE {shown} "
                    f"ORDER BY created_at DESC, record_id LIMIT {limit} OFFSET {offset}"
                ).fetchall()
            ]
        return Page(
            [(u, f"{e:016x}" if e else "") for u, e in rows],
            page,
            pages,
            total,
            total_all,
            total_published,
            size,
        )

    def labels(self, uris: list[str]) -> dict[str, tuple[str, str | None]]:
        """The label and the slug of records (`records`): {uri: (label, slug)}, an empty
        label when a record has none."""
        if not uris:
            return {}
        rows = self.con.execute(
            f"SELECT record_uri(record_id), label, slug FROM {self.records} "
            f"WHERE record_id IN {sql_ids(uris)}"
        ).fetchall()
        return {u: (label or "", slug) for u, label, slug in rows}

    def page_uris(self, rec_uri: str) -> list[str]:
        """The record and every record its page is made of, sorted, from `etags` (empty:
        none)."""
        record_id = self.record_id_of(rec_uri)
        if record_id is None:
            return []
        rows = self.con.execute(
            f"""SELECT record_uri FROM etags WHERE record_id = {record_id}
                UNION SELECT e.record_uri FROM etags e
                      WHERE e.record_id IN (SELECT unnest(deep_references) FROM etags
                                            WHERE record_id = {record_id})
                ORDER BY 1"""
        ).fetchall()
        return [r[0] for r in rows]

    def dependencies(self, rec_uri: str, max_depth: int = 10) -> dict[str, object]:
        """A record and every record its page is made of, with when each was last
        changed: the record, the records that its resource points at (an object that is
        the resource uri of a record), the records those point at, and so on,
        `max_depth` steps out (a record is looked at once, so a cycle ends). From
        `records_references` and `records`, not from the triples. A deleted record is
        not one."""
        references, records = f"{self.records}_references", self.records
        first = self.record_id_of(rec_uri)
        if first is None:
            return {}
        found = {first}
        frontier = [first]
        for _step in range(max_depth):
            if not frontier:
                break
            some = f"rr.record_id IN (SELECT unnest({list(frontier)}::INTEGER[]))"
            rows = self.con.execute(
                f"SELECT r.record_id FROM {references} rr JOIN {records} r ON "
                "r.res_uri = "
                "rr.reference "
                f"WHERE {some}"
            ).fetchall()
            frontier = [id_ for (id_,) in rows if id_ not in found]
            found.update(frontier)
        rows = self.con.execute(
            f"SELECT record_uri(record_id), modified_at FROM {records} "
            f"WHERE record_id IN (SELECT unnest({sorted(found)}::INTEGER[]))"
        ).fetchall()
        return dict(rows)

    def slug_lookup(self, rec_type: str, slug: str) -> tuple[str, bool] | None:
        """The record that has this slug, or had it, in the address of a type: (its uri,
        whether it is deleted), or None when no record ever had it. (A number is a slug
        of a deleted record only: `rec:person:23` that was deleted has the slug
        `23`.)"""
        row = self.con.execute(
            "SELECT record_uri, is_deleted FROM slugs WHERE rec_type = "
            f"{literal(rec_type)} "
            f"AND slug = {literal(slug)}"
        ).fetchone()
        return (row[0], bool(row[1])) if row else None

    def current_slug(self, rec_uri: str) -> str | None:
        """The slug a record has now (`gracia:slug` of the record), or None."""
        row = self.con.execute(
            f"SELECT slug FROM {self.records} WHERE record_id = "
            f"record_id({literal(rec_uri)})"
        ).fetchone()
        return row[0] if row else None

    def slugs_of(self, rec_uri: str) -> list[str]:
        """Every slug a record has had, the one it has now too, in alphabetical order
        (the number a deleted record is given is not one of them)."""
        rows = self.con.execute(
            f"SELECT slug FROM slugs WHERE record_uri = {literal(rec_uri)} "
            "AND NOT regexp_matches(slug, '^[0-9]+$') ORDER BY slug"
        ).fetchall()
        return [r[0] for r in rows]

    def purge_slugs(self, rec_type: str) -> list[tuple[str, str]]:
        """Forget the old slugs of the live records of a type: what is left is the slug
        each has now and everything that belongs to deleted records (their addresses
        stay gone, 410). The addresses that were purged answer 404 from then on. Returns
        what went: (slug, record uri)."""
        if not TYPE_SLUG.match(rec_type):
            raise ValueError(f"invalid type slug: {rec_type!r}")
        return self.con.execute(
            f"DELETE FROM slugs WHERE rec_type = {literal(rec_type)} AND NOT "
            "is_deleted "
            f"AND slug IS DISTINCT FROM (SELECT r.slug FROM {self.records} r "
            "WHERE r.record_id = record_id(slugs.record_uri)) RETURNING slug, "
            "record_uri"
        ).fetchall()

    def record_modified(self, rec_uri: str):
        """When a record was last changed (UTC, a naive datetime), None for one that is
        not there."""
        row = self.con.execute(
            f"SELECT modified_at FROM {self.records} WHERE record_id = "
            f"record_id({literal(rec_uri)})"
        ).fetchone()
        return row[0] if row else None

    def revisions(
        self, rec_uri: str, limit: int = 200
    ) -> list[tuple[int, object, bool]]:
        """The versions of a record there are in the history, newest first, as
        (transaction, when, is_deleted)."""
        rows = self.con.execute(
            "SELECT txn_id, modified_at, is_deleted FROM history "
            f"WHERE record_id = record_id({literal(rec_uri)}) ORDER BY txn_id DESC "
            "LIMIT "
            f"{int(limit)}"
        ).fetchall()
        return [(t, when, bool(deleted)) for t, when, deleted in rows]

    def revision(self, rec_uri: str, txn_id: int) -> tuple[object, bool, str] | None:
        """One version of a record: (when, is_deleted, its N-Triples), or None."""
        row = self.con.execute(
            "SELECT modified_at, is_deleted, source FROM history "
            f"WHERE record_id = record_id({literal(rec_uri)}) AND txn_id = "
            f"{int(txn_id)} "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return (row[0], bool(row[1]), row[2]) if row else None

    def revision_before(self, rec_uri: str, txn_id: int) -> int | None:
        """The transaction of the version before the one in `txn_id`, or None for the
        first."""
        row = self.con.execute(
            "SELECT max(txn_id) FROM history WHERE record_id = "
            f"record_id({literal(rec_uri)}) "
            f"AND txn_id < {int(txn_id)}"
        ).fetchone()
        return row[0] if row else None

    def changes_since(
        self, number: int, limit: int | None = None
    ) -> list[tuple[int, str, bool]]:
        """What changed in the transactions after `number`, as (transaction, record,
        is_deleted) in the order of the transactions: for something that follows the
        changes (the search index) and remembers the last number it has seen. A record
        that changed twice in one transaction is there once."""
        more = f" LIMIT {int(limit)}" if limit else ""
        rows = self.con.execute(
            "SELECT txn_id, record_uri(record_id), is_deleted FROM history WHERE "
            "txn_id > "
            f"{int(number)} "
            f"ORDER BY txn_id, record_id{more}"
        ).fetchall()
        return [(t, uri_, bool(d)) for t, uri_, d in rows]

    def drop_staging(self) -> None:
        self.con.execute(f"DROP TABLE IF EXISTS {self.staging}")
        self.con.execute(f"DROP TABLE IF EXISTS {self.flat}")

    # ---- hooks
    # ---------------------------------------------------------------------------

    def configure(self, provider) -> None:
        """Say how the store is told what a repository knows about its records:
        `provider()` is called when something is written, and gives (the SQL of the
        hooks of the predicates, the property roles of the types, and optionally the
        numbers of the types, `set_type_ids`). The hooks are run on every batch that is
        stored, see `flatten`; the roles are put in the database when they changed
        (`set_property_roles`)."""
        self._provider = provider
        self.hooks = self._roles = None

    def sync(self) -> None:
        """Put the property roles the repository knows in the database now (after its
        types changed), instead of before the next load."""
        self._configured()

    def _configured(self) -> list[str]:
        """The hooks, and the roles brought up to date in the database; nothing for a
        store that was not configured (a plain triple store)."""
        if self._provider is None:
            return []
        if self.hooks is None:
            provided = self._provider()
            hooks, self._roles = provided[0], provided[1]
            self.hooks = [
                statement.replace("@TRIPLES@", self.triples) for statement in hooks
            ]
            self.set_property_roles(self._roles)
            if (
                len(provided) > 2
            ):  # the numbers of the types: what the ids of the etags are made of
                self.set_type_ids(provided[2])
        return self.hooks

    def flatten(self) -> None:
        """Flatten the staged rows into `flat` and run the hooks on them: what is then
        inserted is what the predicates made of the batch. A hook is SQL on the table
        `@FLAT@`, which all the records of the batch are in (see `predicates`)."""
        self.con.execute(
            f"CREATE OR REPLACE TABLE {self.flat} AS SELECT * FROM "
            f"flatten_triples({literal(self.staging)})"
        )
        for statement in self._configured():
            self.con.execute(statement.replace("@FLAT@", self.flat))

    def rederive(
        self, rec_type: str, numbers: tuple[int, int] | None = None
    ) -> list[str]:
        """Run the hooks again on records that are stored (after the schema of a type
        was switched, say) and store only what they change: the rows of the records of
        the type (the numbers from `numbers[0]` to `numbers[1]`, all of them without)
        are copied to `flat`, the hooks run on the copy, and the rows that are not there
        any more are deleted and the new ones inserted, in one transaction. A record the
        hooks leave as it is is not touched, so the triggers (the records table, the
        history) never see it. Returns the records that changed."""
        self._configured()
        prefix = literal(f"rec:{rec_type}:")
        where = f"starts_with(s, {prefix})"
        if numbers is not None:
            where += (
                " AND CAST(split_part(s, ':', 3) AS BIGINT) "
                f"BETWEEN {int(numbers[0])} AND {int(numbers[1])}"
            )
        flat, before, gone, new = (
            self.flat,
            f"{self.flat}_before",
            f"{self.flat}_gone",
            f"{self.flat}_new",
        )
        same = " AND ".join(
            f"g.{c} IS NOT DISTINCT FROM t.{c}"
            for c in ("s", "loc", "p", "o", "lit", "lang", "dt")
        )
        self.con.begin()
        try:
            self.con.execute(
                f"CREATE OR REPLACE TABLE {flat} AS SELECT * FROM {self.triples} WHERE "
                f"{where}"
            )
            self.con.execute(
                f"CREATE OR REPLACE TABLE {before} AS SELECT * FROM {flat}"
            )
            for statement in self._configured():
                self.con.execute(statement.replace("@FLAT@", flat))
            self.con.execute(
                f"CREATE OR REPLACE TABLE {gone} AS SELECT * FROM {before} EXCEPT ALL "
                "SELECT * "
                f"FROM {flat}"
            )
            self.con.execute(
                f"CREATE OR REPLACE TABLE {new} AS SELECT * FROM {flat} EXCEPT ALL "
                "SELECT * FROM "
                f"{before}"
            )
            changed = [
                r[0]
                for r in self.con.execute(
                    f"SELECT DISTINCT s FROM (SELECT s FROM {gone} UNION ALL SELECT s "
                    f"FROM {new}) "
                    "ORDER BY s"
                ).fetchall()
            ]
            number = None
            if changed:
                number = (
                    self._take_number()
                )  # a rewrite that changes nothing is no transaction
                self.con.execute(
                    f"DELETE FROM {self.triples} t WHERE EXISTS (SELECT 1 FROM {gone} "
                    "g WHERE "
                    f"{same})"
                )
                self.con.execute(f"INSERT INTO {self.triples} SELECT * FROM {new}")
                self.refresh_etags()
            self.con.commit()
            if number:
                self._committed(number)
        except BaseException:
            self._rollback()
            raise
        finally:
            for table in (flat, before, gone, new):
                self.con.execute(f"DROP TABLE IF EXISTS {table}")
        return changed

    def apply_hooks(self, graph: "Graph | ox.Dataset") -> Graph:
        """The graph as it would be stored, without storing it: the hooks have run on
        it. Nothing is left in the database."""
        graph = graph if isinstance(graph, Graph) else Graph(graph)
        if len(graph) == 0:
            return Graph()
        self._configured()
        self.stage_rows(graph.rows())
        try:
            self.flatten()
            rows = self.con.execute(
                f"SELECT {_ROW_COLUMNS} FROM triples_rows({literal(self.flat)})"
            ).fetchall()
        finally:
            self.drop_staging()
        return Graph.of_rows(rows)

    # ---- load
    # ----------------------------------------------------------------------------

    def check(self) -> list[str]:
        """Warnings about the staged triples: self-loops and bnodes with several
        parents."""
        rows = self.con.execute(
            f"SELECT warning FROM check_triples({literal(self.staging)})"
        )
        return [r[0] for r in rows.fetchall()]

    def non_records(self, limit: int = 5) -> list[str]:
        """Subject uris of the staged triples that are not records (`rec:<type>:<n>`):
        only records are stored."""
        rows = self.con.execute(
            f"""SELECT DISTINCT s FROM {self.staging}
                WHERE s_type = 'uri' AND NOT regexp_matches(s,
                  {literal(uri.RECORD_PATTERN)})
                ORDER BY s LIMIT {int(limit)}"""
        )
        return [r[0] for r in rows.fetchall()]

    def load(self) -> Counter:
        """Load the staged triples in one transaction, then drop the staging table:
        every resource (subject uri) in the staged triples is replaced as a whole, other
        resources are untouched and nothing is ever deleted otherwise. There is no load
        that only adds: a resource that is there is replaced. Returns {"insert": n,
        "delete": n}.

        One file is one transaction, whatever its size: its commit holds up the commits
        of every repository (1.2 s for 300,000 records), and writers of the repository
        wait for it. A load in chunks that go on where they stopped is not built (see
        "Chunked, resumable imports" in plan.md)."""
        counts: Counter = Counter()
        refused = self.non_records()
        if refused:
            self.drop_staging()
            raise ValueError(
                "only records are stored, subjects must be rec:<type>:<n>; not a "
                "record: "
                f"{refused[0]}"
            )
        # the roles are put in the database before the transaction starts
        self._configured()
        try:
            for attempt in range(LOAD_ATTEMPTS):
                self.con.begin()
                try:
                    # flatten and run the hooks first: they touch nothing that another
                    # writer
                    # touches, and the counters are taken as late as can be, so that two
                    # writers
                    # are in each other's way for the writes only
                    self.flatten()
                    number = self._take_number()
                    self.con.execute(self._sql("advance_ids.sql"))
                    # no MERGE (the table has triggers): delete the resources, insert
                    # them
                    counts["delete"] = len(
                        self.con.execute(self._sql("merge.sql")).fetchall()
                    )
                    (counts["insert"],) = fetch_one(
                        self.con.execute(self._sql("insert.sql"))
                    )
                    # the etags are those of the data, in the same transaction
                    self.refresh_etags()
                    self.con.commit()
                    self._committed(number)
                    break
                except duckdb.ConstraintException as err:
                    self._rollback()
                    duplicate = _DUPLICATE.search(str(err))
                    if duplicate:  # a refusal, not a lost race: it is not repeated
                        raise self._duplicate(
                            *[g for g in duplicate.groups() if g is not None]
                        ) from None
                    counts.clear()
                    if attempt == LOAD_ATTEMPTS - 1:
                        raise
                    time.sleep(random.uniform(0.005, 0.05) * (attempt + 1))
                except duckdb.TransactionException:
                    # another writer committed first (a transaction conflict, or through
                    # Quack a
                    # duplicate key of the counters, like `mint_id` below): our
                    # transaction is
                    # void, the staged rows are still here
                    self._rollback()
                    counts.clear()
                    if attempt == LOAD_ATTEMPTS - 1:
                        raise
                    time.sleep(random.uniform(0.005, 0.05) * (attempt + 1))
                except BaseException:
                    self._rollback()
                    raise
        finally:
            self.drop_staging()
        return counts

    # ---- counts
    # --------------------------------------------------------------------------

    def count_records_by_type(self) -> dict[str, int]:
        """How many records there are of each type, by its slug (a deleted record is not
        one): one pass over the `records` table, which is much smaller than the
        triples."""
        rows = self.con.execute(
            f"SELECT rec_type, count(*) FROM {self.records} GROUP BY rec_type"
        )
        return dict(rows.fetchall())

    def type_stats(self) -> dict[str, dict]:
        """By type slug: how many records, how many are published, the size of their
        sources in bytes and when the latest was modified. One pass over the narrow
        columns of `records` (not the sources)."""
        rows = self.con.execute(
            "SELECT rec_type, count(*), count(*) FILTER (WHERE is_published), "
            "sum(bytes), "
            "max(modified_at) "
            f"FROM {self.records} GROUP BY rec_type"
        ).fetchall()
        return {
            slug: {
                "records": n,
                "published": published,
                "bytes": int(size or 0),
                "modified": modified,
            }
            for slug, n, published, size, modified in rows
        }

    def etag_stats(self) -> dict:
        """How many records have an etag, the size of the sources of all their pages
        together (`etags.closure_bytes`: shared records are counted for each page) and
        when the latest page changed."""
        pages, size, modified = fetch_one(
            self.con.execute(
                "SELECT count(*), coalesce(sum(closure_bytes), 0), "
                "max(last_modified_at) FROM etags"
            )
        )
        return {"pages": pages, "bytes": int(size), "modified": modified}

    def pending_summary(self) -> dict:
        """What waits in the queue of what follows the data (see `gracia.actions`): the
        rows and the records they are about, the sources of what those are made of in
        bytes, the rows by action, the oldest transaction and when it was, and the last
        transaction there is."""
        rows, oldest = self.pending_actions()
        records, size = self.pending_size()
        actions = dict(
            self.con.execute(
                "SELECT action, count(*) FROM actions_queue GROUP BY action"
            ).fetchall()
        )
        since = None
        if oldest is not None:
            (since,) = fetch_one(
                self.con.execute(
                    "SELECT min(modified_at) FROM history WHERE txn_id = ?", [oldest]
                )
            )
        return {
            "rows": rows,
            "records": records,
            "bytes": size,
            "actions": actions,
            "oldest": oldest,
            "since": since,
            "last": self.last_transaction(),
        }

    def count_by_predicate(self) -> dict[str, int]:
        """How many rows (values) there are of each predicate, in one scan of the
        table."""
        rows = self.con.execute(f"SELECT p, count(*) FROM {self.triples} GROUP BY p")
        return dict(rows.fetchall())

    # ---- prefixes
    # ------------------------------------------------------------------------

    def used_prefixes(self, prefixes: dict[str, str]) -> dict[str, str]:
        """Those of `prefixes` ({prefix: namespace}, the ns_prefix extension has them)
        whose namespace starts at least one uri that is stored: what an output needs to
        declare."""
        if not prefixes:
            return {}
        values = ", ".join(f"({literal(p)}, {literal(n)})" for p, n in prefixes.items())
        rows = self.con.execute(
            f"""SELECT p.prefix, p.namespace FROM (VALUES {values}) AS p(prefix,
              namespace)
                WHERE EXISTS (
                    SELECT 1 FROM (
                        SELECT p AS v FROM {self.triples} UNION SELECT dt FROM
                          {self.triples}
                        UNION SELECT o FROM {self.triples} UNION SELECT s FROM
                          {self.triples}
                        UNION SELECT unnest(loc) FROM {self.triples}
                    ) u WHERE starts_with(u.v, p.namespace))
                ORDER BY p.prefix""",
        )
        return dict(rows.fetchall())

    # ---- read
    # ----------------------------------------------------------------------------

    def records_of_type(self, rec_type: str) -> list[str]:
        """The uris of the records of a type (its slug), in the order of their numbers;
        a deleted record is not one."""
        rows = self.con.execute(
            f"SELECT record_uri(record_id) FROM {self.records} WHERE rec_type = "
            f"{literal(rec_type)} "
            "ORDER BY record_id"
        ).fetchall()
        return [r[0] for r in rows]

    def records_with_resource_uris(self, resource_uris: list[str]) -> dict[str, str]:
        """The records whose resource is published under these uris (ARKs, class iris of
        the types): {resource uri: record uri}, only for the ones that have a record."""
        if not resource_uris:
            return {}
        rows = self.con.execute(
            f"SELECT res_uri, record_uri(record_id) FROM {self.records} "
            f"WHERE res_uri IN (SELECT unnest({sql_list(sorted(set(resource_uris)))}))"
        ).fetchall()
        return dict(rows)

    def record_with_resource_uri(self, resource_uri: str) -> str | None:
        """The record whose resource is published under this uri (an ARK, an ORCID), or
        None."""
        row = self.con.execute(
            f"SELECT record_uri(record_id) FROM {self.records} WHERE res_uri = "
            f"{literal(resource_uri)} "
            "ORDER BY record_id LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def _ensure_ids(self) -> None:
        """The counters table, made once per store (a repository made before it existed
        has none, and a store opened without `init` does not make it)."""
        if not self._ids_ready:
            self.con.execute(self._sql("ids.sql"))
            self._ids_ready = True

    def _highest_record(self, res_type: str) -> str:
        """SQL: the highest number a record of the type has now (0: none)."""
        return (
            f"coalesce((SELECT max(record_id % 10000000) FROM {self.records} "
            f"WHERE rec_type = {literal(res_type)}), 0)"
        )

    def _highest_used(self, res_type: str) -> str:
        """SQL: the highest number a record of the type has or had: `records` and the
        history, which has a deleted record too, so a number that was used is not given
        out again, also when what used it was written outside the store, which the
        counters never hear of."""
        number = self.type_numbers().get(res_type)  # the ids of a type are one range
        first = "NULL" if number is None else number * uri.TYPE_BASE
        return (
            f"greatest({self._highest_record(res_type)}, coalesce((SELECT max("
            f"record_id % 10000000) FROM history "
            f"WHERE record_id BETWEEN {first} AND {first} + 9999999), 0))"
        )

    def last_id(self, res_type: str) -> int:
        """The highest number minted or used for a type of record (its slug): 0 when
        there is none. The counter knows the numbers that were given out and not used
        yet, the records table the ones that were."""
        self._ensure_ids()
        row = self.con.execute(
            "SELECT greatest(coalesce((SELECT greatest(last, reserved) FROM "
            f"{self.ids} "
            f"WHERE type = {literal(res_type)}), 0), {self._highest_used(res_type)})"
        ).fetchone()
        return row[0]  # ty: ignore[not-subscriptable]

    def highest_number(self, res_type: str) -> int:
        """The highest number a record of the type has now (0: none): not one that was
        deleted, not what was given out or reserved (`last_id` is that, and the numbers
        that were used)."""
        return fetch_one(self.con.execute(f"SELECT {self._highest_record(res_type)}"))[
            0
        ]

    def numbering(self, res_type: str) -> dict[str, int]:
        """What decides the next number of a type: `given` (the last one a form or an
        import was given), `reserved` (the floor a person set), `highest` (the highest
        number a record has or had, one that was deleted too), `taken` (the greatest of
        the three: a number cannot be lowered past it) and `next`."""
        self._ensure_ids()
        row = self.con.execute(
            f"SELECT last, reserved FROM {self.ids} WHERE type = {literal(res_type)}"
        ).fetchone()
        given, reserved = row if row else (0, 0)
        highest = fetch_one(self.con.execute(f"SELECT {self._highest_used(res_type)}"))[
            0
        ]
        taken = max(given, highest)
        return {
            "given": given,
            "reserved": reserved,
            "highest": highest,
            "taken": taken,
            "last": max(taken, reserved),
            "next": max(taken, reserved) + 1,
        }

    def reserve_ids(self, res_type: str, number: int) -> int:
        """Keep the numbers up to `number` for what is going to be imported: the next
        number minted is past it. It cannot be lower than a number that is in use (a
        record has it, or a form was given it), which is a `ValueError`; lowering a
        reservation to that is how it is given up. Returns the new floor."""
        if not TYPE_SLUG.match(res_type):
            raise ValueError(f"invalid type slug: {res_type!r}")
        self._ensure_ids()
        self.con.begin()
        try:
            taken = self.numbering(res_type)["taken"]
            if number < taken:
                raise ValueError(
                    f"the number cannot be lower than {taken}, which is in use"
                )
            self.con.execute(
                f"INSERT INTO {self.ids} (type, last, reserved) VALUES "
                f"({literal(res_type)}, 0, "
                f"{int(number)}) "
                "ON CONFLICT (type) DO UPDATE SET reserved = excluded.reserved"
            )
            self.con.commit()
        except BaseException:
            self._rollback()
            raise
        return number

    def mint_id(self, res_type: str) -> int:
        """The next number for records of this type (1, 2, 3, ...), never given out
        twice. Each type counts for itself; `type` is the type's slug, as in
        `rec:<type>:<n>`."""
        if not TYPE_SLUG.match(res_type):
            raise ValueError(f"invalid type slug: {res_type!r}")
        self._ensure_ids()
        # the counter keeps the numbers that were given out (a form takes its number
        # before the
        # record is saved), `records` the ones that were used, whoever wrote them: the
        # next is
        # past both, and the counter row serialises two who take one at once
        highest = self._highest_used(res_type)
        # (a DO UPDATE clause cannot hold a subquery: the highest is in the new row)
        sql = (
            f"INSERT INTO {self.ids} (type, last) SELECT {literal(res_type)}, "
            f"{highest} + 1 "
            f"ON CONFLICT (type) DO UPDATE SET last = "
            f"greatest({self.ids}.last, {self.ids}.reserved, excluded.last - 1) + 1 "
            "RETURNING last"
        )
        for attempt in range(LOAD_ATTEMPTS):
            try:
                return fetch_one(self.con.execute(sql))[0]
            except (duckdb.TransactionException, duckdb.ConstraintException):
                # another writer minted at the same moment (for the first number of a
                # type they
                # race to insert its row, and the loser sees a duplicate key)
                if attempt == LOAD_ATTEMPTS - 1:
                    raise
                time.sleep(random.uniform(0.005, 0.05) * (attempt + 1))
        raise AssertionError("unreachable")

    def export_json(self, uris=None) -> str:
        """json-triples text for the whole table, or only for the given resource(s)."""
        row = self.con.execute(
            f"SELECT json FROM export_triples({literal(self.triples)}, uris := "
            f"{_uris_arg(uris)})"
        ).fetchone()
        return row[0]  # ty: ignore[not-subscriptable]

    def _records_where(
        self,
        uris=None,
        rec_type: str | None = None,
        numbers: tuple[int, int] | None = None,
    ) -> str:
        """The WHERE of a read of the live records: some uris, and/or the records of a
        type, and/or of that type the numbers from `numbers[0]` to `numbers[1]`."""
        where = []
        values = uri_list(uris)
        if values is not None:
            where.append(f"record_id IN {sql_ids(values)}")
        if rec_type is not None:
            where.append(f"rec_type = {literal(rec_type)}")
        if numbers is not None:
            where.append(
                f"record_id % 10000000 BETWEEN {int(numbers[0])} AND {int(numbers[1])}"
            )
        return " AND ".join(where) or "TRUE"

    def _copy_to(self, select: str, path: str | Path, options: str) -> int:
        """`COPY` a query to a file, written by DuckDB itself: under a temporary name,
        renamed when the copy is complete, so that whoever waits for `path` never reads
        half of it. Returns the number of rows written. (Over Quack the owner writes the
        file: it is on the same machine.)"""
        path = Path(path).absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.writing")
        temporary.unlink(missing_ok=True)
        try:
            (count,) = fetch_one(
                self.con.execute(
                    f"COPY ({select}) TO {literal(str(temporary))} ({options})"
                )
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return count

    def export_ntriples(
        self, path: str | Path, uris=None, rec_type=None, numbers=None
    ) -> int:
        """Write the `source` of the live records to `path` as N-Triples and return how
        many records: one record after the other, a blank line between them, in the
        order of (type, number, uri). DuckDB writes it (`COPY` as CSV with the quote and
        escape characters off, so the text goes out as it is), Python does no work. See
        `_records_where` for the arguments."""
        return self._copy_to(
            f"SELECT source FROM {self.records} WHERE "
            f"{self._records_where(uris, rec_type, numbers)} "
            "ORDER BY rec_type, record_id",
            path,
            "FORMAT csv, HEADER false, QUOTE '', ESCAPE ''",
        )

    def export_chunks(
        self,
        directory: str | Path,
        rec_type: str | None,
        chunk_bytes: int = 0,
        *,
        graphs: bool = False,
    ) -> tuple[int, list[Path]]:
        """Write the live records of a type (None: of every type) as files in
        `directory` (which has to be empty or not exist): `chunk-0.nt`, `chunk-1.nt`,
        ... of about `chunk_bytes` each, or one file, `chunk-0.nt`, when it is 0. A
        record is never split over two files. One `COPY` does all of it, writing the
        files at the same time, so none of them is complete before this returns. The
        files are made under another name and moved into place at the end. Returns (the
        number of records, the files in order of their number).

        With `graphs` the files are TriG (`chunk-0.trig`) and a record is its
        `record_graph()`: the graph named by the record, with the record and everything
        it is made of, after a comment line with its etag (`# 0123456789abcdef`). Only
        the records that have an etag are there.

        DuckDB looks at the size after every 2048 rows, so a file can be over
        `chunk_bytes` by that many records, and fewer records than that always make one
        file."""
        directory = Path(directory).absolute()
        extension = "trig" if graphs else "nt"
        if graphs:
            where = "TRUE" if rec_type is None else f"record_type = {literal(rec_type)}"
            select = (
                "SELECT '# ' || printf('%016x', etag) || chr(10) || graph "
                f"FROM record_graph() WHERE {where} ORDER BY record_id"
            )
        else:
            select = (
                f"SELECT source FROM {self.records} WHERE "
                f"{self._records_where(None, rec_type)} "
                "ORDER BY record_id"
            )
        if chunk_bytes <= 0:
            target = directory / f"chunk-0.{extension}"
            if graphs:
                count = self._copy_to(select, target, self._PLAIN)
            else:
                count = self.export_ntriples(target, rec_type=rec_type)
            return count, [target] if count else []
        return self._copy_chunks(select, directory, chunk_bytes, extension)

    _PLAIN = "FORMAT csv, HEADER false, QUOTE '', ESCAPE ''"

    def _copy_chunks(
        self, select: str, directory: Path, chunk_bytes: int, extension: str
    ) -> tuple[int, list[Path]]:
        """`COPY` a query to files of about `chunk_bytes` in `directory` (one `COPY`,
        written by DuckDB at once, the files moved into place at the end):
        `chunk-<n>.<extension>`. Returns (the number of rows, the files in order)."""
        working = directory.with_name(f"{directory.name}.writing")
        shutil.rmtree(working, ignore_errors=True)
        try:
            options = (
                f"{self._PLAIN}, FILE_SIZE_BYTES {int(chunk_bytes)}, FILENAME_PATTERN "
                f"'chunk-{{i}}', "
                f"FILE_EXTENSION '{extension}', RETURN_FILES true"
            )
            count, written = fetch_one(
                self.con.execute(
                    f"COPY ({select}) TO {literal(str(working))} ({options})"
                )
            )
            directory.mkdir(parents=True, exist_ok=True)
            numbered = sorted(
                (int(re.search(rf"chunk-(\d+)\.{extension}$", name)[1]), name)  # ty: ignore[not-subscriptable]
                for name in written or []
            )
            files = []
            if (
                count == 0
            ):  # DuckDB writes an empty file for an empty result: no file, no batch
                numbered = []
            for number, name in numbered:
                target = directory / f"chunk-{number}.{extension}"
                os.replace(name, target)
                files.append(target)
        finally:
            shutil.rmtree(working, ignore_errors=True)
        return count, files

    def export_queue(
        self, directory: str | Path, chunk_bytes: int
    ) -> tuple[int, list[Path], int]:
        """Write what the queue holds (`actions_queue`) as TriG files in `directory`,
        the way `export_chunks` with `graphs` does, one graph for each record that is
        waiting: the record and everything it is made of, after a comment line `# <etag>
        <action>`. The action is the newest the queue has for the record; a record that
        was deleted, or has no etag, is `# delete <action>` and a graph with nothing in
        it. Only the rows up to the last transaction as it is now are written. Returns
        (the number of records, the files, that transaction): what `actions_done` needs
        to take these rows off when the files have been handled."""
        directory = Path(directory).absolute()
        # what is read is the etags: a write outside the store has none yet
        self.catch_up_etags()
        bound = self.last_transaction()
        select = (
            f"WITH q AS (SELECT record_id, arg_max(action, transaction_id) AS action, "
            "max(transaction_id) AS txn FROM actions_queue WHERE transaction_id <= "
            f"{bound} GROUP "
            "BY record_id) "
            "SELECT '# ' || CASE WHEN g.record_id IS NULL OR q.action = 'delete' THEN "
            "'delete' "
            "ELSE printf('%016x', g.etag) END || ' ' || q.action || chr(10) || "
            "CASE WHEN g.record_id IS NULL OR q.action = 'delete' "
            "THEN encode_ntriples_uri(record_uri(q.record_id)) || ' "
            "{' || chr(10) || '}' ELSE "
            "g.graph END "
            "FROM q LEFT JOIN (SELECT * FROM record_graph() WHERE record_id IN (SELECT "
            "record_id "
            "FROM q)) g "
            "ON g.record_id = q.record_id ORDER BY q.txn, q.record_id"
        )
        count, files = self._copy_chunks(select, directory, chunk_bytes, "trig")
        return count, files, bound

    # ---- datasets
    # -------------------------------------------------------------------------

    def get(self, uris) -> Graph:
        """The records with these uris (one uri, or several) as a `Graph`: each with its
        embedded blank nodes and rdf lists, all in the default graph. They are read from
        the `source` column of the `records` table (one N-Triples text per record) and
        parsed together. A uri that is not a record, or whose record is deleted, is
        simply absent from the result."""
        values = uri_list(uris)
        if not values:
            return Graph()
        rows = self.con.execute(
            f"SELECT source FROM {self.records} WHERE record_id IN {sql_ids(values)}"
        ).fetchall()
        source = "\n".join(row[0] for row in rows)
        return Graph(
            ox.Dataset(ox.parse(source.encode("utf-8"), format=ox.RdfFormat.N_TRIPLES))
        )

    def put(self, graph: "Graph | ox.Dataset", strict: bool = False) -> Counter:
        """Store the resources of a graph (or a pyoxigraph Dataset), replacing each one
        as a whole (nothing else is touched, nothing is ever deleted otherwise). A
        resource is a uri subject with the blank nodes below it; see gracia.graph. Quads
        that belong to no resource (a blank node that no uri reaches) are an error, they
        are not dropped silently. With `strict`, warnings (self-loops, shared blank
        nodes) are errors too. Returns {"insert": n, "delete": n}."""
        dataset = graph if isinstance(graph, Graph) else Graph(graph)
        if len(dataset) == 0:
            return Counter()
        orphans = dataset.orphaned_nodes()
        if len(orphans):
            raise ValueError(
                f"{len(orphans)} quad(s) belong to no resource (a blank node that no "
                "uri "
                f"subject reaches), e.g. {next(iter(orphans))}"
            )
        self.stage_rows(dataset.rows())
        try:
            warnings = self.check()
            if strict and warnings:
                raise ValueError("; ".join(warnings))
            return self.load()
        finally:
            self.drop_staging()

    # ---- the records table
    # ----------------------------------------------------------------

    def _duplicate(self, rec_type: str, predicate: str, value: str) -> DuplicateValue:
        row = self.con.execute(
            "SELECT s FROM unique_values "
            f"WHERE rec_type = {literal(rec_type)} AND p = {literal(predicate)} "
            f"AND value = {literal(value)}"
        ).fetchone()
        return DuplicateValue(rec_type, predicate, value, row[0] if row else None)

    def set_property_roles(self, roles: Iterable[tuple[str, str, str, int]]) -> bool:
        """Tell the database what the shapes say about the properties of the types: rows
        of (type slug, role, property uri, sh:order), the roles being `label`,
        `key_info`, `description` and `unique`. Returns whether anything changed. When
        something did, the records are made again from the triples (their `identifiers`
        and `text` follow the roles) and the unique values are collected again: a value
        that more than one record of a type has, where the property is now unique, is a
        `DuplicateValue` and nothing changes."""
        wanted = sorted(set(roles))
        current = self.con.execute(
            "SELECT rec_type, role, p, ord FROM property_roles ORDER BY ALL"
        ).fetchall()
        if [tuple(r) for r in current] == wanted:
            return False
        values = ", ".join(
            f"({literal(t)}, {literal(r)}, {literal(p)}, {int(o)})"
            for t, r, p, o in wanted
        )
        self.con.begin()
        try:
            self.con.execute("DELETE FROM property_roles")
            if wanted:
                self.con.execute(f"INSERT INTO property_roles VALUES {values}")
            self.con.execute("DELETE FROM unique_values")
            self.con.execute(
                "INSERT INTO unique_values SELECT DISTINCT r.rec_type, t.p, "
                "coalesce(t.o, t.lit), "
                "t.s "
                f"FROM {self.triples} t JOIN property_roles r ON r.role = 'unique' AND "
                "r.p = t.p "
                "AND r.rec_type = regexp_extract(t.s, '^rec:([a-z][a-z0-9-]*):', 1)"
            )
            self._rebuild_records()
            self.con.commit()
        except duckdb.ConstraintException as err:
            self._rollback()
            duplicate = _DUPLICATE.search(str(err))
            if duplicate:
                raise self._duplicate(
                    *[g for g in duplicate.groups() if g is not None]
                ) from None
            raise
        except BaseException:
            self._rollback()
            raise
        return True

    def _rebuild_records(self) -> None:
        """All the records and their list tables again, from the triples (in the
        transaction of the caller). The triggers are not involved: this is the same
        statement they run."""
        self.con.execute(f"DELETE FROM {self.records}_identifiers")
        self.con.execute(f"DELETE FROM {self.records}_references")
        self.con.execute(f"DELETE FROM {self.records}")
        self.con.execute(
            f"INSERT INTO {self.records} SELECT * FROM "
            f"record_rows({literal(self.triples)}, "
            f"(SELECT list(DISTINCT s) FROM {self.triples}))"
        )
        self.con.execute(
            f"INSERT INTO {self.records}_identifiers SELECT DISTINCT "
            "unnest(identifiers), "
            "record_id "
            f"FROM {self.records}"
        )
        self.con.execute(
            f"INSERT INTO {self.records}_references SELECT DISTINCT "
            'unnest("references"), '
            "record_id "
            f"FROM {self.records}"
        )

    def records_with(
        self, kind: str, value: str, rec_type: str | None = None
    ) -> list[str]:
        """The records that have `value` among their `identifiers` or their `references`
        (`kind`), by the list index, sorted; only those of a type with `rec_type` (its
        slug)."""
        if kind not in ("identifiers", "references"):
            raise ValueError("kind is 'identifiers' or 'references'")
        column = kind[:-1] if kind == "references" else "identifier"
        number = self.type_numbers().get(rec_type) if rec_type else None
        first = "NULL" if number is None else number * uri.TYPE_BASE
        only = (
            f" AND record_id BETWEEN {first} AND {first} + 9999999" if rec_type else ""
        )
        rows = self.con.execute(
            f"""SELECT record_uri(record_id) FROM (SELECT DISTINCT record_id FROM
              {self.records}_{kind}
                WHERE {column} = {literal(value)}{only}) ORDER BY record_id"""
        )
        return [r[0] for r in rows.fetchall()]

    def sort(self) -> int:
        """Rewrite the triples table in the order of (s, loc, p), in one transaction,
        and return the number of rows. The rows of one type (`rec:person:`) then lie
        together in a few row groups, so a query for a type (an export) reads those and
        skips the rest. Loading and saving append to the end, so run this now and
        then."""
        order = "s, loc, p"
        copy = f"{self.triples}_sorting"
        self.con.begin()
        try:
            self.con.execute(
                f"CREATE TEMP TABLE {copy} AS SELECT * FROM {self.triples} ORDER BY "
                f"{order}"
            )
            # the triggers would rewrite every record twice: the rows come back
            # unchanged
            self._drop_record_triggers()
            self.con.execute(f"DELETE FROM {self.triples}")
            self.con.execute(
                f"INSERT INTO {self.triples} SELECT * FROM {copy} ORDER BY {order}"
            )
            self.con.execute(self._sql("records.sql") + self._record_triggers())
            rows = fetch_one(self.con.execute(f"SELECT count(*) FROM {self.triples}"))[
                0
            ]
            self.con.execute(f"DROP TABLE {copy}")
            self.con.commit()
        except BaseException:
            self._rollback()
            raise
        return rows

    def delete(self, uris: list[str]) -> dict[str, int]:
        """Delete resources (all rows of each subject uri, bnodes included): {uri:
        rows}."""
        subs = {"@URIS@": sql_list(uris)}
        self.con.begin()
        try:
            number = self._take_number()
            counted = dict(
                self.con.execute(self._sql("delete_count.sql", **subs)).fetchall()
            )
            self.con.execute(self._sql("delete_resource.sql", **subs))
            self.refresh_etags()
            self.con.commit()
            self._committed(number)
        except BaseException:
            self._rollback()
            raise
        return counted


# The database as its own process: DuckDB lets one process hold a file read-write.
# `serve_db` is
# that owner: it opens the file and runs `quack_serve`, so other processes (the admin
# CLI, web
# workers) attach over localhost with a token (see RemoteConnection). The token is a
# master key,
# kept in an owner-only file (0600), created on first start; only loopback addresses are
# served.

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
DEFAULT_PORT = 9494


def owner_file(db: str | Path) -> Path:
    """`<db>.owner`: written while `serve-db` runs, two lines: its address and its token
    file. It is a hint for clients, who check it by connecting; a crash can leave it
    behind."""
    return Path(f"{db}.owner")


def read_owner(db: str | Path) -> tuple[str, str] | None:
    """(address, token file) from the owner file of `db`, or None when there is none."""
    try:
        uri, token_file = owner_file(db).read_text(encoding="utf-8").splitlines()[:2]
    except (OSError, ValueError):
        return None
    return uri, token_file


def read_or_create_token(path: Path) -> str:
    """The token in `path`; a new random one is written there (mode 0600) when the file
    is missing. A file that group or others can read is refused."""
    if path.exists():
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError(
                f"{path} is readable by group or others: run `chmod 600 {path}`"
            )
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise ValueError(
                f"the token in {path} is too short (at least 16 characters)"
            )
        return token
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def serve_db(
    db: str | Path,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    token_file: str | Path | None = None,
    stop: threading.Event | None = None,
    ready=None,
) -> None:
    """Open `db` read-write and serve it over Quack until `stop` is set (or forever).

    `ready(uri, token_file)` is called once the server is listening. On the way out the
    server is stopped, the database checkpointed and the file closed, so it can be
    opened again at once."""
    if str(db) == ":memory:":
        raise ValueError("an in-memory database cannot be served: give a file")
    if host not in LOCAL_HOSTS:
        raise ValueError(
            f"only {', '.join(sorted(LOCAL_HOSTS))} can be served for now, not {host!r}"
        )
    token_path = Path(token_file) if token_file else Path(f"{db}.token")
    token = read_or_create_token(token_path)
    uri = f"quack:{host}:{port}"
    owner_path = owner_file(db)
    con = duckdb.connect(str(db))
    try:
        con.execute(f"CALL quack_serve({literal(uri)}, token = {literal(token)})")
        # where this owner listens and which token file it uses, for clients in the same
        # directory
        owner_path.write_text(f"{uri}\n{token_path.resolve()}\n", encoding="utf-8")
        if ready:
            ready(uri, token_path)
        (stop or threading.Event()).wait()
    finally:
        owner_path.unlink(missing_ok=True)
        with contextlib.suppress(duckdb.Error):
            con.execute(f"CALL quack_stop({literal(uri)})")
        con.execute("CHECKPOINT")
        con.close()
