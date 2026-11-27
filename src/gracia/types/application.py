from typing import ClassVar

from pyramid.authorization import ALL_PERMISSIONS, Allow, Everyone
from pyramid.httpexceptions import (
    HTTPGone,
    HTTPMovedPermanently,
    HTTPPermanentRedirect,
)
from pyramid.traversal import resource_path
from pyramid.view import (
    exception_view_config,
    forbidden_view_config,
    notfound_view_config,
    view_config,
)

from gracia import presets, uri
from gracia.cache import Cache
from gracia.database import Database
from gracia.store import DEFAULT_REPOSITORY, Store, new_store, validate_repository_id
from gracia.types.repository import Repository
from gracia.types.type import Record, RecordDeleted, RecordMoved
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
        ),  # managers also manage users, see security.TYPE_ACLS
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
        preset: str = presets.DEFAULT_PRESET,
    ) -> bool:
        """Makes the repository and runs the preset (`gracia.presets`) in it; a
        repository
        that is there is left as it is (False)."""
        if repo_id != DEFAULT_REPOSITORY:
            validate_repository_id(repo_id)
        if admin:  # refuse before anything is created
            validate_userid(admin[0])
            validate_password(admin[1])
        ark = validate_ark(*ark)
        preset_class = presets.find(preset)
        existed = self.has_repository(repo_id)
        node = Repository(self.registry, repo_id, self)
        run = preset_class(node, admin=admin, ark=ark, content_types=content_types)
        run.validate()  # check first, before anything is made
        with Store(
            self.database.cursor(), repo=repo_id, create=True, close_connection=True
        ) as store:
            if not existed:
                self.database.known.add(repo_id)
                node.use_store(store)
                run.apply()
        self.database.known.add(repo_id)
        return not existed

    def new_store(self, repo_id: str) -> Store:
        return new_store(self.database, repo_id, cache=self.cache)


class ApplicationView:
    def __init__(self, request):
        self.request = request
        self.context = getattr(request, "context", None)

    @view_config(context=Application, renderer="json", request_method="GET")
    def repositories(self):
        return {"repositories": self.request.root.list_repositories()}

    @exception_view_config(RecordDeleted)
    def gone(self):
        """A record that was deleted: not a 404, it was there (a deleted number is not
        given out again, and its slugs stay taken)."""
        return HTTPGone()

    @exception_view_config(RecordMoved, renderer="json")
    def moved(self):
        """Every record has one address: its slug (`/lib/person/ada-lovelace`), or its
        number (`/lib/person/12`) while it has no slug. The other ways to name it keep
        working (its number once it has a slug, a slug it had before: a slug can be
        changed, and the old ones are kept), as a permanent redirect to where the record
        is now: `/lib/person/12/edit?saved=1` goes to
        `/lib/person/ada-lovelace/edit?saved=1`. What follows the record in the address
        and the query are kept, and so is the method (a POST stays a POST)."""
        request, moved = self.request, self.request.exception
        record = moved.record
        request.context = record  # as if it was reached: who asks is known from it
        if not request.has_permission("view", record):
            return self.refuse(record)  # where a record is is not told to who may not
        # what comes after the record in the address: its view, and what is below
        start = resource_path(record.__parent__) + "/" + moved.key
        rest = []
        if request.path_info.startswith(start):
            rest = [part for part in request.path_info[len(start) :].split("/") if part]
        target = request.resource_url(record, *rest)
        if not rest:
            target = target.rstrip("/")  # the address of the record has no slash
        if request.query_string:
            target += f"?{request.query_string}"
        if request.method in ("GET", "HEAD"):
            return HTTPMovedPermanently(location=target)
        return HTTPPermanentRedirect(location=target)

    @notfound_view_config(renderer="json")
    def not_found(self):
        request = self.request
        context = getattr(request, "context", None)
        if context is not None and not request.has_permission("view", context):
            # something below a node the caller may not see: do not say whether it
            # exists, or the difference between 404 and 401/403 lists the users and
            # groups to the public
            return self.forbidden()
        request.response.status = 404
        return {"error": "not_found", "path": request.path}

    @forbidden_view_config(renderer="json")
    def forbidden(self):
        return self.refuse(getattr(self.request, "context", None))

    def refuse(self, context):
        """The answer to someone who may not see `context`: not there (a record, in a
        type
        they may see), else a login is asked for, else forbidden."""
        request = self.request
        if (
            isinstance(context, Record)
            and not request.has_permission("view", context)
            and request.has_permission("view", context.__parent__)
        ):
            # a record the caller may not see, in a type they may: it is not there for
            # them
            request.response.status = 404
            return {"error": "not_found", "path": request.path}
        if request.authenticated_userid is None and getattr(
            request.registry, "challenge", False
        ):
            # not logged in: ask the browser for a login instead of refusing for good
            request.response.status = 401
            request.response.headers["WWW-Authenticate"] = (
                'Basic realm="gracia", charset="UTF-8"'
            )
            return {"error": "unauthorized", "path": request.path}
        request.response.status = 403
        return {"error": "forbidden", "path": request.path}
