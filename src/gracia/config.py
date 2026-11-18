"""The configuration file (`config.ini`), shared by `serve-db`, `serve` and the CLI.

    [app:main]
    gracia.db = quack:localhost:9494       # where clients connect (or a file, opened directly)
    gracia.db.file = gracia.db             # the file `serve-db` opens
    gracia.db.token_file = gracia.db.token
    gracia.jobs_dir = /var/lib/gracia/jobs  # where jobs keep their files (see below)
    gracia.cache_dir = /var/cache/gracia    # where the web server keeps the RDF/XML of the records
    gracia.index_dir = /var/lib/gracia/index  # where the search indexes are (Tantivy, see below)
    gracia.worker = true                    # a thread that hands the changes to the subscribers
    gracia.worker.batch = 5000              # the most records in one event (default 5000)
    gracia.worker.batch_bytes = 20000000    # the most bytes of sources in one event (default 20 MB)
    gracia.worker.poll = 1                  # seconds between looks (default 1)

The section is `app:main`, like any Pyramid ini, so the same file can feed `config.include`.
Which file: `-c FILE`, else `$GRACIA_CONFIG`, else `config.ini` in the current directory when
there is one (`config_file`). `$GRACIA_CONFIG` set to nothing means no file at all (the tests use
that). A file that is named and missing is an error; the default one that is missing is just no
file. Within the settings, a flag overrides the environment, which overrides the file, which
overrides the defaults.

`gracia.jobs_dir` is where each job gets its directory (`<jobs_dir>/<uuid>/`). A relative path is
taken from the directory of the config file, like the other paths. It is for the jobs system, which
is designed and not built yet: the setting is read and resolved here so that it can be put in the
file now. The directory has to be writable by the process that owns the database (`serve-db`,
because a `COPY` runs there) and by the job processes, so in production give it explicitly.

`gracia.cache_dir` is an LMDB directory (see `gracia.cache`): the RDF/XML of a record and what it is
made of, kept until the record or anything it points at changes. Leave it out and nothing is kept.
The web server also reads `gracia.db.file` to see that the database changed: the file must be on the
machine it runs on (otherwise it asks the database at every request).

`gracia.worker` (true to start it, with `gracia.worker.batch`, `.batch_bytes` and `.poll`) runs `gracia.actions.Worker` in the web server: it takes the changes
every write queues, a batch at a time (up to 5000 records or 20 MB of sources), makes the RDF/XML of the records in the graph cache (when there is
one) and fires `gracia.actions.RecordsChanged` for what subscribed to it (a search index). The lock
that keeps two web processes off one repository's queue is a file in `gracia.cache_dir`, or next to the
database file.

`gracia.index_dir` is where the search indexes are, one directory for each repository and index
(`gracia.search`). With it set the worker's changes go into them, a batch at a time; it needs
`gracia.worker` (or the job "Process pending actions") to be fed. Leave it out and there is no search.

`gracia.db.token` (the secret itself, inline) is allowed but a `token_file` is preferred: the file
stays out of version control and is checked for mode 0600 like the one `serve-db` writes.
"""

import configparser
import stat
from pathlib import Path

SECTION = "app:main"
DEFAULT_DB = "gracia.db"


def read(path: str | Path | None) -> dict[str, str]:
    """The `gracia.*` settings of a config file (empty when there is no file)."""
    if not path:
        return {}
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"no such config file: {path}")
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
    parser.read(path, encoding="utf-8")
    if not parser.has_section(SECTION):
        raise ValueError(f"{path}: no [{SECTION}] section")
    settings = {k: v.strip() for k, v in parser.items(SECTION) if k.startswith("gracia.")}
    for key in (
        "gracia.db.file",
        "gracia.db.token_file",
        "gracia.jobs_dir",
        "gracia.cache_dir",
        "gracia.index_dir",
    ):  # from the config file
        if settings.get(key) and not Path(settings[key]).is_absolute():
            settings[key] = str(path.parent / settings[key])
    if settings.get("gracia.db") and not is_remote(settings["gracia.db"]):
        if not Path(settings["gracia.db"]).is_absolute():
            settings["gracia.db"] = str(path.parent / settings["gracia.db"])
    return settings


def is_remote(target: str | Path) -> bool:
    return str(target).startswith("quack:")


def parse_target(target: str) -> tuple[str, int]:
    """`quack:host:port` as (host, port)."""
    parts = target.split(":")
    if len(parts) != 3 or parts[0] != "quack" or not parts[1] or not parts[2].isdigit():
        raise ValueError(f"invalid target {target!r}: expected quack:host:port")
    return parts[1], int(parts[2])


def read_secret(path: str | Path, minimum: int = 16) -> str:
    """A secret from an owner-only file (mode 0600, no access for group or others)."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"no such file: {path}")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(f"{path} is readable by group or others: run `chmod 600 {path}`")
    secret = path.read_text(encoding="utf-8").strip()
    if len(secret) < minimum:
        raise ValueError(f"the secret in {path} is too short (at least {minimum} characters)")
    return secret


def read_token(settings: dict[str, str]) -> str:
    """The token for a remote database: inline, or read from the token file (which must be
    owner-only). The file is never created here: only `serve-db` creates it."""
    if settings.get("gracia.db.token"):
        return settings["gracia.db.token"]
    path = settings.get("gracia.db.token_file")
    if not path:
        raise ValueError(
            "a quack: database needs a token: set gracia.db.token_file (or gracia.db.token) "
            "in the config file"
        )
    if not Path(path).is_file():
        raise ValueError(f"no such token file: {path} (it is created by `gracia serve-db`)")
    return read_secret(path)


DEFAULT_FILE = "config.ini"


def config_file(flag=None, environ=None, cwd=None) -> Path | None:
    """The config file to read: the flag, else `$GRACIA_CONFIG` (empty: none), else
    `config.ini` in `cwd` (the current directory) if it exists, else None."""
    import os

    environ = os.environ if environ is None else environ
    if flag:
        return Path(flag)
    if "GRACIA_CONFIG" in environ:
        return Path(environ["GRACIA_CONFIG"]) if environ["GRACIA_CONFIG"] else None
    candidate = Path(cwd) / DEFAULT_FILE if cwd else Path.cwd() / DEFAULT_FILE
    return candidate if candidate.is_file() else None


def resolve(flag=None, db=None, environ=None, cwd=None) -> dict[str, str]:
    """The effective settings: the config file (see `config_file`), with the database
    overridden by `db` (a flag) or else `$GRACIA_DB`, and the default last."""
    import os

    environ = os.environ if environ is None else environ
    settings = read(config_file(flag, environ, cwd))
    chosen = db or environ.get("GRACIA_DB")
    if chosen:
        settings["gracia.db"] = str(chosen)
    settings.setdefault("gracia.db", DEFAULT_DB)
    return settings
