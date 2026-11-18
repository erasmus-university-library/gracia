"""The tests never read a `config.ini` that happens to be in the directory they run from: an
empty `$GRACIA_CONFIG` means no config file (see `gracia.config.config_file`). Tests that want a
file name it with `-c` or set the variable themselves."""

import os
import sys

os.environ["GRACIA_CONFIG"] = ""

# The test data is tiny, and every pytest-xdist worker is a process with its own DuckDB, which by default
# uses one thread per core: with eight workers on 32 cores that is 256 threads fighting over them. Two
# threads each is enough for the data these tests use (set GRACIA_TEST_DUCKDB_THREADS to try another number).
import duckdb  # noqa: E402

_connect = duckdb.connect


def _connect_with_few_threads(*args, **kwargs):
    config = dict(kwargs.pop("config", None) or {})
    config.setdefault("threads", int(os.environ.get("GRACIA_TEST_DUCKDB_THREADS", "2")))
    return _connect(*args, config=config, **kwargs)


duckdb.connect = _connect_with_few_threads


# Making a repository is 127 statements of DDL, triggers and first records: 200 to 300 ms, and 590 times in the
# suite, about a quarter of all the time it takes. So the first repository of a kind made in a process is made
# the normal way into a template file, and every later one is a copy of that file. A copy of the file keeps the
# triggers, the macros and the data (COPY FROM DATABASE into memory does not keep the triggers: the records, the
# history and the etags then stop following the triples, and nothing says so). Only the first repository of an
# application on a database in memory is cloned; a test that names a database file, or makes a second
# repository, gets the real thing.
import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from gracia import uri as _uri  # noqa: E402
from gracia.application import Application  # noqa: E402

_TEMPLATES = Path(tempfile.mkdtemp(prefix="gracia_test_repositories_"))
atexit.register(shutil.rmtree, _TEMPLATES, ignore_errors=True)
_templates: dict[tuple, Path] = {}
_clones = iter(range(1, 10**9))
_real_init, _real_close, _real_create = Application.__init__, Application.close, Application.create_repository


def _init(self, path=":memory:", **kwargs):
    _real_init(self, path, **kwargs)
    self._in_memory = str(path) == ":memory:"
    self._clone: Path | None = None


def _close(self):
    _real_close(self)
    if self._clone is not None:
        for suffix in ("", ".wal"):
            Path(str(self._clone) + suffix).unlink(missing_ok=True)
        self._clone = None


def _swap_in(application, template: Path) -> None:
    """The application holds a copy of the database in the template file."""
    clone = _TEMPLATES / f"clone-{next(_clones)}.db"
    shutil.copy(template, clone)
    application._con.close()
    application._con = duckdb.connect(str(clone))  # the connection of a test with a database file, with the data in it
    application._clone = clone


def _clonable(application) -> bool:
    return not application.remote and application._in_memory and application._clone is None and not application.list_repositories()


def prepared(application, key, build) -> None:
    """Make `application` (on a database in memory, with nothing in it) hold what `build(app)` makes of an
    application: built once in a process, into a template file, and copied for every later test. For the
    state that a whole group of tests starts from (a repository with some records in it): a write is 0.1 s
    at any size, and a test that starts with two of them spends a quarter of a second before it begins.
    `key` says which state it is; anything else (a database in a file, a second repository) just runs `build`."""
    if not _clonable(application):
        build(application)
        return
    template = _templates.get(("prepared", key))
    if template is None:
        template = _TEMPLATES / f"template-{len(_templates)}.db"
        built = Application(template)
        try:
            build(built)
        finally:
            built.close()
        _templates[("prepared", key)] = template
    _swap_in(application, template)


def _create_repository(self, repo_id, admin=None, content_types=(), ark=(_uri.DEFAULT_NAAN, _uri.DEFAULT_ROOT_PREFIX)):
    content_types = list(content_types)
    if not _clonable(self):
        return _real_create(self, repo_id, admin, content_types, ark)
    key = (
        repo_id,
        tuple(admin) if admin else None,
        tuple((getattr(c, "slug", None), getattr(c, "assets", None), getattr(c, "schema", None)) for c in content_types),
        tuple(ark),
    )
    template = _templates.get(key)
    if template is None:  # the normal way, once, into a file that is then only copied
        template = _TEMPLATES / f"template-{len(_templates)}.db"
        built = Application(template)
        try:
            if not _real_create(built, repo_id, admin, content_types, ark):
                return _real_create(self, repo_id, admin, content_types, ark)
        finally:
            built.close()
        _templates[key] = template
    _swap_in(self, template)
    return True


Application.__init__, Application.close, Application.create_repository = _init, _close, _create_repository


# HTTP Basic checks the password on every request, with scrypt at a cost of about 25 ms: a test that makes ten
# requests as the admin spent a quarter of a second on it, which was most of the time of a request (2.7 ms
# without credentials, 26 ms with). A hash carries its own parameters (`scrypt$N$r$p$...`) and a check reads
# them from the stored string, so the tests hash with the cheapest cost and what is verified is unchanged.
from gracia.utils import passwords  # noqa: E402

passwords.N = 2**4


# The ids of the records are made of the number of their type (`record_id`), and a record of a type that has no
# number cannot be stored. A repository is told its types by its provider (`Store.configure`); a plain `Store` (most
# of the store tests, the command line) has only the four a repository starts with, and a test may write a record
# of a type that its repository does not have. So when a write starts (`Store._configured`, before its transaction),
# the types in the rows it staged that the database has no number for are given the next free one (5 and up: 1 to
# 4 are the types a repository starts with), except the types that are not there on purpose.
from gracia.store import Store, literal  # noqa: E402

NO_NUMBER = {"nothing", "ghost", "nobody"}
_real_configured = Store._configured


def _configured(self):
    hooks = _real_configured(self)  # the types of the repository first: their numbers are the ones that count
    staged = self.con.execute(  # (a statement that fails would abort a transaction that is open)
        f"SELECT count(*) FROM duckdb_tables() WHERE table_name = {literal(self.staging)}"
    ).fetchone()[0]
    if not staged:
        return hooks
    found = self.con.execute(  # the types of the subjects, and of the records that the test data points at
        f"SELECT DISTINCT regexp_extract(x, '^rec:([a-z][a-z0-9-]*):', 1) FROM "
        f"(SELECT s AS x FROM {self.staging} UNION SELECT o FROM {self.staging}) "
        "WHERE regexp_matches(x, '^rec:[a-z][a-z0-9-]*:')"
    ).fetchall()
    known = self.type_numbers()
    missing = sorted(slug for (slug,) in found if slug not in known and slug not in NO_NUMBER)
    if missing:
        free = iter(n for n in range(5, 100) if n not in known.values())
        self.set_type_ids([(slug, next(free)) for slug in missing])
    return hooks


Store._configured = _configured
