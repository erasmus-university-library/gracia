import math
import socket
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

import colander  # noqa: F401 - used by the data below
import duckdb
import luigi
import pyoxigraph as ox

from gracia.application import Application
from gracia.jobs import runner
from gracia.jobs.builtin import Source
from gracia.jobs.db import JobsDb
from gracia.jobs.spec import validate_spec
from gracia.jobs.stage import Stage, StageFailure, read_lines

FOAF = "http://xmlns.com/foaf/0.1/"
PREFIXES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix dash: <http://datashapes.org/dash#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
"""
SCHEMAS = {
    "Person-full-01.ttl": PREFIXES
    + """<urn:t:Full> a sh:NodeShape ; sh:targetClass foaf:Person ;
    sh:property [ sh:path foaf:name ; dash:propertyRole dash:LabelRole ;
                  sh:minCount 1 ; sh:maxCount 1 ] .""",
    # a candidate: it also wants a mail address that looks like one
    "Person-strict-01.ttl": PREFIXES
    + """<urn:t:Strict> a sh:NodeShape ; sh:targetClass foaf:Person ;
    sh:property [ sh:path foaf:name ; sh:minCount 1 ; sh:maxCount 1 ] ,
        [ sh:path foaf:mbox ; sh:minCount 1 ; sh:maxCount 1 ; sh:pattern "@" ] .""",
}
REPOSITORY = """
@prefix gracia: <https://gracia.pub/ns/> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
<rec:repository:1> a gracia:Record ;
    gracia:resource [ a gracia:Repository ; gracia:title "library" ;
        gracia:types ( gracia:Repository gracia:Type gracia:User gracia:UserGroup
          foaf:Person ) ] .
<rec:type:5> a gracia:Record ; gracia:resource_uri foaf:Person ;
    gracia:resource [ a gracia:Type ; gracia:name "Person" ; gracia:slug "person" ;
        gracia:class foaf:Person ; gracia:assets "@ASSETS@" ; gracia:schemaTemplate
          "Person-full-01.ttl" ] .
"""


def person(
    number: int, name: bool = True, mail: str | None = None, content: bool = True
) -> str:
    if not content:
        return f"<rec:person:{number}> a gracia:Record ."
    props = [f'foaf:name "Person {number}"'] if name else []
    props += [f'foaf:mbox "{mail}"'] if mail else []
    body = " ; ".join(["a foaf:Person", *props])
    return f"<rec:person:{number}> a gracia:Record ; gracia:resource [ {body} ] ."


STOP = None  # a test may put the StopFlag of the running job here


class Echo(Source):
    """`n_items` items `item-1` .. `item-n`, in batches of `size`; the parameters make
    things go wrong or slow."""

    n_items = luigi.IntParameter(default=6)
    size = luigi.IntParameter(default=2)
    sleep = luigi.FloatParameter(default=0.0)
    fail_batch = luigi.IntParameter(default=-1)  # this batch raises ...
    fail_file = luigi.Parameter(
        default=""
    )  # ... while this file exists (if one is named)
    stop_at = luigi.IntParameter(
        default=-1
    )  # this batch sets the stop flag, as a SIGTERM would
    marker = luigi.Parameter(default="")  # a file that is written when a batch starts
    hold = luigi.Parameter(default="")  # a batch waits for as long as this file exists

    def plan(self):
        return {
            "batches": math.ceil(self.n_items / self.size),
            "estimate": self.n_items,
            "unit": "items",
        }

    def process(self, items):
        if self.marker:
            with open(self.marker, "a") as handle:
                handle.write(f"{self.n}\n")
        while self.hold and Path(self.hold).exists():
            time.sleep(0.05)
        if self.sleep:
            time.sleep(self.sleep)
        if self.n == self.fail_batch and (
            not self.fail_file or Path(self.fail_file).exists()
        ):
            raise StageFailure(f"batch {self.n} was asked to fail")
        if self.n == self.stop_at and STOP is not None:
            STOP.set()
        low = self.n * self.size
        high = min(low + self.size, self.n_items)
        return [{"id": f"item-{i}", "value": i} for i in range(low + 1, high + 1)], []


def echo(**params) -> dict:
    """A stage entry for a job: `Echo` with 6 items in batches of 2, unless told
    otherwise."""
    return {"stage": "Echo", "params": {"n_items": 6, "size": 2, **params}}


class Reject(Stage):
    """Rejects the items named in `ids` (comma separated), passes the rest."""

    ids = luigi.Parameter(default="")

    def process(self, items):
        names = set(filter(None, self.ids.split(",")))
        passed = [i for i in items if i["id"] not in names]
        rejected = [
            {"id": i["id"], "reason": "unwanted", "message": "it is not wanted"}
            for i in items
            if i["id"] in names
        ]
        return passed, rejected


class Keep(Stage):
    """Passes everything on unchanged: the output is a hard link of the input."""

    def process(self, items):
        return items, []


class Mark(Stage):
    """A transform: every item gets a mark (the data changes, so the file is
    written)."""

    def process(self, items):
        return [{**i, "marked": True} for i in items], []


class Tool(Stage):
    """Stands in for a call to a tool or a web service that fails the first `failures`
    times."""

    failures = luigi.IntParameter(default=0)
    counter = luigi.Parameter(default="")

    def process(self, items):
        path = Path(self.counter)
        done = int(path.read_text()) if path.exists() else 0
        if done < self.failures:
            path.write_text(str(done + 1))
            raise RuntimeError("the tool failed")
        return items, []


class JobsCase(unittest.TestCase):
    """An application with a repository `library` that has a Person type and some
    people."""

    def setUp(self):
        self.application = self.make_application()
        self.addCleanup(self.application.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.jobs_dir = self.tmp / "jobs"
        shacl = self.tmp / "person" / "shacl"
        shacl.mkdir(parents=True)
        for name, text in SCHEMAS.items():
            (shacl / name).write_text(text)
        self.application.create_repository("library")
        self.put(REPOSITORY.replace("@ASSETS@", f"{self.tmp / 'person'}/"))
        self.put(self.people())

    def make_application(self) -> Application:
        return Application(":memory:")

    settings: ClassVar[dict] = {}  # what `gracia run` would have: a test may set more

    def people(self) -> str:
        """12 people: 3 have no name, 4 have no mail address, 1 has a bad one, 1 has no
        content."""
        header = (
            "@prefix gracia: <https://gracia.pub/ns/> . @prefix foaf: <"
            + FOAF
            + "> .\n"
        )
        lines = [header]
        for n in range(1, 11):
            lines.append(
                person(
                    n, name=n % 4 != 0, mail=None if n % 3 == 0 else f"p{n}@example.org"
                )
            )
        lines.append(person(11, mail="not-a-mail-address"))
        lines.append(person(12, content=False))
        return "\n".join(lines)

    def put(self, text: str) -> None:
        header = (
            ""
            if text.lstrip().startswith("@prefix")
            else "@prefix gracia: <https://gracia.pub/ns/> .\n"
        )
        with self.application.open_repository("library") as store:
            store.put(
                ox.Dataset(
                    ox.parse((header + text).encode(), format=ox.RdfFormat.TURTLE)
                )
            )

    # ---- jobs
    # ---------------------------------------------------------------------------

    def make(self, *stages, **extra) -> str:
        spec = validate_spec({"repo": "library", "stages": list(stages), **extra})
        with JobsDb(self.application) as db:
            return db.create(spec)

    def run_job(self, job_id: str, stop=None) -> int:
        return runner.run_job(
            self.application, job_id, self.jobs_dir, self.settings, stop
        )

    def job(self, job_id: str) -> dict:
        with JobsDb(self.application) as db:
            return db.get(job_id)

    def stage(self, job_id: str, name: str) -> dict:
        """The snapshot of one stage, from the job row."""
        return next(
            s for s in self.job(job_id)["progress"]["stages"] if s["name"] == name
        )

    def lines(self, job_id: str, name: str) -> list[dict]:
        return read_lines(self.jobs_dir / job_id / name)


def quack_owner(test: unittest.TestCase) -> tuple[Application, str, str]:
    """An application that reaches an in-memory database through a Quack owner: with the
    address and the token of that owner."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    uri, token = f"quack:localhost:{port}", "0123456789abcdef0123456789"
    owner = duckdb.connect(":memory:")
    owner.execute("INSTALL quack; LOAD quack")
    owner.execute(f"CALL quack_serve('{uri}', token = '{token}')")
    test.addCleanup(owner.close)
    test.addCleanup(lambda: owner.execute(f"CALL quack_stop('{uri}')"))
    app = Application(uri, token=token)
    test.addCleanup(app.close)
    return app, uri, token
