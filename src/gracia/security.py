import base64
import binascii

from pyramid.authorization import ACLHelper, Authenticated, Everyone
from pyramid.location import lineage

from gracia.resources import Repository


class UsersPolicy:
    """Logging in with HTTP Basic against the users of the repository a request reaches.

    A user is a record of type User; its `gracia:memberOf` lists the resource uris of
    group records, and the name of a group is its principal: members of `admin` are
    `group:admin`, which the ACLs already know. Users of one repository mean nothing in
    another. Use it over TLS or on localhost only. Browsers show their own login box
    when the forbidden view answers 401 with `WWW-Authenticate` (`challenge`)."""

    challenge = True

    def _login(self, request, context):
        """(userid, group names) of the caller in the repository of `context`, or None.
        Checked once per request and repository: a password check is meant to be
        slow."""
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
