import contextlib
import os
import secrets
import stat
import threading
from pathlib import Path

import duckdb

from gracia import config


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_value(value) -> str:
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
    parts = sql.split("?")
    if len(parts) != len(params) + 1:
        raise ValueError("the number of parameters does not match the placeholders")
    out = [parts[0]]
    for value, part in zip(params, parts[1:], strict=True):
        out.append(sql_value(value) + part)
    return "".join(out)


class RemoteConnection:
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


class Database:
    def __init__(
        self,
        db_file: str | Path | None = ":memory:",
        *,
        quack: str | None = None,
        token: str | None = None,
    ):
        self.remote = quack is not None
        self._catalog = None  # the attached catalog, when remote
        if quack is not None:
            config.parse_target(quack)  # a clear error for a malformed address
            if not token:
                raise ValueError("a quack database needs a token")
            self._target, self._token, self._catalog = quack, token, "gracia_db"
            self.connect().close()  # a wrong address or token fails here
            self._con = None
        else:
            self._con = duckdb.connect(str(db_file))
        self._lock = threading.Lock()  # cursor creation on the shared connection
        # the ids of the repositories that are known to be there: the database is
        # asked only for an id that is not in it
        self.known: set[str] = set()

    def connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        try:
            con.execute("INSTALL quack; LOAD quack")
            attach = (
                f"ATTACH {literal(self._target)} AS {self._catalog} "
                f"(TOKEN {literal(self._token)})"
            )
            con.execute(attach)
            con.execute(f"USE {self._catalog}")
        except duckdb.Error as err:
            con.close()
            raise ValueError(
                f"cannot reach the database owner at {self._target}: {err}"
            ) from err
        return con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()

    def cursor(self):
        if self.remote:
            return RemoteConnection(self.connect(), self._catalog, self.connect)  # ty: ignore[invalid-argument-type]
        with self._lock:
            return self._con.cursor()  # ty: ignore[missing-union-attribute]

    def list_repositories(self) -> list[str]:
        cursor = self.cursor()
        try:
            rows = cursor.execute(
                "SELECT DISTINCT schema_name FROM duckdb_tables() "
                "WHERE database_name = current_database() AND table_name = 'triples' "
                "ORDER BY schema_name"
            ).fetchall()
        finally:
            cursor.close()
        return [r[0] for r in rows]

    def has_repository(self, repo_id: str) -> bool:
        return repo_id in self.list_repositories()

    def knows(self, repo_id: str) -> bool:
        if repo_id not in self.known:
            if not self.has_repository(repo_id):
                return False
            self.known.add(repo_id)
        return True


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
    return Path(f"{db}.owner")


def read_owner(db: str | Path) -> tuple[str, str] | None:
    try:
        uri, token_file = owner_file(db).read_text(encoding="utf-8").splitlines()[:2]
    except (OSError, ValueError):
        return None
    return uri, token_file


def read_or_create_token(path: Path) -> str:
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
