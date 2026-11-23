from typing import ClassVar

from pyramid.authorization import ALL_PERMISSIONS, Allow, Everyone

from gracia import init, uri
from gracia.cache import Cache
from gracia.database import Database
from gracia.resources import Repository
from gracia.store import DEFAULT_REPOSITORY, Store, new_store, validate_repository_id
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

    def __init__(self, db, registry):
        """The root of the tree for one request: `db` is the cursor of the request (the
        stores of its repository are scoped to a schema on it; None: a store opens a
        cursor of its own, which is what a command, a job or a test has), `registry` the
        registry of the application, where what lives across requests is (`database`,
        `cache`, `indexes`: `gracia.main.init` puts them there)."""
        self.db = db
        self.registry = registry

    @property
    def database(self) -> Database:
        return self.registry["database"]

    @property
    def cache(self) -> Cache:
        return self.registry["cache"]

    @property
    def indexes(self):
        return self.registry["indexes"]

    @indexes.setter
    def indexes(self, indexes) -> None:
        self.registry["indexes"] = indexes

    @property
    def remote(self) -> bool:
        return self.database.remote

    def __getitem__(self, repo_id: str) -> Repository:
        """The node of a repository: a new one for whoever asks, as it holds the store
        and what was read for one request. The database is asked only for an id not seen
        before."""
        if not self.database.knows(repo_id):
            raise KeyError(repo_id)
        return Repository(self.registry, repo_id, self)

    def list_repositories(self) -> list[str]:
        """The ids of all repositories: the schemas that hold the gracia tables."""
        return self.database.list_repositories()

    def has_repository(self, repo_id: str) -> bool:
        return self.database.has_repository(repo_id)

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
            self.database.cursor(), repo=repo_id, create=True, close_connection=True
        ) as store:
            if not existed:
                store.put(
                    init.initial_dataset(
                        repo_id, store.mint_id, admin, content_types, ark
                    )
                )
                # what the shapes of its types say (the key info of the users, say) is
                # in the database from the start
                # (the store has the schema, in a transaction that is not seen yet)
                self.database.known.add(repo_id)
                node = self[repo_id]
                node.use_store(store)
                node.sync_roles()
        self.database.known.add(repo_id)
        return not existed

    def new_store(self, repo_id: str) -> Store:
        """A Store for an existing repository, on its own cursor. Raises
        NoSuchRepository. The caller closes it."""
        return new_store(self.database, repo_id)
