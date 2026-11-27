import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import quoteattr

import duckdb
import lmdb

from gracia.uri import XSL_NS
from gracia.utils import assets, xslt

log = logging.getLogger(__name__)

GRAPH_CACHE_BYTES = (
    2 * 1024**3
)  # the most LMDB maps; the file only grows as far as it is filled

STATS_LIMIT = 100_000  # entries counted for the stats of a repository, no more


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

    def repo_stats(self, repo: str, limit: int = STATS_LIMIT) -> dict:
        """What is kept of a repository: `entries`, and `bytes` (the keys and values;
        the
        file is shared). Counted no further than `limit` entries (`capped`)."""
        prefix = repo.encode() + b"\0"
        found = size = 0
        capped = False
        with self._env.begin(buffers=True) as txn:
            cursor = txn.cursor()
            if cursor.set_range(prefix):
                for key, value in cursor.iternext():
                    if bytes(key[: len(prefix)]) != prefix:
                        break
                    if found >= limit:
                        capped = True
                        break
                    found += 1
                    size += len(key) + len(value)
        return {"entries": found, "stale": 0, "bytes": size, "capped": capped}

    def delete_many(self, repo: str, record_ids) -> None:
        with self._env.begin(write=True) as txn:
            for record_id in record_ids:
                txn.delete(self._key(repo, record_id))

    def close(self) -> None:
        self._env.close()


# The most each LMDB file can hold (`gracia.cache.<name>_size` in the config); a file
# only grows as far as it is filled, and a cache that is full is emptied and starts
# again.
# An etag takes about 100 bytes, a slug about 80
ETAG_CACHE_BYTES = 256 * 1024**2
SLUG_CACHE_BYTES = 64 * 1024**2
REFERENCING_CACHE_BYTES = 512 * 1024**2
DEFAULT_CACHE_BYTES = {
    "graphs": GRAPH_CACHE_BYTES,
    "docs": GRAPH_CACHE_BYTES,
    "etags": ETAG_CACHE_BYTES,
    "slugs": SLUG_CACHE_BYTES,
    "referencing": REFERENCING_CACHE_BYTES,
}


def delete_range_batched(
    env: lmdb.Environment,
    start_key: bytes,
    end_key: bytes,
    batch_size: int = 5000,
    inclusive_end: bool = True,
) -> int:
    """Deletes the keys in [start_key, end_key] ([start_key, end_key) if not
    `inclusive_end`) in write transactions of at most `batch_size` keys, so that readers
    and writers of other keys are never held up for long. Returns how many were
    deleted."""
    total = 0
    seek = start_key
    while True:
        deleted = 0
        done = False
        with env.begin(write=True) as txn:
            cursor = txn.cursor()
            has_key = cursor.set_range(seek)
            while has_key and deleted < batch_size:
                key = cursor.key()
                if key > end_key if inclusive_end else key >= end_key:
                    done = True
                    break
                has_key = cursor.delete()  # removes it and moves to the next one
                deleted += 1
            if done or not has_key:
                done = True
            else:
                seek = cursor.key()
        total += deleted
        if done or deleted == 0:
            return total


class StampedStore:
    """What is kept for a repository while it is as it was, in LMDB:
    `repo \\0 repo_last_modified_at key -> value`, all bytes.

    `repo_last_modified_at` is when the repository was last written, in microseconds:
    when the database changes it changes, and what is under the old one is never read
    again; `sweep` deletes it with a ranged delete. The one who reads takes the time
    once and uses that key to read and to write, so that what was read before a write is
    never filed under the time after it."""

    def __init__(self, path: str | Path | None, size: int, name: str = "stamped"):
        self._tmp = None
        if path is None:  # no cache directory: a directory that lives as long as we do
            self._tmp = tempfile.TemporaryDirectory(prefix=f"gracia-{name}-")
            path = self._tmp.name
        Path(path).mkdir(parents=True, exist_ok=True)
        # metasync off: a crash may lose the last write, never corrupt; it is a cache
        self._env = lmdb.open(str(path), map_size=size, metasync=False)

    @staticmethod
    def _prefix(repo: str) -> bytes:
        return repo.encode() + b"\0"

    @classmethod
    def _key(cls, repo: str, last_modified_at: int, key: bytes) -> bytes:
        return cls._prefix(repo) + last_modified_at.to_bytes(8, "big") + key

    def sweep(self, repo: str, last_modified_at: int) -> int:
        """Deletes what was kept of the repository before that time."""
        prefix = self._prefix(repo)
        return delete_range_batched(
            self._env,
            prefix,
            prefix + last_modified_at.to_bytes(8, "big"),
            inclusive_end=False,
        )

    def get_bytes(
        self, repo: str, last_modified_at: int, keys: list[bytes]
    ) -> dict[bytes, bytes]:
        """The values of the keys that are kept; the others are not in the answer."""
        out = {}
        with self._env.begin() as txn:
            for key in keys:
                value = txn.get(self._key(repo, last_modified_at, key))
                if value is not None:
                    out[key] = bytes(value)
        return out

    def put_bytes(
        self, repo: str, last_modified_at: int, found: dict[bytes, bytes]
    ) -> None:
        for _ in range(2):
            try:
                with self._env.begin(write=True) as txn:
                    for key, value in found.items():
                        txn.put(self._key(repo, last_modified_at, key), value)
                return
            except lmdb.MapFullError:  # only a cache: start again
                with self._env.begin(write=True) as txn:
                    txn.drop(self._env.open_db(None, txn=txn), delete=False)
        raise ValueError(f"{len(found)} entries do not fit the cache")

    def repo_stats(
        self, repo: str, last_modified_at: int, limit: int = STATS_LIMIT
    ) -> dict:
        """What is kept of a repository: the `entries` of its current time, the `stale`
        ones of earlier times (not deleted yet) and `bytes` (keys and values of both).
        Counted no further than `limit` entries (`capped`)."""
        prefix = self._prefix(repo)
        current = last_modified_at.to_bytes(8, "big")
        found = stale = size = 0
        capped = False
        with self._env.begin(buffers=True) as txn:
            cursor = txn.cursor()
            if cursor.set_range(prefix):
                for key, value in cursor.iternext():
                    if bytes(key[: len(prefix)]) != prefix:
                        break
                    if found + stale >= limit:
                        capped = True
                        break
                    if bytes(key[len(prefix) : len(prefix) + 8]) == current:
                        found += 1
                    else:
                        stale += 1
                    size += len(key) + len(value)
        return {"entries": found, "stale": stale, "bytes": size, "capped": capped}

    def close(self) -> None:
        self._env.close()
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


class EtagStore(StampedStore):
    """The etags of the pages of the records: `record_id -> etag`. An empty value is a
    record that is not there (which is remembered too)."""

    def __init__(self, path: str | Path | None = None, size: int = ETAG_CACHE_BYTES):
        super().__init__(path, size, "etags")

    def get_many(
        self, repo: str, last_modified_at: int, record_ids
    ) -> dict[int, str | None]:
        """What is kept of the ids (`None` for a record that is not there is kept too);
        ids that are not kept are not in the answer."""
        keys = [record_id.to_bytes(4, "big") for record_id in record_ids]
        kept = self.get_bytes(repo, last_modified_at, keys)
        found: dict[int, str | None] = {}
        for record_id, key in zip(record_ids, keys, strict=True):
            if key in kept:
                found[record_id] = kept[key].decode() or None  # empty: not there
        return found

    def put_many(self, repo: str, last_modified_at: int, etag_records: dict) -> None:
        entries = {}
        for record_id, etag in etag_records.items():
            entries[record_id.to_bytes(4, "big")] = (etag or "").encode()
        self.put_bytes(repo, last_modified_at, entries)


class SlugStore(StampedStore):
    """The records of the slugs: `rec_type \\0 slug -> record_id, is_deleted`."""

    def __init__(self, path: str | Path | None = None, size: int = SLUG_CACHE_BYTES):
        super().__init__(path, size, "slugs")

    @staticmethod
    def _key_of(rec_type: str, slug: str) -> bytes:
        return rec_type.encode() + b"\0" + slug.encode()

    def get(
        self, repo: str, last_modified_at: int, rec_type: str, slug: str
    ) -> tuple[int, bool] | None:
        key = self._key_of(rec_type, slug)
        value = self.get_bytes(repo, last_modified_at, [key]).get(key)
        if value is None:
            return None
        return int.from_bytes(value[:4], "big"), bool(value[4])

    def put(
        self,
        repo: str,
        last_modified_at: int,
        rec_type: str,
        slug: str,
        found: tuple[int, bool],
    ) -> None:
        record_id, deleted = found
        self.put_bytes(
            repo,
            last_modified_at,
            {
                self._key_of(rec_type, slug): record_id.to_bytes(4, "big")
                + (b"\1" if deleted else b"\0")
            },
        )


class ReferencingStore(StampedStore):
    """The listings of `Store.referencing_records`: `md5 of the arguments -> record
    ids`,
    and the totals of them: `md5 of the arguments that count -> an integer`."""

    def __init__(
        self, path: str | Path | None = None, size: int = REFERENCING_CACHE_BYTES
    ):
        super().__init__(path, size, "referencing")

    @staticmethod
    def hash_of(**arguments) -> bytes:
        text = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        return hashlib.md5(text.encode()).digest()

    def get_ids(self, repo: str, last_modified_at: int, key: bytes) -> list[int] | None:
        value = self.get_bytes(repo, last_modified_at, [key]).get(key)
        if value is None:
            return None
        return [
            int.from_bytes(value[i : i + 4], "big") for i in range(0, len(value), 4)
        ]

    def put_ids(
        self, repo: str, last_modified_at: int, key: bytes, ids: list[int]
    ) -> None:
        self.put_bytes(
            repo, last_modified_at, {key: b"".join(i.to_bytes(4, "big") for i in ids)}
        )

    def get_total(self, repo: str, last_modified_at: int, key: bytes) -> int | None:
        value = self.get_bytes(repo, last_modified_at, [key]).get(key)
        return None if value is None else int.from_bytes(value, "big")

    def put_total(
        self, repo: str, last_modified_at: int, key: bytes, total: int
    ) -> None:
        self.put_bytes(repo, last_modified_at, {key: total.to_bytes(8, "big")})


class DocStore(GraphStore):
    BUNDLED_BASE = "gracia:assets/xslt/doc/record-base-01.xsl"

    def __init__(
        self,
        cache: "Cache",
        path: str | Path | None = None,
        size: int = GRAPH_CACHE_BYTES,
    ):
        self._cache = cache
        self._compiled: dict[str, xslt.Compiled] = {}  # repo: its doc stylesheet
        self._lock = (
            threading.RLock()
        )  # compiling asks the database, which may look at the cache
        if path is not None:
            super().__init__(path, size)
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

    @staticmethod
    def citation_style(repository) -> str | None:
        """The style the docs get their citation in; None when the extension is off."""
        try:
            found = repository.extension("citations")
        except KeyError:  # not registered: a bare repository, made by a script
            return None
        return found.style if found.enabled else None

    def fingerprint(self, repository, compiled: xslt.Compiled) -> str:
        """What a kept doc was made with: the stylesheet and the citation style."""
        style = self.citation_style(repository)
        return compiled.fingerprint + (f"+citation:{style}" if style else "")

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
        for slug, res_type in sorted(repository.types.items()):
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
        if style := self.citation_style(repository):
            self._cite(style, made)
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
                        etags[ids[uri]] + self.fingerprint(repository, compiled),
                        json.dumps(doc).encode(),
                    )
                )
        if entries and self._env is not None:
            self.put_many(entries)
        return made

    @staticmethod
    def _cite(style: str, made: dict[str, dict]) -> None:
        """Renders the CSL JSON of the batch in the style: `doc["citation"]`."""
        from gracia.utils import citations

        blobs = {
            uri: doc["csl"]
            for uri, doc in made.items()
            if "error" not in doc and isinstance(doc.get("csl"), str)
        }
        for uri, text in citations.render(style, blobs).items():
            made[uri]["citation"] = text

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


STAMPED = {"etags": EtagStore, "slugs": SlugStore, "referencing": ReferencingStore}


class Cache:
    def __init__(self, database, registry, *, db_file=None, graph_dir=None, sizes=None):
        self._database = database
        self._sizes = {**DEFAULT_CACHE_BYTES, **(sizes or {})}  # bytes, by cache
        self.registry = registry
        self._files = [Path(db_file), Path(f"{db_file}.wal")] if db_file else []
        self._seen = None  # the signature of the files when the table was last read
        self._written: dict[str, object] = {}  # when each repository was written, then
        self._graph_dir = graph_dir
        self._graphs: GraphStore | None = None  # opened by the first use
        self._docs: DocStore | None = None  # likewise
        self._stamped: dict[str, StampedStore] = {}  # the ones that are open, by name
        # what follows the writes (`gracia.change_events.ChangeEventsWorker`): kept
        # running by `check`
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
                self._sizes["docs"],
            )
        return self._docs

    def stamped(self, name: str) -> StampedStore:
        """The stamped cache `name` (`etags`, `slugs` or `referencing`), opened the
        first
        time it is asked for (a process can open an LMDB directory once, so the stores
        that are never asked for are not opened). What an earlier run left of the
        repositories is old, and is deleted."""
        if name not in self._stamped:
            self.refresh()
            directory = Path(self._graph_dir) / name if self._graph_dir else None
            store = STAMPED[name](directory, self._sizes[name])
            for repo, moment in self._written.items():
                store.sweep(repo, self._micros(moment))
            self._stamped[name] = store
        return self._stamped[name]

    @property
    def graph_store(self) -> GraphStore | None:
        if self._graphs is None and self._graph_dir:
            self._graphs = GraphStore(self._graph_dir, self._sizes["graphs"])
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
        self.refresh()

    def refresh(self) -> None:
        """Sees the writes: what was kept of a repository that was written is dropped,
        and what the open stamped caches kept of it before the write is deleted. One
        that is not open does it when it opens (`stamped`). What is left is what a
        request that was running wrote under the old time, and goes with the next
        write."""
        signature = self.file_signature()
        if signature is not None and signature == self._seen:
            return
        # read after the signature: a write in between shows the next time
        written = self.last_written()
        for repo, moment in written.items():
            if self._written.get(repo) != moment:
                self.invalidate(repo)
                for store in self._stamped.values():
                    store.sweep(repo, self._micros(moment))
        self._written, self._seen = written, signature

    @staticmethod
    def _micros(moment) -> int:
        """A `last_modified_at` of a repository as microseconds since the epoch."""
        if moment is None:
            return 0
        return (moment - datetime(1970, 1, 1, tzinfo=moment.tzinfo)) // timedelta(
            microseconds=1
        )

    def repo_last_modified_at(self, repo: str) -> int:
        return self._micros(self._written.get(repo))

    def written(self) -> dict[str, object]:
        self.refresh()
        return dict(self._written)

    def invalidate(self, repo: str) -> None:
        """Drops what is kept in memory of a repository: its types, and the stylesheets
        of its docs."""
        self.registry.get("types", {}).pop(repo, None)
        if self._docs is not None:  # nothing to drop where it was never opened
            self._docs.drop(repo)

    @property
    def keeps_docs(self) -> bool:
        return bool(self._graph_dir)

    def keep(self, repo: str, entries) -> None:
        graphs = self.graph_store
        if graphs is None:
            raise ValueError("there is no graph cache: set gracia.cache_dir")
        graphs.put_many(
            (repo, record_id, etag, xml) for record_id, etag, xml in entries
        )

    def repo_stats(self, repo: str) -> dict:
        """What each cache keeps of a repository, and the time of the repository that
        the stamped caches are of: `{"modified_at": the time or None, "caches":
        [{"name",
        "entries", "stale", "bytes", "capped"} ...]}`. The graph and doc caches have no
        times (an entry is used when its etag is the current one: `stale` is 0), and are
        None without a cache directory. The worker is not started by it (`check` does):
        a page that says the worker is stopped must not start it."""
        self.refresh()
        stamp = self.repo_last_modified_at(repo)
        caches = [
            {"name": "etags", **self.stamped("etags").repo_stats(repo, stamp)},
            {"name": "slugs", **self.stamped("slugs").repo_stats(repo, stamp)},
            {
                "name": "referencing",
                **self.stamped("referencing").repo_stats(repo, stamp),
            },
        ]
        graphs = self.graph_store
        caches.append(
            {"name": "graphs", **graphs.repo_stats(repo)}
            if graphs
            else {"name": "graphs"}
        )
        caches.append(
            {"name": "docs", **self.docs.repo_stats(repo)}
            if self.keeps_docs
            else {"name": "docs"}
        )
        return {"modified_at": self._written.get(repo), "caches": caches}

    def forget(self, repo: str, record_ids) -> None:
        graphs = self.graph_store
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
        for store in self._stamped.values():
            store.close()
        self._stamped = {}
