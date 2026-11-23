import json
import logging
import os
import threading
from pathlib import Path
from xml.sax.saxutils import quoteattr

import duckdb
import lmdb
import pyoxigraph as ox

from gracia.graph import serialize_bytes
from gracia.store import new_store
from gracia.uri import XSL_NS
from gracia.utils import assets, xslt

log = logging.getLogger(__name__)

GRAPH_CACHE_BYTES = (
    2 * 1024**3
)  # the most LMDB maps; the file only grows as far as it is filled


class GraphStore:
    def __init__(self, path: str | Path, size: int = GRAPH_CACHE_BYTES):
        Path(path).mkdir(parents=True, exist_ok=True)
        # metasync off: a crash may lose the last write, never corrupt the file; it is
        # only a cache
        self._path = Path(path)
        self._env = lmdb.open(str(path), map_size=size, metasync=False)

    @staticmethod
    def _key(repo: str, record_id: int) -> bytes:
        return repo.encode() + b"\0" + record_id.to_bytes(4, "big")

    def get(self, repo: str, record_id: int, etag: str) -> bytes | None:
        with self._env.begin() as txn:
            value = txn.get(self._key(repo, record_id))
            if value is not None and value[:16] == etag.encode():
                return bytes(value[16:])
        return None

    def put(self, repo: str, record_id: int, etag: str, xml: bytes) -> None:
        self.put_many([(repo, record_id, etag, xml)])

    def put_many(self, entries) -> None:
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
        with self._env.begin(write=True) as txn:
            for record_id in record_ids:
                txn.delete(self._key(repo, record_id))

    def close(self) -> None:
        self._env.close()


class DocStore(GraphStore):
    BUNDLED_BASE = "gracia.types:type/doc/doc-base-01.xsl"

    def __init__(self, cache: "Cache", path: str | Path | None = None):
        self._cache = cache
        self._compiled: dict[str, xslt.Compiled] = {}  # repo: its doc stylesheet
        self._lock = (
            threading.RLock()
        )  # compiling asks the database, which may look at the cache
        if path is not None:
            super().__init__(path)
        else:
            self._env = None

    @property
    def keeps(self) -> bool:
        return self._env is not None

    def get(
        self, repo: str, record_id: int, etag: str, fingerprint: str = ""
    ) -> bytes | None:
        if self._env is None:
            return None
        tag = (etag + fingerprint).encode()
        with self._env.begin() as txn:
            value = txn.get(self._key(repo, record_id))
            if value is not None and value[: len(tag)] == tag:
                return bytes(value[len(tag) :])
        return None

    def compiled(self, repository) -> xslt.Compiled:
        # a repository that was written drops its stylesheet
        self._cache.check()
        with self._lock:
            found = self._compiled.get(repository.__name__)
            if found is None:
                found = self._compiled[repository.__name__] = self._compile(repository)
            return found

    def drop(self, repo: str) -> None:
        with self._lock:
            old = self._compiled.pop(repo, None)
        if old is not None:
            # waits for a run that is going on; one that has not started gets a fresh
            # one (`_run`)
            old.close()

    def _compile(self, repository) -> xslt.Compiled:

        def resolve(href: str):
            try:
                return repository.resolve_type(href)
            except LookupError:
                if (
                    href == assets.DOC_BASE
                ):  # a bare repository, made by a script, has no type `type`
                    return assets.resolve(self.BUNDLED_BASE)
                raise

        addresses = [assets.DOC_BASE]
        for slug, res_type in sorted(repository.res_types.items()):
            if not res_type.doc_template:
                continue
            try:
                href = assets.doc_href(slug, res_type.doc_template)
                resolve(href)
            except (ValueError, LookupError, FileNotFoundError) as err:
                log.warning(
                    "%s: the doc template of %s is not used: %s",
                    repository.__name__,
                    slug,
                    err,
                )
                continue
            addresses.append(href)
        imports = "".join(f'<xsl:import href="{href}"/>' for href in addresses)
        return xslt.Compiled(
            '<xsl:stylesheet version="3.0" '
            f'xmlns:xsl="{XSL_NS}">{imports}</xsl:stylesheet>',
            resolve,
        )

    def _run(
        self, repository, params: dict[str, str], xml: str
    ) -> tuple[str, xslt.Compiled]:
        for attempt in (1, 2):
            compiled = self.compiled(repository)
            try:
                return compiled.transform(xml, params), compiled
            except xslt.XsltError:
                if attempt == 2 or not compiled.closed:
                    raise
        raise AssertionError("unreachable")

    def bulk_update(
        self,
        repository,
        ids: dict[str, int],
        rdfxml: str,
        etags: dict[int, str] | None = None,
    ) -> dict[str, dict]:
        if not ids:
            return {}
        out, compiled = self._run(repository, {"records": json.dumps(ids)}, rdfxml)
        made = {doc["record"]: doc for doc in json.loads(out)}
        entries = []
        for uri, doc in made.items():
            if "error" in doc:
                log.warning(
                    "%s: no doc for %s: %s", repository.__name__, uri, doc["error"]
                )
            elif etags is not None and uri in ids:
                entries.append(
                    (
                        repository.__name__,
                        ids[uri],
                        etags[ids[uri]] + compiled.fingerprint,
                        json.dumps(doc).encode(),
                    )
                )
        if entries and self._env is not None:
            self.put_many(entries)
        return made

    def schema(self, repository, slugs: list[str]) -> list[dict]:
        types = "".join(f"<type slug={quoteattr(slug)}/>" for slug in slugs)
        return json.loads(
            self._run(repository, {"schema": "true"}, f"<types>{types}</types>")[0]
        )

    def close(self) -> None:
        for repo in list(self._compiled):
            self.drop(repo)
        if self._env is not None:
            super().close()
            self._env = None


def dataset_rdfxml(dataset: ox.Dataset) -> bytes:
    return serialize_bytes(dataset, format=ox.RdfFormat.RDF_XML)


def rdfxml(ntriples: str) -> bytes:
    return dataset_rdfxml(
        ox.Dataset(ox.parse(ntriples.encode(), format=ox.RdfFormat.N_TRIPLES))
    )


class Cache:
    KEPT = ("etags", "slugs", "types")

    def __init__(self, database, registry, *, db_file=None, graph_dir=None):
        self._database = database
        self.registry = registry
        self._files = [Path(db_file), Path(f"{db_file}.wal")] if db_file else []
        self._seen = None  # the signature of the files when the table was last read
        self._written: dict[str, object] = {}  # when each repository was written, then
        self._graph_dir = graph_dir
        self._graphs: GraphStore | None = None  # opened by the first use
        self._docs: DocStore | None = None  # likewise
        # what follows the writes (`gracia.actions.Worker`): kept running by `check`
        self.worker = None

    @property
    def keeps_graphs(self) -> bool:
        return bool(self._graph_dir)

    @property
    def docs(self) -> DocStore:
        if self._docs is None:
            self._docs = DocStore(
                self,
                Path(self._graph_dir) / "docs" if self._graph_dir else None,
            )
        return self._docs

    def _graph_store(self) -> GraphStore | None:
        if self._graphs is None and self._graph_dir:
            self._graphs = GraphStore(self._graph_dir)
        return self._graphs

    def file_signature(self) -> tuple | None:
        if not self._files:
            return None
        signature = []
        for path in self._files:
            try:
                found = os.stat(path)
                signature.append((found.st_mtime_ns, found.st_size))
            except FileNotFoundError:  # no WAL: nothing since the last checkpoint
                signature.append(None)
        return tuple(signature)

    def last_written(self) -> dict[str, object]:
        connection = self._database.cursor()
        try:
            return dict(
                connection.execute(
                    "SELECT repo_id, last_modified_at FROM "
                    "main.repositories_last_modified_at"
                ).fetchall()
            )
        except duckdb.CatalogException:  # nothing has been written yet
            return {}
        finally:
            connection.close()

    def check(self) -> None:
        if self.worker is not None:
            self.worker.ensure_running()
        self._refresh()

    def _refresh(self) -> None:
        signature = self.file_signature()
        if signature is not None and signature == self._seen:
            return
        # read after the signature: a write in between shows the next time
        written = self.last_written()
        for repo, moment in written.items():
            if self._written.get(repo) != moment:
                self.invalidate(repo)
        self._written, self._seen = written, signature

    def written(self) -> dict[str, object]:
        self._refresh()
        return dict(self._written)

    def invalidate(self, repo: str) -> None:
        for name in self.KEPT:
            self._shared(name).pop(repo, None)
        self.docs.drop(repo)

    def _table(self, repo: str, name: str) -> dict:
        return self._shared(name).setdefault(repo, {})

    def _shared(self, name: str) -> dict:
        return self.registry.setdefault(name, {})

    def _kept(self, repo: str, name: str, key, load):
        self._refresh()
        table = self._table(repo, name)
        if key in table:
            return table[key]
        value = load()
        if value is not None:
            table[key] = value
        return value

    def types(self, repo: str, load):
        return self._kept(repo, "types", None, load)

    def slug(self, repo: str, rec_type: str, slug: str, load):
        return self._kept(repo, "slugs", (rec_type, slug), load)

    def etags(
        self, repo: str, record_ids
    ) -> dict[int, tuple[str, tuple[str, ...]] | None]:
        self.check()
        generation = self._table(repo, "etags")  # this one, whatever happens next
        missing = [i for i in dict.fromkeys(record_ids) if i not in generation]
        if missing:
            listed = ", ".join(str(int(i)) for i in missing)
            with new_store(self._database, repo) as store:
                rows = store.con.execute(
                    "SELECT record_id, etag, deep_types FROM etags WHERE record_id IN "
                    f"({listed})"
                ).fetchall()
            found = {r[0]: (f"{r[1]:016x}", tuple(r[2])) for r in rows}
            for i in missing:
                generation[i] = found.get(i)
        return {i: generation[i] for i in record_ids}

    @property
    def keeps_docs(self) -> bool:
        return bool(self._graph_dir)

    def doc(self, repository, record_id: int, record_uri: str) -> dict | None:
        repo = repository.__name__
        current = self.etags(repo, [record_id])[record_id]
        if current is None:
            return None
        if self.keeps_docs:
            fingerprint = self.docs.compiled(repository).fingerprint
            if (
                kept := self.docs.get(repo, record_id, current[0], fingerprint)
            ) is not None:
                return json.loads(kept)
        xml = self.graph(repo, record_id)
        if xml is None:
            return None
        return self.docs.bulk_update(
            repository,
            {record_uri: record_id},
            xml.decode(),
            {record_id: current[0]},
        )[record_uri]

    def graph(self, repo: str, record_id: int) -> bytes | None:
        current = self.etags(repo, [record_id])[record_id]
        if current is None:
            return None
        graphs = self._graph_store()
        if graphs is not None and (kept := graphs.get(repo, record_id, current[0])):
            return kept
        with new_store(self._database, repo) as store:
            made = store.closure_source(record_id)
        if made is None:
            return None
        etag, text = made  # the etag of this text, which may be newer than `current`
        xml = rdfxml(text)
        if graphs is not None:
            graphs.put(repo, record_id, etag, xml)
        return xml

    def keep(self, repo: str, entries) -> None:
        graphs = self._graph_store()
        if graphs is None:
            raise ValueError("there is no graph cache: set gracia.cache_dir")
        graphs.put_many(
            (repo, record_id, etag, xml) for record_id, etag, xml in entries
        )

    def etags_held(self, repo: str) -> int:
        return len(self._shared("etags").get(repo, {}))

    def graph_stats(self, repo: str) -> dict | None:
        graphs = self._graph_store()
        if graphs is None:
            return None
        return {
            **graphs.stats(),
            "repo_entries": graphs.count(repo),
            "directory": str(self._graph_dir),
        }

    def forget(self, repo: str, record_ids) -> None:
        graphs = self._graph_store()
        if graphs is not None:
            graphs.delete_many(repo, record_ids)
        if self.keeps_docs:
            self.docs.delete_many(repo, record_ids)

    def close(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        if self._docs is not None:
            self._docs.close()
            self._docs = None
        if self._graphs is not None:
            self._graphs.close()
            self._graphs = None
