from gracia.jobs.job import Job, job


CHUNK_BYTES = 32_000_000  # the graphs are exported in files of about this size: one is loaded at a time


@job("rebuild_etags")
class RebuildEtagsJob(Job):
    title = "Rebuild etags"
    description = (
        "Make the etags of the pages of every record again from the data, and list the records "
        "whose etag was wrong. Writes keep them up to date by themselves: this is for a repository "
        "that has none yet, or whose data was changed outside gracia."
    )
    groups = ()  # it reads every record: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [{"stage": "RebuildEtags", "params": {}}],
        }


@job("update_cache")
class UpdateCacheJob(Job):
    title = "Update cache"
    description = (
        "Make the RDF/XML of the graph of every record again (the record and everything it is made "
        "of) and keep it in the graph cache, so that the pages are made from it. Only records that "
        "have an etag are done: rebuild the etags first in a repository that has none."
    )
    groups = ()  # it reads every record: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [
                {"stage": "Export", "params": {"graphs": True, "chunk_bytes": CHUNK_BYTES}},
                {"stage": "UpdateCache", "params": {}},
            ],
        }


@job("process_change_events")
class ProcessActionsJob(Job):
    title = "Process pending change events"
    description = (
        "Hand every change that is waiting in the queue to what follows the data (the search index, "
        "the graph cache), as the worker does by itself, from files DuckDB writes in one go: for a "
        "repository whose worker is not running, or to catch up after something was down."
    )
    groups = ()  # it reaches every record that changed: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [
                {"stage": "Export", "params": {"queued": True, "chunk_bytes": CHUNK_BYTES}},
                {"stage": "ProcessChangeEvents", "params": {}},
            ],
        }


@job("rebuild_search")
class RebuildSearchJob(Job):
    title = "Rebuild search"
    description = (
        "Make the search indexes of every record again, next to the ones that answer, and let the new ones "
        "take their place when they are done: for an index that is not there yet, that is out of date (its "
        "definition changed), or that lacks records. What is written meanwhile is brought up "
        "to date by the worker afterwards."
    )
    groups = ()  # it reads every record: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [
                {"stage": "Export", "params": {"graphs": True, "chunk_bytes": CHUNK_BYTES}},
                {"stage": "IndexRecords", "params": {}},
            ],
        }
