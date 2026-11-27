import contextlib
import json
import os
import random
import re
import shutil
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path

import duckdb
import pyoxigraph as ox

from gracia import uri
from gracia.database import RemoteConnection, literal
from gracia.graph import Graph, dataset_rdfxml

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


_TYPE_NUMBER = re.compile(r"\(n_ = (\d+)\)\) THEN \('((?:[^']|'')*)'\)")


def _type_macros(types: dict[str, int], replace: bool) -> str:
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
    row = cursor.fetchone()
    if row is None:
        raise LookupError("the query gave no row")
    return row


def sql_list(values: list[str]) -> str:
    return "[" + ", ".join(literal(v) for v in values) + "]"


def sql_ids(uris: list[str]) -> str:
    return (
        "(SELECT record_id(u) FROM "
        f"(SELECT unnest({sql_list(sorted(set(uris)))}) AS u) "
        "WHERE starts_with(u, 'rec:'))"
    )


def uri_list(
    uris: "str | ox.NamedNode | Iterable[str | ox.NamedNode] | None",
) -> list[str] | None:
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
    def __init__(self, repo: str):
        super().__init__(
            f"no such repository: {repo} (create it with `gracia repo create {repo}`)"
        )
        self.repo = repo


def validate_repository_id(repo: str) -> str:
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
# the order `etags` is kept in: what `Store.referencing_records` reads, newest created first,
# with no ORDER BY
ETAGS_ORDER = "ORDER BY record_type, sort_key DESC, record_id"
PAGE_SIZE = 20  # records on a page of a listing; the last page has up to twice as many
EMPTY_TOTALS = (
    "CAST(MAP {} AS MAP(VARCHAR, STRUCT(total_all BIGINT, total_published BIGINT)))"
)


def page_window(total: int, page: int | None, size: int) -> tuple[int, int, int, int]:
    pages = max(1, -(-total // size))
    page = 1 if page is None else page
    if not 1 <= page <= pages:
        raise ValueError(f"there is no page {page}: the pages are 1 to {pages}")
    offset = (page - 1) * size
    return page, pages, offset, min(size, total - offset)


class Store:
    def __init__(
        self,
        connection,
        triples: str = "triples",
        repo: str = DEFAULT_REPOSITORY,
        *,
        create: bool = False,
        init: bool = True,
        close_connection: bool = False,
        cache=None,
    ):
        # Over Quack the macros run in the owner process and cannot see a temp table of
        # ours: the
        # staging table is then a regular table (one per store, so writers do not clash)
        # that is
        # filled by pushing a local temp table with INSERT ... SELECT (appenders do not
        # work there).
        self.remote = isinstance(connection, RemoteConnection)
        self.staging = (
            f"gracia_staging_{uuid.uuid4().hex[:12]}"
            if self.remote
            else "gracia_staging"
        )
        self.flat = (
            f"{self.staging}_flat"  # the staged rows flattened, what the hooks work on
        )
        # SQL run on `flat`, each with the slug of the type it is for: see `configure`
        self.hooks: list[tuple[str, str]] | None = None
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
        self._close_connection = close_connection
        self.cache = cache  # the caches of the process (`gracia.cache.Cache`), if any
        self.repository = None  # the repository that made the store (`get_doc`)
        self.con = connection
        try:
            # the local view of the database decides the catalog name and whether the
            # repository exists; over Quack the rest of the SQL runs on the owner (see
            # `RemoteConnection`)
            local = connection.local if self.remote else connection
            self._catalog = fetch_one(local.execute("SELECT current_database()"))[0]
            exists = _repository_exists(local, self._catalog, repo)
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
            "INSERT INTO slugs (rec_type, slug, record_uri, is_deleted) SELECT DISTINCT regexp_extract(t.s, "
            "'^rec:([a-z][a-z0-9-]*):', 1), "
            "t.lit, t.s, false FROM {table} t "
            "WHERE t.s IN ({subs}) AND len(t.loc) = 0 AND t.p = {slug_uri} AND t.lit "
            "IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM slugs g WHERE g.record_uri = t.s AND g.slug "
            "= t.lit)",
            "UPDATE slugs SET is_deleted = false, modified_at = now() AT TIME ZONE 'UTC' "
            "WHERE is_deleted "
            f"AND record_uri IN (SELECT record_uri(record_id) FROM {{rec}} WHERE "
            "record_id IN "
            f"({changed}))",
            "UPDATE slugs SET is_deleted = true, modified_at = now() AT TIME ZONE 'UTC' "
            "WHERE NOT is_deleted AND record_uri IN "
            "{gone}",
            "INSERT INTO slugs (rec_type, slug, record_uri, is_deleted) SELECT regexp_extract(s, '^rec:([a-z][a-z0-9-]*):', 1), "
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
            f"DELETE FROM change_events WHERE transaction_id = {number} AND record_id "
            "IN "
            f"({changed})",
            f"INSERT INTO change_events SELECT {number}, c.record_id, "
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
        if self.remote:  # the owner keeps a current schema per connection, and so do we
            con.execute(f'USE "{self.repo}"')
            con = con.local
            con.execute(
                "CALL quack_clear_cache()"
            )  # the local view may predate the schema
        con.execute(f'USE "{self._catalog}"."{self.repo}"')

    def fork(self) -> "Store":
        return Store(
            self.con.cursor(),
            self.triples,
            self.repo,
            init=False,
            close_connection=True,
        )

    def close(self) -> None:
        if self._close_connection:
            self.con.close()

    def counts(self) -> dict[str, int]:

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
        self._new_staging()
        # remote: fill a local temp table, push it afterwards
        target = "gracia_push" if self.remote else self.staging
        if self.remote:
            self.con.local.execute(
                f"CREATE OR REPLACE TEMP TABLE {target} ({_STAGING_COLUMNS})"
            )
        appender = (self.con.local if self.remote else self.con).appender(target)
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
            self.con.local.execute(
                "CALL quack_clear_cache()"
            )  # the staging table is new
            self.con.local.execute(
                f"INSERT INTO {self.staging} SELECT * FROM temp.main.{target}"
            )
            self.con.local.execute(f"DROP TABLE temp.main.{target}")
        return count

    def stage_json(self, json_file: str | Path) -> int:
        self._new_staging()
        self.con.execute(
            f"INSERT INTO {self.staging} SELECT {_ROW_COLUMNS} FROM "
            f"parse_triples({literal(str(json_file))})"
        )
        return fetch_one(self.con.execute(f"SELECT count(*) FROM {self.staging}"))[0]

    def _rollback(self) -> None:
        with contextlib.suppress(duckdb.TransactionException):
            self.con.rollback()
        self._forget_number()

    # ---- the transactions of the history (history.py)
    # ---------------------------------------

    def _take_number(self) -> int:
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
        with contextlib.suppress(duckdb.Error):
            self.con.execute("SET VARIABLE txn_id = NULL")

    def _committed(self, number: int) -> None:
        self._forget_number()

    def last_transaction(self) -> int:
        self._ensure_ids()
        row = self.con.execute(
            f"SELECT last FROM {self.ids} WHERE type = '_transaction'"
        ).fetchone()
        return row[0] if row else 0

    # ---- the etags of the pages (etags.sql)
    # ----------------------------------------------------

    def type_numbers(self) -> dict[str, int]:
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
        self.con.execute(
            f"""INSERT INTO change_events
                SELECT {int(transaction)}, id, 'ref_update' FROM (
                    SELECT record_id AS id FROM etags_todo
                    UNION SELECT record_id FROM etags_holders WHERE delta <> 0 AND NOT
                      bad)
                WHERE id NOT IN (SELECT record_id FROM change_events WHERE
                  transaction_id > {int(marker)})"""
        )

    def _sort_etags(self) -> None:
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
        self._backref_changes()
        self.con.execute(
            "DELETE FROM etags WHERE record_id IN (SELECT record_id FROM etags_todo)"
        )
        self.con.execute("INSERT INTO etags SELECT * FROM etags_new")
        self._backref_totals()

    def _backref_changes(self) -> None:
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
                f"""INSERT INTO change_events
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
                       bytes, closure_bytes, state, old_etag) AS (
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
                       r0.bytes, sum(r.bytes), r0.state,
                       any_value(e0.etag) AS old_etag
                FROM clo c
                JOIN {self.records} r ON r.record_id = c.dep
                JOIN {self.records} r0 ON r0.record_id = c.root
                LEFT JOIN etags_refs f0 ON f0.record_id = c.root
                LEFT JOIN etags e0 ON e0.record_id = c.root
                GROUP BY c.root, r0.rec_type, r0.res_uri, r0.created_at, r0.modified_at,
                  r0.is_published, r0.bytes, r0.state)
                SELECT * EXCLUDE (old_etag) REPLACE (
                    CASE WHEN old_etag IS NOT NULL AND old_etag <> etag
                         THEN greatest(last_modified_at, now() AT TIME ZONE 'UTC')
                         ELSE last_modified_at END AS last_modified_at)
                FROM walked"""
        )

    def record_id_of(self, rec_uri: str) -> int | None:
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
        record_id = self.record_id_of(rec_uri)
        if record_id is None:
            return None
        row = self.con.execute(
            f"SELECT etag, deep_types FROM etags WHERE record_id = {record_id}"
        ).fetchone()
        return (f"{row[0]:016x}", list(row[1])) if row else None

    def fetch_compound_graphs(self, record_ids) -> dict[int, tuple[str, str]]:
        """The compound graph of each record that has an etag: the N-Triples of the
        record and of every record it is made of, with the etag they are of
        (`{record id: (etag, text)}`). What the graph cache keeps, made from the
        database."""
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

    # ---- the queue of what the followers have to do (records.sql, gracia.change_events)
    # ---------------

    def take_change_events(
        self, limit: int = 100, max_bytes: int | None = None
    ) -> tuple[list[tuple[int, str, str]], int]:
        self.con.begin()
        try:
            bound = self.last_transaction()
            oldest = self.con.execute(
                f"SELECT q.record_id, coalesce(e.closure_bytes, 0) FROM "
                f"(SELECT record_id FROM change_events WHERE transaction_id <= {bound} "
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
                    "SELECT record_id, arg_max(kind, transaction_id) FROM "
                    "change_events "
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

    def change_events_done(self, record_ids, bound: int) -> None:
        listed = ", ".join(str(int(i)) for i in dict.fromkeys(record_ids))
        if listed:
            self.con.execute(
                f"DELETE FROM change_events WHERE record_id IN ({listed}) AND "
                "transaction_id <= "
                f"{int(bound)}"
            )

    def requeue_changes_since(self, bound: int) -> int:
        (since,) = fetch_one(
            self.con.execute(
                "SELECT max(modified_at) FROM history WHERE txn_id = ?", [int(bound)]
            )
        )
        transaction = self.last_transaction()
        before = fetch_one(self.con.execute("SELECT count(*) FROM change_events"))[0]
        self.con.execute(
            f"""INSERT INTO change_events
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
            fetch_one(self.con.execute("SELECT count(*) FROM change_events"))[0]
            - before
        )

    def pending_size(self) -> tuple[int, int]:
        records, size = fetch_one(
            self.con.execute(
                "SELECT count(*), coalesce(sum(e.closure_bytes), 0) FROM "
                "(SELECT DISTINCT record_id FROM change_events) q LEFT JOIN etags e ON "
                "e.record_id "
                "= q.record_id"
            )
        )
        return records, int(size)

    def pending_change_events(self) -> tuple[int, int | None]:
        count, oldest = fetch_one(
            self.con.execute("SELECT count(*), min(transaction_id) FROM change_events")
        )
        return count, oldest

    @staticmethod
    def _check_referencing(
        referenced_record_id: int | None,
        record_type: int,
        include_deleted: bool,
        state: str | None,
    ) -> None:
        if referenced_record_id is None and not record_type:
            raise ValueError("give a referenced_record_id, a record_type, or both")
        if include_deleted:
            # the deleted records are those of a type (`slugs`): whatever points at a
            # record is not known of a record that is gone, and it has no state
            if referenced_record_id is not None:
                raise ValueError(
                    "include_deleted lists the records of a type: give a record_type "
                    "and no referenced_record_id"
                )
            if state is not None:
                raise ValueError("include_deleted cannot be used with a state")

    @staticmethod
    def _referencing_where(
        referenced_record_id: int | None,
        record_type: int,
        include_non_public: bool,
        state: str | None,
    ) -> str:
        where = []
        if referenced_record_id is not None:
            where.append(f'list_contains("references", {int(referenced_record_id)})')
        if record_type and referenced_record_id is None:
            # the records of a type: `etags` is kept by its type, no references are read
            where.append(f"record_type = type_slug({int(record_type)})")
        elif record_type:  # the ids of a type are the numbers from type * 10^7 up
            low = int(record_type) * uri.TYPE_BASE
            where.append(f"record_id >= {low} AND record_id < {low + uri.TYPE_BASE}")
        if not include_non_public:
            where.append("is_published")
        if state is not None:
            where.append(f"state = {literal(state)}")
        return " AND ".join(where)

    @staticmethod
    def _deleted_where(number: int) -> str:
        """The deleted records of a type, from `slugs`: one row of each, its number as a
        slug flagged deleted (`modified_at` is when it was deleted)."""
        return (
            f"rec_type = type_slug({int(number)}) AND is_deleted AND slug = "
            "regexp_extract(record_uri, ':([0-9]+)$', 1)"
        )

    def referencing_records(
        self,
        referenced_record_id: int | None = None,
        record_type: int = 0,
        offset: int = 0,
        limit: int = PAGE_SIZE,
        include_non_public: bool = False,
        include_deleted: bool = False,
        state: str | None = None,
        ignore_cache: bool = False,
    ) -> list[int]:
        """The ids of the records that point at the record, one page of them, in the
        order of `etags` (newest created first). `record_type` is the number of a type
        (0: all of them). Give the record that is pointed at, the type, or both; the
        type alone is the records of that type.

        `include_deleted` (OAI-PMH) adds the records of the type that were deleted, so
        only a `record_type` can be given with it, and no `referenced_record_id` or
        `state` (`ValueError`): a record that is gone has no references and no state.
        The page is then sorted on when the records were modified, or deleted (`etags` is
        kept by when a record was created, so the two kinds have to be merged by a sort;
        without `include_deleted` there is none). The ids of the deleted have no etag:
        `etags` gives None for them.

        A page is kept by the cache of the process, by its arguments, while the
        repository is not written (`ignore_cache`: not read, not kept)."""
        self._check_referencing(
            referenced_record_id, record_type, include_deleted, state
        )
        deleted = self._deleted_where(record_type) if include_deleted else None
        where = self._referencing_where(
            referenced_record_id, record_type, include_non_public, state
        )
        if deleted is not None:
            sql = (
                f"SELECT record_id FROM (SELECT record_id, modified_at FROM etags WHERE "
                f"{where} UNION ALL SELECT record_id(record_uri), modified_at FROM "
                f"slugs WHERE {deleted}) ORDER BY modified_at DESC, record_id "
                f"LIMIT {int(limit)} OFFSET {int(offset)}"
            )
        else:
            sql = (
                f"SELECT record_id FROM etags WHERE {where} LIMIT {int(limit)} "
                f"OFFSET {int(offset)}"
            )
        kept = self.cache is not None and not ignore_cache
        if kept:
            self.cache.check()
            repo_last_modified_at = self.cache.repo_last_modified_at(self.repo)
            referencing = self.cache.stamped("referencing")
            key = referencing.hash_of(
                referenced=referenced_record_id,
                record_type=record_type,
                offset=offset,
                limit=limit,
                include_non_public=include_non_public,
                include_deleted=include_deleted,
                state=state,
            )
            found = referencing.get_ids(self.repo, repo_last_modified_at, key)
            if found is not None:
                return found
        ids = [r[0] for r in self.con.execute(sql).fetchall()]
        if kept:
            referencing.put_ids(self.repo, repo_last_modified_at, key, ids)
        return ids

    def count_referencing_records(
        self,
        referenced_record_id: int | None = None,
        record_type: int = 0,
        include_non_public: bool = False,
        include_deleted: bool = False,
        state: str | None = None,
        ignore_cache: bool = False,
    ) -> int:
        """How many records `referencing_records` has in all (kept the same way, by the
        arguments that count: every page of a listing has one total)."""
        self._check_referencing(
            referenced_record_id, record_type, include_deleted, state
        )
        deleted = self._deleted_where(record_type) if include_deleted else None
        where = self._referencing_where(
            referenced_record_id, record_type, include_non_public, state
        )
        kept = self.cache is not None and not ignore_cache
        if kept:
            self.cache.check()
            repo_last_modified_at = self.cache.repo_last_modified_at(self.repo)
            referencing = self.cache.stamped("referencing")
            key = referencing.hash_of(
                referenced=referenced_record_id,
                record_type=record_type,
                include_non_public=include_non_public,
                include_deleted=include_deleted,
                state=state,
                return_count_only=True,
            )
            found = referencing.get_total(self.repo, repo_last_modified_at, key)
            if found is not None:
                return found
        live = f"(SELECT count(*) FROM etags WHERE {where})"
        gone = f"(SELECT count(*) FROM slugs WHERE {deleted})" if deleted else "0"
        total = fetch_one(self.con.execute(f"SELECT {live} + {gone}"))[0]
        if kept:
            referencing.put_total(self.repo, repo_last_modified_at, key, total)
        return total

    def resource_records(self, uris) -> dict[str, tuple[str, str, str | None]]:
        """The record of each resource uri that is the resource uri of one (a reference
        is by the resource uri of a record): its label, its uri and its slug."""
        values = sorted(set(uri_list(uris) or []))
        if not values:
            return {}
        rows = self.con.execute(
            f"SELECT res_uri, label, record_uri(record_id), slug FROM {self.records} "
            f"WHERE res_uri IN (SELECT unnest({sql_list(values)}))"
        ).fetchall()
        return {u: (label or "", record, slug) for u, label, record, slug in rows}

    def page_uris(self, rec_uri: str) -> list[str]:
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

    def slug_lookup(self, rec_type: str, slug: str) -> tuple[int, bool] | None:
        """The id of the record that has the slug, and whether that record is gone.
        Kept by the cache of the process if the store has one (a slug that is not there
        is not kept: it may be made next)."""
        if self.cache is not None:
            self.cache.check()
            repo_last_modified_at = self.cache.repo_last_modified_at(self.repo)
            slug_store = self.cache.stamped("slugs")
            found = slug_store.get(self.repo, repo_last_modified_at, rec_type, slug)
            if found is not None:
                return found
        row = self.con.execute(
            "SELECT record_id(record_uri), is_deleted FROM slugs WHERE rec_type = "
            f"{literal(rec_type)} "
            f"AND slug = {literal(slug)}"
        ).fetchone()
        found = (row[0], bool(row[1])) if row else None
        if found is not None and self.cache is not None:
            slug_store.put(self.repo, repo_last_modified_at, rec_type, slug, found)
        return found

    def current_slug(self, rec_uri: str) -> str | None:
        row = self.con.execute(
            f"SELECT slug FROM {self.records} WHERE record_id = "
            f"record_id({literal(rec_uri)})"
        ).fetchone()
        return row[0] if row else None

    def slugs_of(self, rec_uri: str) -> list[str]:
        rows = self.con.execute(
            f"SELECT slug FROM slugs WHERE record_uri = {literal(rec_uri)} "
            "AND NOT regexp_matches(slug, '^[0-9]+$') ORDER BY slug"
        ).fetchall()
        return [r[0] for r in rows]

    def purge_slugs(self, rec_type: str) -> list[tuple[str, str]]:
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
        row = self.con.execute(
            f"SELECT modified_at FROM {self.records} WHERE record_id = "
            f"record_id({literal(rec_uri)})"
        ).fetchone()
        return row[0] if row else None

    def revisions(
        self, rec_uri: str, limit: int = 200
    ) -> list[tuple[int, object, bool]]:
        rows = self.con.execute(
            "SELECT txn_id, modified_at, is_deleted FROM history "
            f"WHERE record_id = record_id({literal(rec_uri)}) ORDER BY txn_id DESC "
            "LIMIT "
            f"{int(limit)}"
        ).fetchall()
        return [(t, when, bool(deleted)) for t, when, deleted in rows]

    def revision(self, rec_uri: str, txn_id: int) -> tuple[object, bool, str] | None:
        row = self.con.execute(
            "SELECT modified_at, is_deleted, source FROM history "
            f"WHERE record_id = record_id({literal(rec_uri)}) AND txn_id = "
            f"{int(txn_id)} "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return (row[0], bool(row[1]), row[2]) if row else None

    def revision_before(self, rec_uri: str, txn_id: int) -> int | None:
        row = self.con.execute(
            "SELECT max(txn_id) FROM history WHERE record_id = "
            f"record_id({literal(rec_uri)}) "
            f"AND txn_id < {int(txn_id)}"
        ).fetchone()
        return row[0] if row else None

    def changes_since(
        self, number: int, limit: int | None = None
    ) -> list[tuple[int, str, bool]]:
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
        self._provider = provider
        self.hooks = self._roles = None

    def sync(self) -> None:
        self._configured()

    def _configured(self) -> list[tuple[str, str]]:
        if self._provider is None:
            return []
        if self.hooks is None:
            types = list(self._provider())
            self.hooks = [
                (res_type.slug, statement.replace("@TRIPLES@", self.triples))
                for res_type in types
                for statement in res_type.sql_hooks
            ]
            self._roles = [
                (res_type.slug, role, path, place)
                for res_type in types
                for role, path, place in res_type.property_roles
            ]
            self.set_property_roles(self._roles)
            # the numbers of the types: what the ids of the etags are made of
            self.set_type_ids([(res_type.slug, res_type.number) for res_type in types])
        return self.hooks

    def flatten(self) -> None:
        self.con.execute(
            f"CREATE OR REPLACE TABLE {self.flat} AS SELECT * FROM "
            f"flatten_triples({literal(self.staging)})"
        )
        hooks = self._configured()
        if hooks:
            # only the hooks of the types that are in the batch (each is for the records
            # of its own type): every statement is a round trip to the database, which
            # may be in another process
            found = {
                row[0]
                for row in self.con.execute(
                    f"SELECT DISTINCT split_part(s, ':', 2) FROM {self.flat} "
                    "WHERE starts_with(s, 'rec:')"
                ).fetchall()
            }
            for slug, statement in hooks:
                if slug in found:
                    self.con.execute(statement.replace("@FLAT@", self.flat))

    def rederive(
        self, rec_type: str, numbers: tuple[int, int] | None = None
    ) -> list[str]:
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
            for slug, statement in self._configured():
                if slug == rec_type:  # the others are for other records
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
        rows = self.con.execute(
            f"SELECT warning FROM check_triples({literal(self.staging)})"
        )
        return [r[0] for r in rows.fetchall()]

    def non_records(self, limit: int = 5) -> list[str]:
        rows = self.con.execute(
            f"""SELECT DISTINCT s FROM {self.staging}
                WHERE s_type = 'uri' AND NOT regexp_matches(s,
                  {literal(uri.RECORD_PATTERN)})
                ORDER BY s LIMIT {int(limit)}"""
        )
        return [r[0] for r in rows.fetchall()]

    def load(self) -> Counter:
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
        rows = self.con.execute(
            f"SELECT rec_type, count(*) FROM {self.records} GROUP BY rec_type"
        )
        return dict(rows.fetchall())

    def type_stats(self) -> dict[str, dict]:
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
        pages, size, modified = fetch_one(
            self.con.execute(
                "SELECT count(*), coalesce(sum(closure_bytes), 0), "
                "max(last_modified_at) FROM etags"
            )
        )
        return {"pages": pages, "bytes": int(size), "modified": modified}

    def pending_summary(self) -> dict:
        rows, oldest = self.pending_change_events()
        records, size = self.pending_size()
        change_events = dict(
            self.con.execute(
                "SELECT kind, count(*) FROM change_events GROUP BY kind"
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
            "change_events": change_events,
            "oldest": oldest,
            "since": since,
            "last": self.last_transaction(),
        }

    def count_by_predicate(self) -> dict[str, int]:
        rows = self.con.execute(f"SELECT p, count(*) FROM {self.triples} GROUP BY p")
        return dict(rows.fetchall())

    # ---- prefixes
    # ------------------------------------------------------------------------

    def used_prefixes(self, prefixes: dict[str, str]) -> dict[str, str]:
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
        rows = self.con.execute(
            f"SELECT record_uri(record_id) FROM {self.records} WHERE rec_type = "
            f"{literal(rec_type)} "
            "ORDER BY record_id"
        ).fetchall()
        return [r[0] for r in rows]

    def records_with_resource_uris(self, resource_uris: list[str]) -> dict[str, str]:
        if not resource_uris:
            return {}
        rows = self.con.execute(
            f"SELECT res_uri, record_uri(record_id) FROM {self.records} "
            f"WHERE res_uri IN (SELECT unnest({sql_list(sorted(set(resource_uris)))}))"
        ).fetchall()
        return dict(rows)

    def record_with_resource_uri(self, resource_uri: str) -> str | None:
        row = self.con.execute(
            f"SELECT record_uri(record_id) FROM {self.records} WHERE res_uri = "
            f"{literal(resource_uri)} "
            "ORDER BY record_id LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def _ensure_ids(self) -> None:
        if not self._ids_ready:
            self.con.execute(self._sql("ids.sql"))
            self._ids_ready = True

    def _highest_record(self, res_type: str) -> str:
        return (
            f"coalesce((SELECT max(record_id % 10000000) FROM {self.records} "
            f"WHERE rec_type = {literal(res_type)}), 0)"
        )

    def _highest_used(self, res_type: str) -> str:
        number = self.type_numbers().get(res_type)  # the ids of a type are one range
        first = "NULL" if number is None else number * uri.TYPE_BASE
        return (
            f"greatest({self._highest_record(res_type)}, coalesce((SELECT max("
            f"record_id % 10000000) FROM history "
            f"WHERE record_id BETWEEN {first} AND {first} + 9999999), 0))"
        )

    def last_id(self, res_type: str) -> int:
        self._ensure_ids()
        row = self.con.execute(
            "SELECT greatest(coalesce((SELECT greatest(last, reserved) FROM "
            f"{self.ids} "
            f"WHERE type = {literal(res_type)}), 0), {self._highest_used(res_type)})"
        ).fetchone()
        return row[0]

    def highest_number(self, res_type: str) -> int:
        return fetch_one(self.con.execute(f"SELECT {self._highest_record(res_type)}"))[
            0
        ]

    def numbering(self, res_type: str) -> dict[str, int]:
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
        row = self.con.execute(
            f"SELECT json FROM export_triples({literal(self.triples)}, uris := "
            f"{_uris_arg(uris)})"
        ).fetchone()
        return row[0]

    def _records_where(
        self,
        uris=None,
        rec_type: str | None = None,
        numbers: tuple[int, int] | None = None,
    ) -> str:
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
        directory = Path(directory).absolute()
        # what is read is the etags: a write outside the store has none yet
        self.catch_up_etags()
        bound = self.last_transaction()
        select = (
            f"WITH q AS (SELECT record_id, arg_max(kind, transaction_id) AS kind, "
            "max(transaction_id) AS txn FROM change_events WHERE transaction_id <= "
            f"{bound} GROUP "
            "BY record_id) "
            "SELECT '# ' || CASE WHEN g.record_id IS NULL OR q.kind = 'delete' THEN "
            "'delete' "
            "ELSE printf('%016x', g.etag) END || ' ' || q.kind || chr(10) || "
            "CASE WHEN g.record_id IS NULL OR q.kind = 'delete' "
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

    def etags(self, record_ids) -> dict[int, str | None]:
        """The etag of each record (None: there is no such record), kept by the cache
        of the process if the store has one."""
        ids = list(dict.fromkeys(record_ids))
        found: dict[int, str | None] = {}
        if self.cache is not None and ids:
            self.cache.check()
            # taken once: what is read and what is kept after it are of this time
            repo_last_modified_at = self.cache.repo_last_modified_at(self.repo)
            etag_store = self.cache.stamped("etags")
            found = etag_store.get_many(self.repo, repo_last_modified_at, ids)
        missing = [i for i in ids if i not in found]
        if missing:
            listed = ", ".join(str(int(i)) for i in missing)
            rows = self.con.execute(
                f"SELECT record_id, etag FROM etags WHERE record_id IN ({listed})"
            ).fetchall()
            etag_records: dict[int, str | None] = {}
            for record_id in missing:
                etag_records[record_id] = None  # a record that is not there: kept too
            for record_id, etag in rows:
                etag_records[record_id] = f"{etag:016x}"
            if self.cache is not None:
                etag_store.put_many(self.repo, repo_last_modified_at, etag_records)
            found.update(etag_records)
        return {i: found[i] for i in record_ids}

    def get_doc(self, record_uri: str) -> dict | None:
        """The doc of a record (None: there is no such record), made by the stylesheets
        of the repository and kept by the cache, by the etag of the record. Only the
        store of a repository (`Repository.store`) has the stylesheets."""
        if self.cache is None or self.repository is None:
            raise ValueError("a doc is made by the store of a repository with a cache")
        record_id = self.record_id_of(record_uri)
        if record_id is None:
            return None
        etag = self.etags([record_id])[record_id]
        if etag is None:
            return None
        docs = self.cache.docs
        if docs.keeps:
            fingerprint = docs.fingerprint(
                self.repository, docs.compiled(self.repository)
            )
            kept = docs.get(self.repo, record_id, etag, fingerprint)
            if kept is not None:
                return json.loads(kept)
        xml = self.get_multi([record_id], compound_records=True, format="rdfxml")
        if not xml:
            return None
        return docs.bulk_update(
            self.repository, {record_uri: record_id}, xml, {record_id: etag}
        )[record_uri]

    def get(self, uri, **kwargs):
        return self.get_multi([uri], **kwargs)

    def get_multi(
        self,
        records,
        compound_records: bool = False,
        ignore_cache: bool = False,
        format: str | None = None,
    ):
        """The triples of records, by uri or by id: a `Graph` (what is not there is not
        in it), or text in a `format` (`rdfxml`, which is flat as the cache keeps it,
        `ntriples`, `turtle`, ...).

        A compound record is the record and every record it is made of. The graph cache
        keeps it under the etag of the record, so what the cache has is always the
        current one. One that it lacks is read from the database, and the worker of the
        cache is asked to make it (`ChangeEventsWorker.rework`). `ignore_cache` reads and writes
        neither the graph cache nor the etags of it (for tests)."""
        ids = []
        seen = set()
        for record in records:
            if isinstance(record, int):
                record_id = record
            else:
                if isinstance(record, ox.NamedNode):
                    record = record.value
                record_id = self.record_id_of(record)
            if record_id is not None and record_id not in seen:
                seen.add(record_id)
                ids.append(record_id)

        graph_cache = None
        worker = None
        if self.cache is not None and not ignore_cache:
            graph_cache = self.cache.graph_store
            worker = self.cache.worker
        ntriples_from_db: list[str] = []
        xml_from_cache: dict[int, bytes] = {}
        if compound_records and graph_cache is not None:
            etags = self.etags(ids)
            stale = []
            for record_id in ids:
                etag = etags[record_id]
                if etag is None:  # there is no such record
                    continue
                kept = graph_cache.get(self.repo, record_id, etag)
                if kept:
                    xml_from_cache[record_id] = kept
                else:
                    stale.append(record_id)
            for etag, text in self.fetch_compound_graphs(stale).values():
                ntriples_from_db.append(text)
            if stale and worker is not None:
                # the worker makes their graphs for the cache (and their docs, and what
                # follows), without this read waiting for it
                worker.rework(self.repo, stale)
        elif compound_records:
            for etag, text in self.fetch_compound_graphs(ids).values():
                ntriples_from_db.append(text)
        elif ids:
            listed = ", ".join(str(record_id) for record_id in ids)
            rows = self.con.execute(
                f"SELECT source FROM {self.records} WHERE record_id IN ({listed})"
            ).fetchall()
            for row in rows:
                ntriples_from_db.append(row[0])

        if (
            format == "rdfxml" and len(xml_from_cache) == 1
        ):  # a cached one passes through
            return next(iter(xml_from_cache.values())).decode()
        quads = list(
            ox.parse(
                "\n".join(ntriples_from_db).encode(), format=ox.RdfFormat.N_TRIPLES
            )
        )
        for text in xml_from_cache.values():
            quads.extend(ox.parse(text, format=ox.RdfFormat.RDF_XML))
        dataset = ox.Dataset(quads)
        if format is None:
            return Graph(dataset)
        if not len(dataset):  # none of them is there
            return ""
        if format == "rdfxml":
            return dataset_rdfxml(dataset).decode()
        return Graph(dataset).serialize(format)

    def put_multi(self, graph: "Graph | ox.Dataset", strict: bool = False) -> Counter:
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


def new_store(database, repo_id: str, connection=None, cache=None) -> Store:
    try:
        if repo_id != DEFAULT_REPOSITORY:
            validate_repository_id(repo_id)
    except ValueError as err:
        raise NoSuchRepository(repo_id) from err
    cursor = connection if connection is not None else database.cursor()
    try:
        return Store(
            cursor,
            repo=repo_id,
            init=False,
            close_connection=connection is None,
            cache=cache,
        )
    except BaseException:
        if connection is None:
            cursor.close()
        raise
