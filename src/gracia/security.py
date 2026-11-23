import base64
import binascii

from pyramid.authorization import ACLHelper, Authenticated, Everyone
from pyramid.location import lineage

from gracia.resources import Repository


class UsersPolicy:
    challenge = True

    def _login(self, request, context):
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            userid, _, password = base64.b64decode(value).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        repository = next(
            (n for n in lineage(context) if isinstance(n, Repository)), None
        )
        cache = request.environ.setdefault("gracia.login", {})
        key = repository.__name__ if repository else None
        if key not in cache:
            cache[key] = self._check(repository, userid, password)
        return cache[key]

    def _check(self, repository, userid: str, password: str):
        groups = repository.login(userid, password) if repository is not None else None
        return (userid, groups) if groups is not None else None

    def authenticated_userid(self, request):
        login = self._login(request, getattr(request, "context", None))
        return login[0] if login else None

    identity = authenticated_userid

    def permits(self, request, context, permission):
        principals = {Everyone}
        login = self._login(request, context)
        if login:
            userid, groups = login
            principals |= {
                Authenticated,
                f"user:{userid}",
                *(f"group:{g}" for g in groups),
            }
        return ACLHelper().permits(context, principals, permission)

    def remember(self, request, userid, **kw):
        return []

    def forget(self, request, **kw):
        return []
