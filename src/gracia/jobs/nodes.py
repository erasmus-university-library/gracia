"""The nodes of the jobs of a repository: `jobs` (the list and the start of jobs) and
below it a run of a job (`JobRun`)."""

import re

from pyramid.authorization import DENY_ALL, Allow

from gracia.security import ADMINS
from gracia.interfaces import Node, traversable

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@traversable("jobs")
class Jobs(Node):
    __name__ = "jobs"

    def __init__(self, repository):
        super().__init__(repository)

    @property
    def __acl__(self):
        from gracia.jobs.job import all_jobs

        grants: dict[str, list[str]] = {}
        for job in all_jobs(self.registry):
            for group in job.groups:
                grants.setdefault(f"group:{group}", ["list_jobs"]).append(
                    job.start_permission
                )
        return [
            ADMINS,
            *((Allow, group, perms) for group, perms in sorted(grants.items())),
            DENY_ALL,
        ]

    def __getitem__(self, job_id: str) -> "JobRun":
        if not _UUID.match(job_id):
            raise KeyError(job_id)
        from gracia.jobs.db import JobsDb

        with JobsDb(self.application) as db:
            row = db.get(job_id)
        if row is None or row["repo"] != self.repository.__name__:
            raise KeyError(job_id)
        return self.run(row)

    def run(self, row: dict) -> "JobRun":
        run = JobRun(self.repository, row)
        run.__parent__ = self
        return run


class JobRun(Node):
    def __init__(self, repository, row: dict):
        self.__name__ = row["id"]
        super().__init__(repository)
        self.row = row

    @property
    def __acl__(self):
        creator = self.row[
            "created_by"
        ]  # `user:<userid>`; None for a job from the command line
        return [
            *([(Allow, creator, ["view_job", "stop_job"])] if creator else []),
            ADMINS,
            DENY_ALL,
        ]
