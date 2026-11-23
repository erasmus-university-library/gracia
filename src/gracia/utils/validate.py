import re

from gracia.utils import passwords

_USERID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NAAN = re.compile(r"^[0-9A-Za-z]{1,32}$")
_ROOT_PREFIX = re.compile(r"^[A-Za-z0-9]{1,32}$")


def validate_ark(naan: str, root_prefix: str) -> tuple[str, str]:
    if not isinstance(naan, str) or not _NAAN.match(naan):
        raise ValueError(f"invalid ARK authority number: {naan!r} (letters and digits)")
    if not isinstance(root_prefix, str) or not _ROOT_PREFIX.match(root_prefix):
        raise ValueError(
            f"invalid ARK root prefix: {root_prefix!r} (letters and digits)"
        )
    return naan, root_prefix


def validate_userid(userid: str) -> str:
    if not isinstance(userid, str) or not _USERID.match(userid):
        raise ValueError(
            f"invalid user id: {userid!r} (lowercase letters, digits, . _ -, at most "
            "64)"
        )
    return userid


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < passwords.MINIMUM_LENGTH:
        raise ValueError(
            f"the password must have at least {passwords.MINIMUM_LENGTH} characters"
        )
    return password
