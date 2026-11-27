import os
import re
from pathlib import Path

from gracia.utils import config

# luigi warns when `core.autoload_range` is not set; gracia does not use range tasks.
# Set before
# luigi is imported (by the modules of this package); a LUIGI_CONFIG_PATH of the
# deployment wins.
os.environ.setdefault("LUIGI_CONFIG_PATH", str(Path(__file__).with_name("luigi.cfg")))

JOB_STATES = ("queued", "running", "stopping", "stopped", "failed", "done")
STAGE_STATES = ("pending", "running", "done", "failed")
TERMINAL = ("stopped", "failed", "done")
ACTIVE = ("queued", "running", "stopping")


def jobs_dir(settings: dict) -> Path:
    chosen = settings.get("gracia.jobs_dir")
    if chosen:
        return Path(chosen)
    database = settings.get("gracia.db.file")
    target = settings.get("gracia.db")
    if (
        not database
        and target
        and not config.is_remote(target)
        and target != ":memory:"
    ):
        database = target
    if not database:
        raise ValueError(
            "where do the jobs keep their files? set gracia.jobs_dir in the config "
            "file "
            "(there is no database file to put them next to)"
        )
    return Path(f"{database}.jobs")


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
DEFAULT_RETENTION = "8h"


def parse_duration(text: str) -> float:
    found = _DURATION.match(str(text).strip())
    if not found:
        raise ValueError(f"not a duration: {text!r} (such as 30m, 8h, 2d)")
    return float(found[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[found[2]]


def retention(settings: dict, override: str | None = None) -> float:
    return parse_duration(
        override or settings.get("gracia.jobs_retention") or DEFAULT_RETENTION
    )
