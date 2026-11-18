"""What follows the data: a search index, the graph cache, anything that has to hear that a record changed.

Every write queues a row for each record it changed (`actions_queue`, see records.sql): `insert`,
`update`, `delete`, and `ref_update` for a record that was not written but whose page changed because
something it is made of did (`Store.refresh_etags`). The rows are written by the triggers and by the
etag refresh, in the transaction of the write: a change is queued if and only if it is committed.

`process_batch` handles the queue a batch at a time, up to `BATCH` records and `BATCH_BYTES` of the
sources of what they are made of (`etags.closure_bytes`, so a batch is a size and not a number):

* it takes the oldest rows (`Store.take_actions`): the records they name, each with the newest action
  the queue has for it, so a record that changed three times is one change and one that was deleted
  after an update is a delete;
* it reads every record that is there with all it is made of (`Store.closure_sources`) and `deliver`s
  them: one `ox.Dataset` each, the RDF/XML of it in the graph cache when there is one (so the cache is
  kept up to date by the queue, and a full rebuild is only for the first fill), the graphs of the
  records that are gone taken out, and one `RecordsChanged` event for the batch, with the changes and
  the datasets, so that what listens (the index) works on a batch at a time, never one record at a time;
* only then does it take the rows off the queue. A handler that fails leaves them, and the batch is
  done again: handlers must be able to do a record twice (read it as it is, put it in the index by its id).

For a lot at once (the job "Process pending actions") the queue is exported the way the export task
exports records (`Store.export_queue`: one `COPY`, files of 32 MB, each record a graph with its etag
and action), and the files are `deliver`ed, so DuckDB does the reading and no query is made for a batch.

`process_pending` does the same by hand, for every repository or one: what a test calls, and what the
job "Process pending actions" runs. `Worker` runs it on a thread, started with the application (the
setting `gracia.worker`, with `gracia.worker.batch` and `gracia.worker.poll`): it looks when the files
of the database changed (the same signature `Cache.check` uses), a lock per repository keeps two
processes off the same queue, and `Cache.check` starts it again if it died.
"""

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

BATCH = 5000  # the most rows a batch takes off the queue, and so the most records of an event ...
BATCH_BYTES = 20_000_000  # ... and the most bytes of sources it reads (what the records are made of)
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
    """The event of a batch (subscribe with `config.add_subscriber(handler, RecordsChanged)`).

    `application` and `repo` say where; `changes` is the list of `Change`; `graphs` has, for every
    record of the changes that is there, an `ox.Dataset` of it and of everything it is made of, by
    record uri (a delete has none). The datasets are the handler's to use, they are not kept.
    `docs` has the doc of each of those records that could be made (`DocStore`), by record uri;
    empty when there is no doc cache, or the stylesheets could not be compiled (logged)."""

    def __init__(
        self,
        application,
        repo: str,
        changes: list[Change],
        graphs: dict[str, ox.Dataset],
        docs: dict[str, dict] | None = None,
    ):
        self.application = application
        self.repo = repo
        self.changes = changes
        self.graphs = graphs
        self.docs = docs or {}


def _notify(registry, event: RecordsChanged) -> None:
    """`registry.notify`, with the registry in place for whoever asks Pyramid for the current one
    (there is no request on a worker thread)."""
    manager.push({"registry": registry, "request": None})
    try:
        registry.notify(event)
    finally:
        manager.pop()


def lock_dir_for(settings: dict) -> Path | None:
    """Where the lock files of the queues go: `gracia.cache_dir`, else next to the database file
    (`gracia.db.file`, or `gracia.db` when that is a file); None when there is no file (a memory
    database: one process, nothing to keep apart)."""
    if settings.get("gracia.cache_dir"):
        return Path(settings["gracia.cache_dir"])
    target = settings.get("gracia.db") or os.environ.get("GRACIA_DB", "gracia.db")
    database = settings.get("gracia.db.file") or (None if target.startswith("quack:") else target)
    return Path(database).parent if database and database != ":memory:" else None


@contextmanager
def repository_lock(lock_dir, repo: str, wait: bool = False):
    """Yield whether this process holds the lock of the queue of `repo`: one process at a time
    handles it (`flock`: the system lets go of it when the process dies, however it dies). With `wait`
    it waits for the lock instead of giving up. No `lock_dir`: there is one process, it is held."""
    if lock_dir is None:
        yield True
        return
    Path(lock_dir).mkdir(parents=True, exist_ok=True)
    descriptor = os.open(Path(lock_dir) / f"actions-{repo}.lock", os.O_CREAT | os.O_RDWR)
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
    application, registry, repo: str | None = None, batch: int = BATCH, max_bytes: int | None = BATCH_BYTES
) -> int:
    """Handle the queue of `repo` (every repository when none is named) now, batch after batch until
    it is empty, on this thread and without the lock: the manual way, for a test, a script or a job.
    Returns how many records were handled."""
    total = 0
    for name in [repo] if repo else application.list_repositories():
        while changes := process_batch(application, registry, name, batch, max_bytes):
            total += len(changes)
    return total


def deliver(application, registry, repo: str, entries) -> list[Change]:
    """Hand records that changed to what follows the data: for each `(record id, record uri, action,
    etag, N-Triples of the record and what it is made of)`, a dataset (a delete, or a record that
    has no text, has none), the RDF/XML of it in the graph cache when there is one (and the graph of
    a record that is gone taken out), and one `RecordsChanged` event for all of them. Returns the
    changes. What is queued is not touched: the caller takes it off when this has returned."""
    cache = application.cache
    changes, graphs, made, gone = [], {}, [], []
    for record_id, record, action, etag, text in entries:
        if action == "delete" or text is None:  # gone: deleted, and perhaps made again, since
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
    docs = _make_docs(application, registry, repo, changes, graphs, {i: etag for i, _, _, etag, _ in entries}) if graphs else {}
    _notify(registry, RecordsChanged(application, repo, changes, graphs, docs))
    return changes


def _make_docs(application, registry, repo: str, changes, graphs, etags) -> dict[str, dict]:
    """The docs of the records of a batch (one run of the doc stylesheet over their graphs) and the ones
    that could be made, in the doc cache. A cache is what a stylesheet that does not work must not stop:
    it is logged, and the batch goes on without docs (`Cache.doc` makes one when it is asked for)."""
    from gracia.resources import Repository

    cache = application.cache
    if not cache.keeps_docs:
        return {}
    ids = {c.record: c.record_id for c in changes if c.record in graphs}
    merged = ox.Dataset(quad for record in ids for quad in graphs[record])
    manager.push({"registry": registry, "request": None})  # the files of the types are assets of the registry
    repository = Repository(repo, application)
    try:
        made = cache.docs.bulk_update(repository, ids, ox.serialize(merged, format=ox.RdfFormat.RDF_XML).decode(), etags)
    except Exception:
        log.exception("%s: the docs of %d records could not be made", repo, len(ids))
        return {}
    finally:
        repository.close()
        manager.pop()
    return {record: doc for record, doc in made.items() if "error" not in doc}


def process_batch(
    application, registry, repo: str, limit: int = BATCH, max_bytes: int | None = BATCH_BYTES
) -> list[Change]:
    """Handle the next rows of the queue of a repository (see the module): at most `limit` rows, and
    records until the sources of what they are made of are `max_bytes` (None: no limit). Returns the
    changes that were handled: none when the queue is empty."""
    with application.open_repository(repo) as store:
        store.catch_up_etags()  # a write outside the store has not made its etags: they are what is read
        taken, bound = store.take_actions(limit, max_bytes)
        if not taken:
            return []
        sources = store.closure_sources(i for i, _, action in taken if action != "delete")
        entries = []
        for record_id, record, action in taken:
            etag, text = sources.get(record_id, (None, None))
            entries.append((record_id, record, action, etag, text))
        changes = deliver(application, registry, repo, entries)
        store.actions_done([c.record_id for c in changes], bound)
    return changes


class Worker:
    """The thread that handles the queues of all the repositories of an application.

    The repositories that may have something to do stand in a line. A turn takes the first, handles
    **one batch** of its queue, and puts it at the back of the line if that batch was not empty, so
    a repository with a large backlog does not keep the others waiting, and one that is empty leaves
    the line. A repository joins the line when it was written (its time in
    `repositories_last_modified_at` moved, which `Cache.moments` reads when the files of the database
    changed: not for the rows the worker takes off itself, nor for the progress of a job), and at the
    start, when every repository joins. A repository that failed waits out its own pause (up to a
    minute, doubling) and the others go on."""

    def __init__(
        self, application, registry, *, lock_dir=None, poll: float = POLL, batch: int = BATCH,
        batch_bytes: int | None = BATCH_BYTES,
    ):
        """`lock_dir` is where the lock files are made, one per repository: a repository is handled by
        one process at a time (None: there is one process, no lock). `batch` is the most rows of the
        queue that go in one event, `batch_bytes` the most bytes of sources, `poll` how many
        seconds pass between looks."""
        self.application, self.registry, self.batch, self.poll = application, registry, batch, poll
        self.batch_bytes = batch_bytes
        self.lock_dir = Path(lock_dir) if lock_dir else None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stopped = False
        self._wake = threading.Event()
        self._seen = None  # the signature of the files when the repositories were last looked at
        self._moments: dict[str, object] = {}  # when each was written, then
        self._first = True
        self._line: deque[str] = deque()  # the repositories that may have something to do
        self._retry: dict[str, tuple[float, int]] = {}  # repo: (when to try again, failures so far)

    # ---- the thread -------------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._start_lock:
            if self.alive:
                return
            self._stopped = False
            self._thread = threading.Thread(target=self._run, name="gracia-worker", daemon=True)
            self._thread.start()

    def ensure_running(self) -> None:
        """Start the thread again if it died (a worker that was stopped stays stopped)."""
        if not self._stopped and not self.alive:
            self.start()

    def stop(self, timeout: float = 10) -> None:
        self._stopped = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def wake(self) -> None:
        """Look at the queues now (something was written in this process)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stopped:
            try:
                busy = self.turn()
            except Exception:  # whatever it was, the thread goes on: the rows are still queued
                log.exception("the worker could not look at the queues")
                busy = False
            if not busy:  # something was done: the next repository is next at once
                self._wake.wait(self._pause())
                self._wake.clear()

    def status(self, repo: str) -> dict:
        """What the worker is doing about `repo`: whether the thread runs, whether the repository is in
        line, how many times in a row it failed and in how many seconds it is tried again."""
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
        """How long to wait when there is nothing to do now: a poll, or less when a repository that
        failed is to be tried again before that."""
        pause = self.poll
        now = time.monotonic()
        for repo in self._line:
            if repo in self._retry:
                pause = min(pause, max(0.05, self._retry[repo][0] - now))
        return pause

    # ---- one turn ---------------------------------------------------------------------------

    def _notice(self) -> None:
        """Let the repositories that were written join the line (all of them the first time)."""
        signature = self.application.cache.signature()
        if not self._first and signature is not None and signature == self._seen:
            return
        moments = self.application.cache.moments()  # read after the signature: a write in between shows next time
        if self._first:
            changed = self.application.list_repositories()
            self._first = False
        else:
            changed = [repo for repo, moment in moments.items() if self._moments.get(repo) != moment]
        self._moments, self._seen = moments, signature
        for repo in changed:
            if repo not in self._line:
                self._line.append(repo)

    def turn(self) -> bool:
        """Look at what was written, then give the repositories in the line a turn, the first one that
        has something to do being handled for one batch. Returns whether a batch was handled."""
        self._notice()
        for _ in range(len(self._line)):
            repo = self._line.popleft()
            due = self._retry.get(repo)
            if due is not None and due[0] > time.monotonic():
                self._line.append(repo)  # still waiting out a failure
                continue
            with self._lock(repo) as held:
                if not held:  # another process is handling it: it may leave rows, so it stays in line
                    self._line.append(repo)
                    continue
                try:
                    changes = process_batch(self.application, self.registry, repo, self.batch, self.batch_bytes)
                except Exception:
                    log.exception("the queue of %s could not be handled", repo)
                    failures = (due[1] if due else 0) + 1
                    self._retry[repo] = (time.monotonic() + min(MAX_PAUSE, 2**failures), failures)
                    self._line.append(repo)
                    continue
            self._retry.pop(repo, None)
            if changes:
                self._line.append(repo)  # and what is left of it waits for the others
                return True
        return False

    def drain(self, repo: str) -> int:
        """Handle batches of the queue of `repo` until it is empty, without a lock or a line. Returns
        how many records."""
        total = 0
        while not self._stopped:
            done = process_batch(self.application, self.registry, repo, self.batch, self.batch_bytes)
            if not done:
                break
            total += len(done)
        return total

    def _lock(self, repo: str):
        return repository_lock(self.lock_dir, repo)
