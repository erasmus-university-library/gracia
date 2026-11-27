import json
from datetime import UTC, datetime
from pathlib import Path

SAMPLES = 5  # rejected items kept in the snapshot of a stage


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Snapshotter:
    def __init__(self, job_dir, stages: list[dict]):
        self.dir = Path(job_dir)
        self.stages = stages
        self._seen: dict[Path, dict] = {}

    def plan(self) -> dict | None:
        return _json(self.dir / "plan.json")

    def errors(self) -> list[dict]:
        try:
            lines = (self.dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def _stats(self, index: int, name: str) -> list[dict]:
        found = []
        for path in sorted((self.dir / f"{index:02d}-{name}").glob("stats-*.json")):
            if path not in self._seen:
                data = _json(path)
                if data is None:
                    continue
                self._seen[path] = data
            found.append(self._seen[path])
        return found

    def take(self, active: bool = True, running: dict | None = None) -> dict:
        plan = self.plan()
        failed = {e.get("stage") for e in self.errors()}
        total = plan["batches"] if plan else None
        stages, previous = [], None
        for index, entry in enumerate(self.stages):
            name = entry["stage"]
            stats = self._stats(index, name)
            done = len(stats)
            rejected: dict[str, int] = {}
            for s in stats:
                for reason, count in s.get("rejected", {}).items():
                    rejected[reason] = rejected.get(reason, 0) + count
            samples = [x for s in stats for x in s.get("samples", [])][:SAMPLES]
            now = (running or {}).get(name, 0)
            finished = total is not None and done >= total
            if finished:
                state = "done"
            elif name in failed:
                state = "failed"
            elif active and (
                now > 0
                or (index == 0 and plan is not None)
                or done > 0
                or (previous and previous["batches"]["done"] > 0)
            ):
                state = "running"
            else:
                state = "pending"
            if index == 0:
                items_total, estimated = (plan or {}).get("estimate"), True
            elif previous and previous["state"] == "done":
                items_total, estimated = previous["passed"], False
            else:
                items_total, estimated = None, False
            stage = {
                "name": name,
                "state": state,
                "batches": {"done": done, "running": now, "total": total},
                "processed": sum(s.get("processed", 0) for s in stats),
                "passed": sum(s.get("passed", 0) for s in stats),
                "rejected": rejected,
                "samples": samples,
                "total": items_total,
                "estimated": estimated and items_total is not None,
            }
            stages.append(stage)
            previous = stage
        return {
            "stages": stages,
            "unit": (plan or {}).get("unit", "items"),
            "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
