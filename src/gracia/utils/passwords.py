import base64
import hashlib
import hmac
import os

N, R, P = 2**14, 8, 1
MINIMUM_LENGTH = 8


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = _scrypt(password, salt, N, R, P)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${N}${R}${P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY = None


def verify_nothing(password: str) -> bool:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not a password")
    verify_password(password, _DUMMY)
    return False
