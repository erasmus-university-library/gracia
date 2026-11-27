import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from harness import open_store

from gracia import cli
from gracia.graph import streaming_parse
from gracia.store import Store

# the subjects of the test data: only records (rec:<type>:<n>) are stored as records
REC = {
    "alice": "rec:person:1",
    "bob": "rec:person:2",
    "carol": "rec:person:3",
    "book1": "rec:book:1",
    "book2": "rec:book:2",
    "book": "rec:book:3",
    "doc1": "rec:doc:1",
    "doc2": "rec:doc:2",
    "doc3": "rec:doc:3",
    "doc4": "rec:doc:4",
    "a": "rec:thing:1",
    "b": "rec:thing:2",
    "c": "rec:thing:3",
    "new1": "rec:new:1",
    "new2": "rec:new:2",
}


DATA = Path(__file__).resolve().parent.parent / "data"


def rec(name: str) -> str:
    if name in REC:
        return REC[name]
    found = re.fullmatch(r"([a-z]+)([1-9][0-9]*)", name)
    if found is None:
        raise KeyError(name)
    return f"rec:{found[1]}:{found[2]}"


# the numbered files are the test data; others in `data/` (the ontology) are not
RDF_FILES = sorted(
    [
        *DATA.glob("[0-9][0-9]_*.ttl"),
        *DATA.glob("[0-9][0-9]_*.trig"),
        *DATA.glob("[0-9][0-9]_*.json"),
    ]
)


class DbTestCase(unittest.TestCase):
    with_record = (
        False  # True: the default repository `main` is made with its repository record
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "t.db"
        if self.with_record:  # it keeps the prefixes that imported files declare
            code, _, err = self.create_repositories("main")
            self.assertEqual(code, 0, err)

    def gracia(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["-d", str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def create_repositories(self, *ids: str) -> tuple[int, str, str]:
        password = self.tmp / "admin.pw"
        if not password.exists():
            password.write_text("s3cret-pass\n")
            password.chmod(0o600)
        return self.gracia(
            "repo",
            "create",
            *ids,
            "--admin",
            "admin",
            "--admin-password-file",
            str(password),
        )

    def load(self, *files: Path) -> None:
        code, _, err = self.gracia("import", *map(str, files))
        self.assertEqual(code, 0, err)

    def rows(self, store: Store, uris=None) -> list[tuple]:
        path = self.tmp / "rows.nt"
        store.export_ntriples(path, uris)
        return list(streaming_parse(path, "ntriples"))

    def export_json(self, uri: str | None = None) -> str:
        with open_store(self.db) as store:
            return store.export_json(uri)

    def write(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(
            "@prefix ex: <http://example.org/> .\n" + body, encoding="utf-8"
        )
        return path
