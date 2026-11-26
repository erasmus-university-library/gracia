from typing import ClassVar

from pyramid.authorization import ALL_PERMISSIONS, Allow, Everyone

from gracia import init, uri
from gracia.cache import Cache
from gracia.database import Database
from gracia.resources import Repository
from gracia.store import DEFAULT_REPOSITORY, Store, new_store, validate_repository_id
from gracia.utils.validate import validate_ark, validate_password, validate_userid

EDITING = ["edit", "add"]


class Application:
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

    def __getitem__(self, repo_id: str) -> Repository:
        if not self.database.knows(repo_id):
            raise KeyError(repo_id)
        return Repository(self.registry, repo_id, self)

    def list_repositories(self) -> list[str]:
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
                store.put_multi(
                    init.initial_dataset(
                        repo_id, store.mint_id, admin, content_types, ark
                    )
                )
                self.database.known.add(repo_id)
                node = self[repo_id]
                node.use_store(store)
                node.sync_roles()
        self.database.known.add(repo_id)
        return not existed

    def new_store(self, repo_id: str) -> Store:
        return new_store(self.database, repo_id, cache=self.cache)
