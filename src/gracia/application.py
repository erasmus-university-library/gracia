import threading
from pathlib import Path
from typing import ClassVar

import duckdb
from pyramid.authorization import ALL_PERMISSIONS, Allow, Everyone

from gracia import config, init, uri
from gracia.cache import Cache
from gracia.resources import Repository
from gracia.store import (
    DEFAULT_REPOSITORY,
    NoSuchRepository,
    RemoteConnection,
    Store,
    literal,
    validate_repository_id,
)
from gracia.utils.validate import validate_ark, validate_password, validate_userid

# what editors may do with records; managers may do that too
EDITING = ["edit", "add"]


class Application:
    """One database and the repositories in it."""

    __name__ = None  # the root of the traversal tree
    __parent__ = None
    # admins may do everything, editors edit, everyone may view
    __acl__: ClassVar[list] = [
        (Allow, "group:admin", ALL_PERMISSIONS),
        (Allow, "group:editor", EDITING),
        (
            Allow,
            "group:manager",
            EDITING,
        ),  # managers also manage users, see resources.TYPE_ACLS
        (Allow, Everyone, "view"),
    ]

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        token: str | None = None,
        token_file: str | Path | None = None,
        watch: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ):
        """`path` is a database file, or `quack:host:port` to use the database that a
        running `gracia serve-db` owns (then `token` or `token_file` is required).

        `cache` (see `gracia.cache`) watches the database file for writes: `path`
        itself, or, over Quack, `watch`, the file the owner opened (when it is on this
        machine; else the table is read at every check). `cache_dir` is where it keeps
        the graphs of the records (none: they are made again every time)."""
        self.remote = config.is_remote(path)
        self._catalog = None  # the attached catalog, when remote
        if self.remote:
            self._attach(str(path), token, token_file)
            self._con = None
        else:
            self._con = duckdb.connect(str(path))
        self._lock = threading.Lock()  # cursor creation on the shared connection
        if watch is None and not self.remote and str(path) != ":memory:":
            watch = path
        self.cache = Cache(self, watch=watch, graph_dir=cache_dir)
        # the search indexes (`gracia.search.Indexes`): set when `gracia.index_dir` is
        self.indexes = None

    def _attach(self, target: str, token: str | None, token_file) -> None:
        """Remember how to reach the owner and check that it can be reached. Each
        `Store` gets its own attachment (see `store.RemoteConnection`), made by
        `_connect`."""
        config.parse_target(target)  # a clear error for a malformed target
        if token is None:
            token = config.read_token({"gracia.db.token_file": str(token_file or "")})
        self._target, self._token, self._catalog = target, token, "gracia_db"
        self._connect().close()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        """A new local connection attached to the owner (its own session there)."""
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
        self.cache.close()
        if self._con is not None:
            self._con.close()

    def __getitem__(self, repo_id: str) -> Repository:
        if self.has_repository(repo_id):
            return Repository(repo_id, self)
        raise KeyError(repo_id)

    def __enter__(self) -> "Application":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _cursor(self):
        if self.remote:  # a connection of its own: one session on the owner per store
            return self._connect()
        with self._lock:
            return self._con.cursor()

    def raw(self):
        """A connection for plain SQL outside any repository (the jobs tables): a cursor
        on the file, or, over Quack, a connection whose statements run on the owner.
        `execute(sql, params)` takes `?` placeholders in both cases. The caller closes
        it."""
        cursor = self._cursor()
        if self.remote:
            return RemoteConnection(cursor, self._catalog, self._connect)
        return cursor

    def list_repositories(self) -> list[str]:
        """The ids of all repositories: the schemas that hold the gracia tables."""
        cursor = self._cursor()
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

    def create_repository(
        self,
        repo_id: str,
        admin: tuple[str, str] | None = None,
        content_types=(),
        ark: tuple[str, str] = (uri.DEFAULT_NAAN, uri.DEFAULT_ROOT_PREFIX),
    ) -> bool:
        """Create a repository (a schema with the tables and macros) and load its
        initial data into its graph: the repository resource, the types (the three it
        needs, and the `content_types` given), the groups `admin`, `editor` and
        `manager` and, with `admin=(userid, password)`, a first user in the group
        `admin` (the password is stored salted and hashed). Idempotent: returns True
        when it was created, False when it already existed (nothing is loaded again,
        `admin` is ignored). `main` can be created like any other; when `main` appears
        implicitly (a command used it first) it has no data."""
        if repo_id != DEFAULT_REPOSITORY:
            validate_repository_id(repo_id)
        if admin:  # refuse before anything is created
            validate_userid(admin[0])
            validate_password(admin[1])
        content_types = list(content_types)
        ark = validate_ark(*ark)
        init.initial_dataset(
            repo_id, lambda res_type: 1, None, content_types, ark
        )  # check first
        existed = self.has_repository(repo_id)
        with Store(
            connection=self._cursor(),
            repo=repo_id,
            create=True,
            close_connection=True,
            remote=self._connect if self.remote else None,
        ) as store:
            if not existed:
                store.put(
                    init.initial_dataset(
                        repo_id, store.mint_id, admin, content_types, ark
                    )
                )
                node = Repository(
                    repo_id, self
                )  # what the shapes of its types say (the key info
                try:  # of the users, say) is in the database from the start
                    node.sync_roles(store)
                finally:
                    node.close()
        return not existed

    def open_repository(self, repo_id: str) -> Store:
        """A Store for an existing repository, on its own cursor. Raises
        NoSuchRepository. The caller closes it."""
        try:
            if repo_id != DEFAULT_REPOSITORY:
                validate_repository_id(repo_id)
        except ValueError as err:
            raise NoSuchRepository(repo_id) from err
        cursor = self._cursor()
        try:
            return Store(
                connection=cursor,
                repo=repo_id,
                init=False,
                close_connection=True,
                remote=self._connect if self.remote else None,
            )
        except BaseException:
            cursor.close()
            raise
