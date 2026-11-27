import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import luigi
import pyoxigraph as ox
from luigi.local_target import LocalTarget
from pyramid.threadlocal import manager

from gracia import uri
from gracia.change_events import ChangeEventsWorker
from gracia.graph import Graph, dataset_rdfxml
from gracia.jobs.runner import context
from gracia.jobs.stage import JobTask, Stage, StageFailure
from gracia.utils import assets


def find_type(node, wanted: str):
    types = node.types
    found = types.get(wanted) or next(
        (k for k in types.values() if k.name == wanted), None
    )
    if found is None:
        known = ", ".join(sorted(types)) or "none"
        raise StageFailure(
            f"no type {wanted!r} in repository {node.__name__} (known: {known})"
        )
    return found


# ---- the plan
# -----------------------------------------------------------------------------------


class Plan(JobTask):
    first = luigi.Parameter()  # the JSON of the first stage of the job

    def output(self):
        return LocalTarget(str(Path(self.job_dir) / "plan.json"))

    def run(self):
        stage = Stage.make(self.job_dir, self.repo, [json.loads(self.first)], 0)
        JobTask.write_json(self.output(), stage.plan())


class Source(Stage):
    first = True
    abstract = True


# ---- Export
# --------------------------------------------------------------------------------------


class Export(Source):
    res_type = luigi.Parameter(default="")  # a slug or a name; none: every type
    chunk_bytes = luigi.IntParameter(default=0)  # 0: all in one file
    graphs = luigi.BoolParameter(default=False)
    queued = luigi.BoolParameter(default=False)

    @property
    def extension(self) -> str:
        return "trig" if self.graphs or self.queued else "nt"

    def plan(self) -> dict:
        with context.repository(self.repo) as node:
            res_type = find_type(node, self.res_type) if self.res_type else None
            directory = self.directory()
            directory.mkdir(parents=True, exist_ok=True)
            bound = (
                node.store.last_transaction()
            )  # before the export: what is written after is not in it
            if self.queued:
                count, files, bound = node.store.export_queue(
                    directory / "chunks", self.chunk_bytes
                )
            else:
                count, files = node.store.export_chunks(
                    directory / "chunks",
                    res_type.slug if res_type else None,
                    self.chunk_bytes,
                    graphs=self.graphs,
                )
            for number, source in enumerate(files):  # the name a batch has
                os.replace(source, directory / f"batch-{number:05d}.{self.extension}")
            shutil.rmtree(directory / "chunks", ignore_errors=True)
        return {
            "batches": len(files),
            "estimate": count,
            "unit": "records",
            "type": res_type.name if res_type else "all types",
            "chunk_bytes": self.chunk_bytes,
            "bound": bound,
        }

    @classmethod
    def offer(cls, job_dir: Path, stages: list, index: int) -> tuple[str, str] | None:
        for extension, content_type in (
            ("nt", "application/n-triples"),
            ("trig", "application/trig"),
        ):
            files = sorted(
                (job_dir / f"{index:02d}-{cls.__name__}").glob(f"batch-*.{extension}")
            )
            if files:
                with open(job_dir / f"export.{extension}", "wb") as out:
                    for path in files:
                        with open(path, "rb") as part:
                            shutil.copyfileobj(part, out)
                return f"export.{extension}", content_type
        return None

    def run(self):
        out = self.output()
        path = Path(out["passed"].path)
        count = len(JobTask.read_items(path))
        JobTask.write_lines(out["rejected"], [])
        JobTask.write_json(
            out["stats"],
            {"processed": count, "passed": count, "rejected": {}, "samples": []},
        )


class UpdateCache(Stage):
    def process(self, items):
        cache = context.application().cache
        passed, rejected, made = [], [], []
        with context.repository(self.repo) as node:
            store = node.store
            for item in items:
                record_id = store.record_id_of(item["id"])
                if record_id is None:
                    rejected.append({"id": item["id"], "reason": "no_record_id"})
                    continue
                graph = ox.Dataset(
                    ox.parse(item["source"].encode(), format=ox.RdfFormat.N_TRIPLES)
                )
                made.append((record_id, item["etag"], dataset_rdfxml(graph)))
                passed.append({"id": item["id"]})
        try:
            cache.keep(self.repo, made)  # one write for the batch
        except ValueError as err:
            raise StageFailure(str(err)) from None
        return passed, rejected


class ProcessChangeEvents(Stage):
    def process(self, items):
        bound = json.loads((Path(self.job_dir) / "plan.json").read_text())["bound"]
        entries, rejected = [], []
        with context.repository(self.repo) as node:
            store = node.store
            for item in items:
                record_id = store.record_id_of(item["id"])
                if record_id is None:
                    rejected.append({"id": item["id"], "reason": "no_record_id"})
                    continue
                gone = item["etag"] == "delete"
                entries.append(
                    (
                        record_id,
                        item["id"],
                        item["kind"],
                        None if gone else item["etag"],
                        None if gone else item["source"],
                    )
                )
            with ChangeEventsWorker.repository_lock(
                ChangeEventsWorker.lock_dir_for(context.settings()),
                self.repo,
                wait=True,
            ):
                changes = ChangeEventsWorker(
                    context.application(), context.registry()
                ).deliver(self.repo, entries)
                store.change_events_done([c.record_id for c in changes], bound)
        return [{"id": c.record, "kind": c.kind} for c in changes], rejected


class IndexRecords(Stage):
    def process(self, items):
        from gracia.search import definitions_for

        job_dir = Path(self.job_dir)
        plan = json.loads((job_dir / "plan.json").read_text())
        locks, token = ChangeEventsWorker.lock_dir_for(context.settings()), job_dir.name
        rejected, ids = [], {}
        with context.repository(
            self.repo
        ) as node:  # this makes the configuration, which sets `indexes`
            indexes = context.application().indexes
            if indexes is None:
                raise StageFailure("there is no search index: set gracia.index_dir")
            store = node.store
            for item in items:
                record_id = store.record_id_of(item["id"])
                if record_id is None:
                    rejected.append({"id": item["id"], "reason": "no_record_id"})
                else:
                    ids[item["id"]] = record_id
            dataset = ox.Dataset(
                ox.parse(
                    "".join(i["source"] for i in items if i["id"] in ids).encode(),
                    format=ox.RdfFormat.N_TRIPLES,
                )
            )
            definitions = definitions_for(node)
            docs = []
            if ids and definitions:
                manager.push(
                    {"registry": context.registry(), "request": None}
                )  # the files of the types are assets
                try:
                    made = context.application().cache.docs.bulk_update(
                        node,
                        ids,
                        dataset_rdfxml(dataset).decode(),
                    )
                finally:
                    manager.pop()
                docs = list(made.values())
            for definition in definitions:
                with ChangeEventsWorker.repository_lock(
                    locks, f"index-{self.repo}-{definition.name}", wait=True
                ):  # one writer
                    done = indexes.build_batch(self.repo, definition, token, docs)
                for record, found in done["problems"].items():
                    rejected.append(
                        {
                            "id": record,
                            "reason": "document",
                            "message": "; ".join(found),
                        }
                    )
            marks = job_dir / "index-done"
            marks.mkdir(exist_ok=True)
            (marks / str(self.n)).write_text("")
            if (
                len(list(marks.iterdir())) >= plan["batches"]
            ):  # the last: the new indexes take over
                with ChangeEventsWorker.repository_lock(
                    ChangeEventsWorker.lock_dir_for(context.settings()),
                    self.repo,
                    wait=True,
                ):  # not the worker's turn
                    if not (job_dir / "index-swapped").exists():
                        for definition in definitions:
                            indexes.swap(self.repo, definition, token)
                        store.requeue_changes_since(plan["bound"])
                        (job_dir / "index-swapped").write_text("")
        return [{"id": uri_} for uri_ in ids], rejected


class Rewrite(Source):
    res_type = luigi.Parameter()
    size = luigi.IntParameter(default=5000)

    def plan(self) -> dict:
        with context.repository(self.repo) as node:
            res_type = find_type(node, self.res_type)
            last = node.store.highest_number(res_type.slug)  # not what is only reserved
        return {
            "batches": -(-last // self.size),
            "estimate": last,
            "unit": "records",
            "type": res_type.name,
            "size": self.size,
        }

    def process(self, items):
        plan = self.plan_data()
        low = self.n * plan["size"] + 1
        high = min(low + plan["size"] - 1, plan["estimate"])
        with context.repository(self.repo) as node:
            res_type = find_type(node, self.res_type)
            changed = node.store.rederive(res_type.slug, (low, high))
        return [{"id": record} for record in changed], []


class PurgeSlugs(Source):
    res_type = luigi.Parameter()

    def plan(self) -> dict:
        with context.repository(self.repo) as node:
            res_type = find_type(node, self.res_type)
        return {"batches": 1, "estimate": 0, "unit": "slugs", "type": res_type.name}

    def process(self, items):
        with context.repository(self.repo) as node:
            res_type = find_type(node, self.res_type)
            purged = node.store.purge_slugs(res_type.slug)
        return [{"id": record, "slug": slug} for slug, record in purged], []


class RebuildEtags(Source):
    def plan(self) -> dict:
        with context.repository(self.repo) as node:
            records = sum(node.store.count_records_by_type().values())
        return {"batches": 1, "estimate": records, "unit": "etags"}

    def process(self, items):
        with context.repository(self.repo) as node:
            store = node.store
            # the numbers of its types, which the ids of the etags are
            # made of
            store.sync()
            wrong = store.rebuild_etags()
        return [{"id": record} for record in wrong], []


# ---- Validate
# ------------------------------------------------------------------------------------


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text or "default")


class PinSchema(JobTask):
    res_type = luigi.Parameter()
    schema = luigi.Parameter(default="")

    def directory(self) -> Path:
        return Path(self.job_dir) / "schema" / _safe(self.res_type) / _safe(self.schema)

    def output(self):
        d = self.directory()
        return {
            "file": LocalTarget(str(d / "schema.ttl")),
            "info": LocalTarget(str(d / "info.json")),
        }

    def run(self):
        with context.repository(self.repo) as node:
            res_type = find_type(node, self.res_type)
            schema = self.schema or res_type.schema
            if not schema:
                raise StageFailure(
                    f"the type {res_type.name} is on no schema: name one with `schema`"
                )
            try:
                spec = assets.schema_spec(res_type.assets or "", schema, res_type.slug)
            except ValueError as err:
                raise StageFailure(str(err)) from None
            try:
                source = assets.resolve(spec)
            except FileNotFoundError as err:
                raise StageFailure(
                    f"no schema file {schema} for {res_type.name} ({err})"
                ) from None
            # the shape must exist for the class of the type, or nothing
            # is checked at all
            try:
                assets.load_shape(source, res_type.class_uri)
            except (ValueError, SyntaxError) as err:
                raise StageFailure(f"cannot use {schema}: {err}") from None
            data = source.read_bytes()
            info = {
                "type": res_type.name,
                "class": res_type.class_uri,
                "schema": schema,
                "checksum": "md5:" + hashlib.md5(data).hexdigest(),
            }
        out = self.output()
        with out["file"].open("w") as handle:  # Turtle is text
            handle.write(data.decode("utf-8"))
        JobTask.write_json(out["info"], info)


class Validate(Stage):
    res_type = luigi.Parameter()
    schema = luigi.Parameter(default="")

    def also_requires(self):
        return {
            "schema": PinSchema(
                job_dir=self.job_dir,
                repo=self.repo,
                res_type=self.res_type,
                schema=self.schema,
            )
        }

    def process(self, items):
        shapes = self.input()["schema"]["file"].path
        owner: dict[str, str] = {}  # blank node label -> the record it is in
        quads: list[ox.Quad] = []
        content: dict[str, bool] = {}
        for item in items:
            found = list(
                ox.parse(item["source"].encode("utf-8"), format=ox.RdfFormat.N_TRIPLES)
            )
            content[item["id"]] = any(q.predicate.value == uri.RESOURCE for q in found)
            for quad in found:
                for term in (quad.subject, quad.object):
                    if isinstance(term, ox.BlankNode):
                        owner[term.value] = item["id"]
            quads.extend(found)
        report = Graph(ox.Dataset(quads)).validate(shapes)
        by_record: dict[str, list[dict]] = {}
        for violation in report.violations:
            label = violation.focus_node
            record = owner.get(label) or owner.get(label.removeprefix("_:")) or label
            by_record.setdefault(record, []).append(
                {
                    "focus": None
                    if record != label and label.startswith("_:")
                    else label,
                    "path": violation.path,
                    "message": violation.message,
                    "severity": violation.severity,
                    "shape": violation.source_shape,
                    "constraint": violation.constraint_component,
                    "value": violation.value,
                }
            )
        passed, rejected = [], []
        for item in items:
            record = item["id"]
            if not content[record]:
                rejected.append({"id": record, "reason": "no_content"})
            elif record in by_record:
                rejected.append(
                    {
                        "id": record,
                        "reason": "violation",
                        "violations": by_record[record],
                    }
                )
            else:
                passed.append(item)
        # nothing rejected: the very same list, so the base class links the file instead
        # of copying
        return (items if not rejected else passed), rejected
