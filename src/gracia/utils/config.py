import configparser
import os
import re
import stat
from pathlib import Path

SECTION = "app:main"
DEFAULT_DB = "gracia.db"


def read(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"no such config file: {path}")
    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=("#",)
    )
    parser.read(path, encoding="utf-8")
    if not parser.has_section(SECTION):
        raise ValueError(f"{path}: no [{SECTION}] section")
    settings = {
        k: v.strip() for k, v in parser.items(SECTION) if k.startswith("gracia.")
    }
    for key in (
        "gracia.db.file",
        "gracia.db.token_file",
        "gracia.jobs_dir",
        "gracia.cache_dir",
        "gracia.index_dir",
    ):  # from the config file
        if settings.get(key) and not Path(settings[key]).is_absolute():
            settings[key] = str(path.parent / settings[key])
    if (
        settings.get("gracia.db")
        and not is_remote(settings["gracia.db"])
        and not Path(settings["gracia.db"]).is_absolute()
    ):
        settings["gracia.db"] = str(path.parent / settings["gracia.db"])
    return settings


CACHE_SIZES = ("graphs", "docs", "etags", "slugs", "referencing")
_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(text: str) -> int:
    """`256M`, `1G`, `512MB`, `65536`: bytes (the units are powers of 1024)."""
    found = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*", text.lower())
    if not found:
        raise ValueError(f"invalid size {text!r}: expected a number and K, M, G or T")
    size = int(float(found[1]) * _UNITS[found[2]])
    if size <= 0:
        raise ValueError(f"invalid size {text!r}: it has to be more than nothing")
    return size


def cache_sizes(settings: dict) -> dict[str, int]:
    """The sizes the config sets (`gracia.cache.<name>_size`); the rest is the
    default."""
    sizes = {}
    for name in CACHE_SIZES:
        key = f"gracia.cache.{name}_size"
        if settings.get(key):
            try:
                sizes[name] = parse_size(settings[key])
            except ValueError as err:
                raise ValueError(f"{key}: {err}") from None
    return sizes


def is_remote(target: str | Path) -> bool:
    return str(target).startswith("quack:")


def parse_target(target: str) -> tuple[str, int]:
    parts = target.split(":")
    if len(parts) != 3 or parts[0] != "quack" or not parts[1] or not parts[2].isdigit():
        raise ValueError(f"invalid target {target!r}: expected quack:host:port")
    return parts[1], int(parts[2])


def read_secret(path: str | Path, minimum: int = 16) -> str:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"no such file: {path}")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(
            f"{path} is readable by group or others: run `chmod 600 {path}`"
        )
    secret = path.read_text(encoding="utf-8").strip()
    if len(secret) < minimum:
        raise ValueError(
            f"the secret in {path} is too short (at least {minimum} characters)"
        )
    return secret


def read_token(settings: dict[str, str]) -> str:
    if settings.get("gracia.db.token"):
        return settings["gracia.db.token"]
    path = settings.get("gracia.db.token_file")
    if not path:
        raise ValueError(
            "a quack: database needs a token: set gracia.db.token_file (or "
            "gracia.db.token) "
            "in the config file"
        )
    if not Path(path).is_file():
        raise ValueError(
            f"no such token file: {path} (it is created by `gracia serve-db`)"
        )
    return read_secret(path)


DEFAULT_FILE = "config.ini"


def config_file(flag=None, environ=None, cwd=None) -> Path | None:

    environ = os.environ if environ is None else environ
    if flag:
        return Path(flag)
    if "GRACIA_CONFIG" in environ:
        return Path(environ["GRACIA_CONFIG"]) if environ["GRACIA_CONFIG"] else None
    candidate = Path(cwd) / DEFAULT_FILE if cwd else Path.cwd() / DEFAULT_FILE
    return candidate if candidate.is_file() else None


def resolve(flag=None, db=None, environ=None, cwd=None) -> dict[str, str]:

    environ = os.environ if environ is None else environ
    settings = read(config_file(flag, environ, cwd))
    chosen = db or environ.get("GRACIA_DB")
    if chosen:
        settings["gracia.db"] = str(chosen)
    settings.setdefault("gracia.db", DEFAULT_DB)
    return settings
