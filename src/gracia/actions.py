import fcntl
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pyoxigraph as ox
from pyramid.threadlocal import manager

from gracia.cache import dataset_rdfxml
from gracia.graph import serialize_bytes

# the most rows a batch takes off the queue, and so the most records of an event ...
BATCH = 5000
# ... and the most bytes of sources it reads (what the records are made of)
BATCH_BYTES = 20_000_000
POLL = 1.0  # seconds between looks, when nothing wakes the worker
MAX_PAUSE = 60  # seconds: the longest the worker waits after a failure
ACTIONS = ("insert", "update", "delete", "ref_update")

log = logging.getLogger("gracia.actions")


@dataclass(frozen=True)
class Change:
    record_id: int
    record: str  # its uri
    action: str  # one of ACTIONS; a record that is not there any more is a `delete`


class RecordsChanged:
    def __init__(
        self,
        repository,
        changes: list[Change],
        graphs: dict[str, ox.Dataset],
        docs: dict[str, dict] | None = None,
    ):
        self.repository = repository
        self.changes = changes
        self.graphs = graphs
        self.docs = docs or {}

    @property
    def application(self):
        return self.repository.__parent__

    @property
    def repo(self) -> str:
        return self.repository.__name__


def _notify(registry, event: RecordsChanged) -> None:
    manager.push({"registry": registry, "request": None})
    try:
        registry.notify(event)
    finally:
        manager.pop()


def lock_dir_for(settings: dict) -> Path | None:
    if settings.get("gracia.cache_dir"):
        return Path(settings["gracia.cache_dir"])
    target = settings.get("gracia.db") or os.environ.get("GRACIA_DB", "gracia.db")
    database = settings.get("gracia.db.file") or (
        None if target.startswith("quack:") else target
    )
    return Path(database).parent if database and database != ":memory:" else None


@contextmanager
def repository_lock(lock_dir, repo: str, wait: bool = False):
    if lock_dir is None:
        yield True
        return
    Path(lock_dir).mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        Path(lock_dir) / f"actions-{repo}.lock", os.O_CREAT | os.O_RDWR
    )
    held = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            held = True
        except BlockingIOError:
            pass
        yield held
    finally:
        if held:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def process_pending(
    application,
    registry,
    repo: str | None = None,
    batch: int = BATCH,
    max_bytes: int | None = BATCH_BYTES,
) -> int:
    total = 0
    for name in [repo] if repo else application.list_repositories():
        while changes := process_batch(application, registry, name, batch, max_bytes):
            total += len(changes)
    return total


def deliver(application, registry, repo: str, entries) -> list[Change]:
    cache = application.cache
    changes, graphs, made, gone = [], {}, [], []
    for record_id, record, action, etag, text in entries:
        if (
            action == "delete" or text is None
        ):  # gone: deleted, and perhaps made again, since
            changes.append(Change(record_id, record, "delete"))
            gone.append(record_id)
            continue
        graph = ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.N_TRIPLES))
        graphs[record] = graph
        changes.append(Change(record_id, record, action))
        if cache.keeps_graphs:
            made.append((record_id, etag, dataset_rdfxml(graph)))
    if made:
        cache.keep(repo, made)  # one write for the batch
    if gone and cache.keeps_graphs:
        cache.forget(repo, gone)
    manager.push(
        {"registry": registry, "request": None}
    )  # the files of the types are assets of the registry
    repository = application[repo]
    try:
        docs = (
            _make_docs(
                repository, changes, graphs, {i: etag for i, _, _, etag, _ in entries}
            )
            if graphs
            else {}
        )
        _notify(registry, RecordsChanged(repository, changes, graphs, docs))
    finally:
        repository.close()
        manager.pop()
    return changes


def _make_docs(repository, changes, graphs, etags) -> dict[str, dict]:
    cache = repository.__parent__.cache
    if not cache.keeps_docs:
        return {}
    ids = {c.record: c.record_id for c in changes if c.record in graphs}
    merged = ox.Dataset(quad for record in ids for quad in graphs[record])
    try:
        made = cache.docs.bulk_update(
            repository,
            ids,
            serialize_bytes(merged, format=ox.RdfFormat.RDF_XML).decode(),
            etags,
        )
    except Exception:
        log.exception(
            "%s: the docs of %d records could not be made",
            repository.__name__,
            len(ids),
        )
        return {}
    return {record: doc for record, doc in made.items() if "error" not in doc}


def process_batch(
    application,
    registry,
    repo: str,
    limit: int = BATCH,
    max_bytes: int | None = BATCH_BYTES,
) -> list[Change]:
    with application.new_store(repo) as store:
        # a write outside the store has not made its etags: they are what is read
        store.catch_up_etags()
        taken, bound = store.take_actions(limit, max_bytes)
        if not taken:
            return []
        sources = store.closure_sources(
            i for i, _, action in taken if action != "delete"
        )
        entries = []
        for record_id, record, action in taken:
            etag, text = sources.get(record_id, (None, None))
            entries.append((record_id, record, action, etag, text))
        changes = deliver(application, registry, repo, entries)
        store.actions_done([c.record_id for c in changes], bound)
    return changes


class Worker:
    def __init__(
        self,
        application,
        registry,
        *,
        lock_dir=None,
        poll: float = POLL,
        batch: int = BATCH,
        batch_bytes: int | None = BATCH_BYTES,
    ):
        self.application, self.registry, self.batch, self.poll = (
            application,
            registry,
            batch,
            poll,
        )
        self.batch_bytes = batch_bytes
        self.lock_dir = Path(lock_dir) if lock_dir else None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stopped = False
        self._wake = threading.Event()
        self._written: dict[str, object] = {}  # when each was written, then
        self._first = True
        self._line: deque[str] = (
            deque()
        )  # the repositories that may have something to do
        self._retry: dict[
            str, tuple[float, int]
        ] = {}  # repo: (when to try again, failures so far)

    # ---- the thread
    # -------------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._start_lock:
            if self.alive:
                return
            self._stopped = False
            self._thread = threading.Thread(
                target=self._run, name="gracia-worker", daemon=True
            )
            self._thread.start()

    def ensure_running(self) -> None:
        if not self._stopped and not self.alive:
            self.start()

    def stop(self, timeout: float = 10) -> None:
        self._stopped = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stopped:
            try:
                busy = self.turn()
            except (
                Exception
            ):  # whatever it was, the thread goes on: the rows are still queued
                log.exception("the worker could not look at the queues")
                busy = False
            if not busy:  # something was done: the next repository is next at once
                self._wake.wait(self._pause())
                self._wake.clear()

    def status(self, repo: str) -> dict:
        retry = self._retry.get(repo)
        return {
            "running": self.alive,
            "stopped": self._stopped,
            "in_line": repo in self._line,
            "line": len(self._line),
            "failures": retry[1] if retry else 0,
            "retry_in": max(0.0, retry[0] - time.monotonic()) if retry else None,
        }

    def _pause(self) -> float:
        pause = self.poll
        now = time.monotonic()
        for repo in self._line:
            if repo in self._retry:
                pause = min(pause, max(0.05, self._retry[repo][0] - now))
        return pause

    # ---- one turn
    # ---------------------------------------------------------------------------

    def _notice(self) -> None:
        written = self.application.cache.written()
        changed = [r for r, t in written.items() if self._written.get(r) != t]
        self._written = written
        if self._first:
            changed = self.application.list_repositories()
            self._first = False
        for repo in changed:
            if repo not in self._line:
                self._line.append(repo)

    def turn(self) -> bool:
        self._notice()
        for _ in range(len(self._line)):
            repo = self._line.popleft()
            due = self._retry.get(repo)
            if due is not None and due[0] > time.monotonic():
                self._line.append(repo)  # still waiting out a failure
                continue
            with self._lock(repo) as held:
                # another process is handling it: it may leave rows, so it stays in line
                if not held:
                    self._line.append(repo)
                    continue
                try:
                    changes = process_batch(
                        self.application,
                        self.registry,
                        repo,
                        self.batch,
                        self.batch_bytes,
                    )
                except Exception:
                    log.exception("the queue of %s could not be handled", repo)
                    failures = (due[1] if due else 0) + 1
                    self._retry[repo] = (
                        time.monotonic() + min(MAX_PAUSE, 2**failures),
                        failures,
                    )
                    self._line.append(repo)
                    continue
            self._retry.pop(repo, None)
            if changes:
                self._line.append(repo)  # and what is left of it waits for the others
                return True
        return False

    def drain(self, repo: str) -> int:
        total = 0
        while not self._stopped:
            done = process_batch(
                self.application, self.registry, repo, self.batch, self.batch_bytes
            )
            if not done:
                break
            total += len(done)
        return total

    def _lock(self, repo: str):
        return repository_lock(self.lock_dir, repo)
