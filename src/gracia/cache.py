"""What a request would otherwise ask the database again and again: the etag of a record, and the
RDF/XML of the record and everything it is made of. Both are filled lazily, by the request that
needs them, and neither has to be kept in step by anything but `Cache.check`.

Knowing that something changed. Every write in a repository upserts a row in
`main.repositories_last_modified_at` (the triggers on `triples`, so a write that bypasses the store
counts too). `Cache.check` reads that table, but only when the database files changed: DuckDB
appends a commit to `<db>.wal` and rewrites `<db>` itself only at a checkpoint, so the signature is
the modification time and size of both, taken *before* the table is read (a commit that lands in
between then shows at the next check). Without a file to watch the table is read every time.

The etags are a dict per repository, one generation at a time: a repository that changed gets a
new, empty generation and the old one is dropped (nothing is refreshed, the requests fill it again).
A request takes the generation before it reads the database and writes only into that one, so a
request that was still running when the repository changed cannot put what it read into the new
generation. One process, one cache: several processes check for themselves.

The graph of a record is the N-Triples of the record and of the records it is made of
(`Store.closure_source`), written as RDF/XML with the blank node labels of the sources (`b<record
id>_<n>`: the same for a record wherever it is, so graphs can be merged) and kept in LMDB under the
record, with the etag it was made at. An entry is used only when that etag is the current one, so
nothing has to be invalidated: a stale entry is never matched, and the next write of the record
replaces it.

The doc of a record (`gracia.docs`: the JSON made of its graph by the doc stylesheets) is kept in a second LMDB
directory (`<graph dir>/docs`) with the etag of the graph and the fingerprint of the stylesheets it was made
with. The compiled doc stylesheet of a repository is kept with its generation: SaxonC resolves the imports
when it compiles, so it must not outlive a change of the repository (a type with another `docTemplate` is a
change of it), and it is dropped, with the etags, when the repository is written. A compile after a write
costs a little and only happens when a doc is asked for.
"""

import json
import logging
import os
import threading
from pathlib import Path

import duckdb
import lmdb
import pyoxigraph as ox

from gracia.utils import xslt

log = logging.getLogger(__name__)

GRAPH_CACHE_BYTES = 2 * 1024**3  # the most LMDB maps; the file only grows as far as it is filled


class GraphStore:
    """RDF/XML by repository and record id, each with the etag it was made at, in an LMDB
    directory (many processes may read it, one writes at a time)."""

    def __init__(self, path: str | Path, size: int = GRAPH_CACHE_BYTES):
        Path(path).mkdir(parents=True, exist_ok=True)
        # metasync off: a crash may lose the last write, never corrupt the file; it is only a cache
        self._path = Path(path)
        self._env = lmdb.open(str(path), map_size=size, metasync=False)

    @staticmethod
    def _key(repo: str, record_id: int) -> bytes:
        return repo.encode() + b"\0" + record_id.to_bytes(4, "big")

    def get(self, repo: str, record_id: int, etag: str) -> bytes | None:
        """The RDF/XML of the record if it was made at `etag`."""
        with self._env.begin() as txn:
            value = txn.get(self._key(repo, record_id))
            if value is not None and value[:16] == etag.encode():
                return bytes(value[16:])
        return None

    def put(self, repo: str, record_id: int, etag: str, xml: bytes) -> None:
        """Keep the RDF/XML of one record (see `put_many`)."""
        self.put_many([(repo, record_id, etag, xml)])

    def put_many(self, entries) -> None:
        """Keep the RDF/XML of many records, `(repo, record_id, etag, xml)` each, in one transaction
        (a transaction of its own for each took 0.45 ms a record, nearly all of it the sync to disk).
        When the map is full everything is dropped and the write is made again: a cache that is
        full is a cache that starts over. (LMDB reuses the pages of a transaction only from the one
        after next, so a second drop may be needed.)"""
        entries = list(entries)
        for _ in range(3):
            try:
                with self._env.begin(write=True) as txn:
                    for repo, record_id, etag, xml in entries:
                        txn.put(self._key(repo, record_id), etag.encode() + xml)
                return
            except lmdb.MapFullError:
                with self._env.begin(write=True) as txn:
                    txn.drop(self._env.open_db(None, txn=txn), delete=False)
        raise ValueError(f"{len(entries)} graphs do not fit the graph cache")

    def count(self, repo: str, limit: int = 100_000) -> int:
        """How many entries a repository has, counting no further than `limit` (the keys are read, so
        counting a million of them would take a while)."""
        prefix = repo.encode() + b"\0"
        found = 0
        with self._env.begin() as txn:
            cursor = txn.cursor()
            if cursor.set_range(prefix):
                for key in cursor.iternext(values=False):
                    if not key.startswith(prefix) or found >= limit:
                        break
                    found += 1
        return found

    def stats(self) -> dict:
        """Entries of all repositories, the bytes the pages that hold them take, the size of the file
        and the most the map may grow to."""
        with self._env.begin() as txn:
            stat = txn.stat()
        pages = stat["branch_pages"] + stat["leaf_pages"] + stat["overflow_pages"]
        data = self._path / "data.mdb"
        return {
            "entries": stat["entries"],
            "bytes": pages * stat["psize"],
            "file": data.stat().st_size if data.exists() else 0,
            "limit": self._env.info()["map_size"],
        }

    def delete_many(self, repo: str, record_ids) -> None:
        """Forget the RDF/XML of records (they are gone: nothing would match an entry of theirs, but it
        would stay in the file)."""
        with self._env.begin(write=True) as txn:
            for record_id in record_ids:
                txn.delete(self._key(repo, record_id))

    def close(self) -> None:
        self._env.close()


class DocStore(GraphStore):
    """Docs (JSON) by repository and record id, each with the etag of the graph it was made from and the
    fingerprint of the stylesheets that made it: the entry is used only for both. The same file layout and
    the same behaviour when the map is full as `GraphStore`."""

    def get(self, repo: str, record_id: int, etag: str, fingerprint: str = "") -> bytes | None:
        tag = (etag + fingerprint).encode()
        with self._env.begin() as txn:
            value = txn.get(self._key(repo, record_id))
            if value is not None and value[: len(tag)] == tag:
                return bytes(value[len(tag) :])
        return None


def dataset_rdfxml(dataset: ox.Dataset) -> bytes:
    """A dataset (the default graph) as RDF/XML, the blank nodes as they are (the sources name them
    `b<record id>_<n>`, valid `rdf:nodeID`s that are the same wherever the record is). The order of
    the elements is not fixed. The result is not checked: the writer does not refuse a predicate that
    ends in a digit or a `/` (it has no XML name) or a control character in a literal, and such a
    graph is not well-formed XML."""
    return ox.serialize(dataset, format=ox.RdfFormat.RDF_XML)


def rdfxml(ntriples: str) -> bytes:
    """N-Triples as RDF/XML (see `dataset_rdfxml`)."""
    return dataset_rdfxml(ox.Dataset(ox.parse(ntriples.encode(), format=ox.RdfFormat.N_TRIPLES)))


class Cache:
    """The etag and graph caches of one `Application`."""

    def __init__(self, application, *, watch=None, graph_dir=None):
        """`watch` is the database file to stat (None: read the table at every check), `graph_dir`
        the LMDB directory of the graphs (None: graphs are made again every time)."""
        self._application = application
        self._watch = [Path(watch), Path(f"{watch}.wal")] if watch else []
        self._seen = None  # the signature of the files when the table was last read
        self._generations: dict[str, tuple[object, dict]] = {}  # repo: (its time in the table, etags)
        self._graph_dir = graph_dir
        self._graphs: GraphStore | None = None  # opened by the first use
        self._docs: DocStore | None = None  # likewise
        self._compiled: dict[str, xslt.Compiled] = {}  # repo: its doc stylesheet, of the generation it is in
        self._compiled_lock = threading.RLock()  # compiling asks the database, which may look at the cache
        self.worker = None  # what follows the writes (`gracia.actions.Worker`): kept running by `check`

    @property
    def keeps_graphs(self) -> bool:
        """Whether there is a graph cache to keep (a graph directory is set)."""
        return bool(self._graph_dir)

    def signature(self) -> tuple | None:
        """What the files of the database look like now (see `check`), or None when there are none
        to watch: whoever compares signatures has to look every time then."""
        return self._signature() if self._watch else None
    def moments(self) -> dict[str, object]:
        """When each repository was last written, by its id (`main.repositories_last_modified_at`: the
        triggers keep it, so a write outside the store counts too). One query."""
        connection = self._application.raw()
        try:
            return dict(
                connection.execute(
                    "SELECT repo_id, last_modified_at FROM main.repositories_last_modified_at"
                ).fetchall()
            )
        except duckdb.CatalogException:  # nothing has been written yet
            return {}
        finally:
            connection.close()

    def _doc_store(self) -> DocStore | None:
        if self._docs is None and self._graph_dir:
            self._docs = DocStore(Path(self._graph_dir) / "docs")
        return self._docs

    def _graph_store(self) -> GraphStore | None:
        if self._graphs is None and self._graph_dir:
            self._graphs = GraphStore(self._graph_dir)
        return self._graphs

    def _signature(self) -> tuple:
        signature = []
        for path in self._watch:
            try:
                found = os.stat(path)
                signature.append((found.st_mtime_ns, found.st_size))
            except FileNotFoundError:  # no WAL: nothing since the last checkpoint
                signature.append(None)
        return tuple(signature)

    def check(self) -> None:
        """Start a new generation of the etags of every repository that was written since the last
        check. Cheap when the files did not change (two `stat`s). It also sees to it that the worker
        that follows the writes is still running, and starts it again if it is not."""
        if self.worker is not None:
            self.worker.ensure_running()
        signature = self._signature() if self._watch else None
        if self._watch and signature == self._seen:
            return
        rows = self.moments()
        for repo, moment in rows.items():
            generation = self._generations.get(repo)
            if generation is None or generation[0] != moment:
                self._generations[repo] = (moment, {})
                self._drop_compiled(repo)
        self._seen = signature

    def etags(self, repo: str, record_ids) -> dict[int, tuple[str, tuple[str, ...]] | None]:
        """For each record id, `(etag as 16 hex digits, slugs of the types its page is made of)`, or
        None when the record has no etag. One query for all that are not cached yet."""
        self.check()
        _, generation = self._generations.setdefault(repo, (None, {}))  # this one, whatever happens next
        missing = [i for i in dict.fromkeys(record_ids) if i not in generation]
        if missing:
            listed = ", ".join(str(int(i)) for i in missing)
            with self._application.open_repository(repo) as store:
                rows = store.con.execute(
                    f"SELECT record_id, etag, deep_types FROM etags WHERE record_id IN ({listed})"
                ).fetchall()
            found = {r[0]: (f"{r[1]:016x}", tuple(r[2])) for r in rows}
            for i in missing:
                generation[i] = found.get(i)
        return {i: generation[i] for i in record_ids}

    def _drop_compiled(self, repo: str) -> None:
        """Let go of the compiled doc stylesheet of a repository (the next doc compiles it again)."""
        with self._compiled_lock:
            old = self._compiled.pop(repo, None)
        if old is not None:
            old.close()  # waits for a run that is going on; one that has not started gets a fresh one (`docs_made`)

    def compiled(self, repo: str) -> xslt.Compiled:
        """The doc stylesheet of a repository, compiled once for as long as the repository stays as it is
        (see the module). The registry of the application has to be the current one: a request has put
        it there, `gracia.actions` does it for the worker."""
        self.check()
        with self._compiled_lock:
            found = self._compiled.get(repo)
            if found is not None:
                return found
            from gracia import docs
            from gracia.resources import Repository

            repository = Repository(repo, self._application)
            try:
                found = self._compiled[repo] = docs.compile(repository)
            finally:
                repository.close()
            return found

    def docs_made(self, repo: str, ids: dict[str, int], rdfxml: str) -> tuple[dict[str, dict], str]:
        """`gracia.docs.make` with the doc stylesheet of the repository: `(the docs by record uri, the
        fingerprint of the stylesheets)`. A stylesheet that was dropped while it was waited for is made
        again once."""
        from gracia import docs

        for attempt in (1, 2):
            compiled = self.compiled(repo)
            try:
                return docs.make(compiled, ids, rdfxml), compiled.fingerprint
            except xslt.XsltError:
                if attempt == 2 or not compiled.closed:
                    raise
        raise AssertionError("unreachable")

    def schema_made(self, repo: str, slugs: list[str]) -> list[dict]:
        """`gracia.docs.schema` with the doc stylesheet of the repository."""
        from gracia import docs

        for attempt in (1, 2):
            compiled = self.compiled(repo)
            try:
                return docs.schema(compiled, slugs)
            except xslt.XsltError:
                if attempt == 2 or not compiled.closed:
                    raise
        raise AssertionError("unreachable")

    @property
    def keeps_docs(self) -> bool:
        """Whether there is a doc cache to keep (a graph directory is set)."""
        return bool(self._graph_dir)

    def doc(self, repo: str, record_id: int, record_uri: str) -> dict | None:
        """The doc of a record, or None when the record has no etag. Kept, or made from the graph and
        kept (when there is a directory for it). A doc that cannot be made is `{"record", "error"}`,
        and is not kept."""
        current = self.etags(repo, [record_id])[record_id]
        if current is None:
            return None
        store = self._doc_store()
        if store is not None:
            fingerprint = self.compiled(repo).fingerprint
            if (kept := store.get(repo, record_id, current[0], fingerprint)) is not None:
                return json.loads(kept)
        xml = self.graph(repo, record_id)
        if xml is None:
            return None
        made, fingerprint = self.docs_made(repo, {record_uri: record_id}, xml.decode())
        doc = made[record_uri]
        if store is not None and "error" not in doc:
            store.put_many([(repo, record_id, current[0] + fingerprint, json.dumps(doc).encode())])
        return doc

    def keep_docs(self, repo: str, fingerprint: str, entries) -> None:
        """Put docs in the doc cache, `(record_id, etag, doc)` each, in one transaction."""
        store = self._doc_store()
        if store is None:
            raise ValueError("there is no doc cache: set gracia.cache_dir")
        store.put_many((repo, i, etag + fingerprint, json.dumps(doc).encode()) for i, etag, doc in entries)

    def graph(self, repo: str, record_id: int) -> bytes | None:
        """The RDF/XML of a record and everything its page is made of, or None when the record has no
        etag. Made once per etag."""
        current = self.etags(repo, [record_id])[record_id]
        if current is None:
            return None
        graphs = self._graph_store()
        if graphs is not None and (kept := graphs.get(repo, record_id, current[0])):
            return kept
        with self._application.open_repository(repo) as store:
            made = store.closure_source(record_id)
        if made is None:
            return None
        etag, text = made  # the etag of this text, which may be newer than `current`
        xml = rdfxml(text)
        if graphs is not None:
            graphs.put(repo, record_id, etag, xml)
        return xml

    def keep(self, repo: str, entries) -> None:
        """Put the RDF/XML of records in the graph cache, `(record_id, etag, xml)` each, in one
        transaction (what `graph` does for itself; the job that makes them all again does it for a
        batch at a time). `ValueError` when there is no graph directory."""
        graphs = self._graph_store()
        if graphs is None:
            raise ValueError("there is no graph cache: set gracia.cache_dir")
        graphs.put_many((repo, record_id, etag, xml) for record_id, etag, xml in entries)

    def etags_held(self, repo: str) -> int:
        """How many etags of a repository the cache holds in memory now (its current generation)."""
        found = self._generations.get(repo)
        return len(found[1]) if found else 0

    def graph_stats(self, repo: str) -> dict | None:
        """What the graph cache holds, or None when there is none (no graph directory): `GraphStore.stats`
        and the entries of `repo`, counted up to 100,000."""
        graphs = self._graph_store()
        if graphs is None:
            return None
        return {**graphs.stats(), "repo_entries": graphs.count(repo), "directory": str(self._graph_dir)}

    def forget(self, repo: str, record_ids) -> None:
        """Take the graphs of records that are gone out of the graph cache."""
        graphs = self._graph_store()
        if graphs is not None:
            graphs.delete_many(repo, record_ids)
        docs = self._doc_store()
        if docs is not None:
            docs.delete_many(repo, record_ids)

    def close(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        for repo in list(self._compiled):
            self._drop_compiled(repo)
        if self._docs is not None:
            self._docs.close()
            self._docs = None
        if self._graphs is not None:
            self._graphs.close()
            self._graphs = None
