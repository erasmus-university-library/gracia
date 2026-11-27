import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from gracia.jobs.db import JobsDb


@dataclass
class CleanReport:
    jobs: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    bytes: int = 0
    dry_run: bool = False


def safe_job_dir(jobs_dir: Path, name: str) -> Path | None:
    try:
        if str(uuid.UUID(name)) != name:
            return None
    except ValueError:
        return None
    path = jobs_dir / name
    if path.is_symlink() or not path.is_dir():
        return None
    if path.resolve().parent != jobs_dir.resolve():
        return None
    return path


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def clean(db: JobsDb, jobs_dir: Path, older_than: float, dry_run: bool = False) -> CleanReport:
    report = CleanReport(dry_run=dry_run)
    for job in db.finished_before(older_than):
        directory = safe_job_dir(jobs_dir, job["id"])
        size = _size(directory) if directory else 0
        if dry_run:
            report.jobs.append(job["id"])
            report.bytes += size
            continue
        if db.delete(job["id"]):
            report.jobs.append(job["id"])
            report.bytes += size
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
    if jobs_dir.is_dir():
        known = {job["id"] for job in db._rows("SELECT id FROM _gracia.jobs")}
        cutoff = time.time() - older_than
        for entry in jobs_dir.iterdir():
            directory = safe_job_dir(jobs_dir, entry.name)
            if directory is None or entry.name in known or directory.stat().st_mtime > cutoff:
                continue
            report.orphans.append(entry.name)
            report.bytes += _size(directory)
            if not dry_run:
                shutil.rmtree(directory, ignore_errors=True)
    return report
