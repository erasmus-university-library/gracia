from __future__ import annotations

import importlib
import json
import os
import traceback
from collections import Counter
from importlib.metadata import entry_points
from pathlib import Path

import luigi
from luigi.local_target import LocalTarget
from luigi.task_register import Register

BUILTIN = "gracia.jobs.builtin"
ENTRY_POINT_GROUP = "gracia.stages"
SAMPLES = 3  # rejected items shown in the status, per batch


class StageFailure(Exception):
    pass


class JobTask(luigi.Task):

    job_dir = luigi.Parameter()
    repo = luigi.Parameter()

    @staticmethod
    def dumps(stages: list) -> str:
        return json.dumps(stages, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def read_lines(path) -> list[dict]:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    @staticmethod
    def read_graphs(text: str) -> list[dict]:
        items, etag, kind, block = [], None, None, None
        for line in text.split("\n"):
            if block is not None:
                if line == "}":
                    items.append(
                        {"id": block[0][1:-3], "etag": etag, "kind": kind, "source": "\n".join(block[1:]) + "\n"}
                    )
                    block = None
                else:
                    block.append(line)
            elif line.startswith("# "):
                etag, _, kind = line[2:].partition(" ")
                kind = kind or None
            elif line.startswith("<") and line.endswith(" {"):
                block = [line]
        return items

    @staticmethod
    def read_items(path) -> list[dict]:
        if str(path).endswith(".trig"):
            return JobTask.read_graphs(Path(path).read_text(encoding="utf-8"))
        if not str(path).endswith(".nt"):
            return JobTask.read_lines(path)
        text = Path(path).read_text(encoding="utf-8")
        items = []
        for block in text.split("\n\n"):
            block = block.strip("\n")
            if block:
                subject = block.split(" ", 1)[0]
                items.append({"id": subject[1:-1], "source": block + "\n"})
        return items

    @staticmethod
    def write_lines(target: LocalTarget, items) -> None:
        with target.open("w") as handle:  # a temporary file that is renamed: never half written
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def write_json(target: LocalTarget, data) -> None:
        with target.open("w") as handle:
            json.dump(data, handle)

    @staticmethod
    def link_or_copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.tmp{os.getpid()}")
        temporary.unlink(missing_ok=True)
        try:
            os.link(source, temporary)
        except OSError:
            temporary.write_bytes(source.read_bytes())
        os.replace(temporary, target)


@JobTask.event_handler(luigi.Event.FAILURE)
def _failed(task, exception):
    message = (
        str(exception)
        if isinstance(exception, StageFailure)
        else (f"{type(exception).__name__}: {exception}")
    )
    record = {
        "stage": type(task).__name__,
        "n": getattr(task, "n", None),
        "error": message,
        "traceback": "".join(traceback.format_exception(exception))[-4000:],
    }
    with open(Path(task.job_dir) / "errors.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


class Stage(JobTask):
    n = luigi.IntParameter()  # the batch
    chain = luigi.Parameter()  # JSON: the stages up to and including this one

    extension = "jsonl"  # of the file of the items that pass on: `nt` for records as N-Triples
    first = False  # a source: nothing comes before it, `plan` says how many batches there are
    abstract = True  # a base class: not a stage a job can name

    @property
    def stages(self) -> list:
        return json.loads(self.chain)

    @property
    def index(self) -> int:
        return len(self.stages) - 1

    # ---- what a stage author may override ----------------------------------------------------

    def process(self, items):
        raise NotImplementedError

    def plan(self) -> dict:
        raise NotImplementedError

    def also_requires(self) -> dict:
        return {}

    # ---- the wiring -----------------------------------------------------------------------------

    def requires(self):
        needed = dict(self.also_requires())
        if self.first:
            from gracia.jobs.builtin import Plan  # the plan is a task of its own: done once

            needed["upstream"] = Plan(
                job_dir=self.job_dir, repo=self.repo, first=self.dumps(self.stages[0])
            )
        else:
            needed["upstream"] = self.make(
                self.job_dir, self.repo, self.stages[: self.index], self.n
            )
        return needed

    def directory(self) -> Path:
        return Path(self.job_dir) / f"{self.index:02d}-{type(self).__name__}"

    def output(self):
        d, n = self.directory(), self.n
        return {
            "passed": LocalTarget(str(d / f"batch-{n:05d}.{self.extension}")),
            "rejected": LocalTarget(str(d / f"rejected-{n:05d}.jsonl")),
            "stats": LocalTarget(str(d / f"stats-{n:05d}.json")),
        }

    def plan_data(self) -> dict:
        found = self.input()["upstream"]
        with open(found.path, encoding="utf-8") as handle:
            return json.load(handle)

    def run(self):
        upstream = self.input()["upstream"]
        items = None if self.first else self.read_items(upstream["passed"].path)
        passed, rejected = self.process(items)
        out = self.output()
        reasons = Counter(r.get("reason", "rejected") for r in rejected)
        samples = [
            {"id": r.get("id"), "reason": r.get("reason"), "message": self._message(r)}
            for r in rejected[:SAMPLES]
        ]
        self.write_lines(out["rejected"], rejected)
        processed = len(items) if items is not None else len(passed) + len(rejected)
        self.write_json(
            out["stats"],
            {
                "processed": processed,
                "passed": len(passed),
                "rejected": dict(reasons),
                "samples": samples,
            },
        )
        unchanged = (
            items is not None
            and not rejected
            and (passed is items or passed == items)
            and str(upstream["passed"].path).endswith(f".{self.extension}")
        )
        if unchanged:
            self.link_or_copy(Path(upstream["passed"].path), Path(out["passed"].path))
        else:  # the data is written last: a task is only done when all three files are there
            self.write_lines(out["passed"], passed)


    # ---- finding stages by name

    @staticmethod
    def named(name: str) -> type[Stage]:
        try:
            found = Register.get_task_cls(name)
        except Exception as err:
            raise KeyError(name) from err
        if not (isinstance(found, type) and issubclass(found, Stage)) or found.__dict__.get("abstract"):
            raise KeyError(name)
        return found

    @staticmethod
    def known() -> list[str]:
        names = []
        for name in Register.task_names():
            try:
                Stage.named(name)
            except KeyError:
                continue
            names.append(name)
        return sorted(names)

    @staticmethod
    def make(job_dir, repo: str, stages: list, n: int) -> Stage:
        entry = stages[-1]
        cls = Stage.named(entry["stage"])
        return cls(job_dir=str(job_dir), repo=repo, n=n, chain=JobTask.dumps(stages), **entry.get("params", {}))

    @staticmethod
    def load_modules(settings: dict | None = None) -> list[str]:
        names = [BUILTIN]
        names += [entry.value.split(":")[0] for entry in entry_points(group=ENTRY_POINT_GROUP)]
        names += (settings or {}).get("gracia.stages", "").split()
        names = list(dict.fromkeys(names))
        for name in names:
            importlib.import_module(name)
        return names

    @staticmethod
    def _message(rejected: dict) -> str | None:
        if rejected.get("message"):
            return rejected["message"]
        violations = rejected.get("violations") or []
        return violations[0].get("message") if violations else None
