import difflib
import json

import pyoxigraph as ox
from pyramid.httpexceptions import HTTPBadRequest, HTTPFound, HTTPNotFound
from pyramid.response import Response
from pyramid.view import view_config

from gracia import static
from gracia.graph import Graph
from gracia.resources import Record
from gracia.store import DuplicateValue
from gracia.views import View, flash, last_modified, same_origin

SHOWN = "%Y-%m-%d %H:%M:%S UTC"


class HistoryView(View):
    template = "gracia:assets/templates/history.pt"

    def __init__(self, request):
        super().__init__(request)
        if not self.context.source_editable:
            raise HTTPNotFound()
        self.record = self.context

    def _turtle(self, source: str) -> str:
        if not source:
            return ""
        graph = Graph(
            ox.Dataset(ox.parse(source.encode("utf-8"), format=ox.RdfFormat.N_TRIPLES))
        )
        return graph.serialize("turtle", self.repository.prefix_map())

    def _txn(self) -> int:
        try:
            return int(self.request.params["txn"])
        except (KeyError, ValueError):
            raise HTTPBadRequest("a version is a number") from None

    @view_config(
        context=Record, name="history", permission="edit", request_method="GET"
    )
    def history(self):
        if "txn" in self.request.params:
            return self.version()
        request, record = self.request, self.record
        revisions = self.store.revisions(record.uri)
        shown = [
            {
                "txn": txn,
                "when": when.strftime(SHOWN),
                "ago": last_modified(when)
                .removeprefix("Last modified ")
                .removeprefix("on "),
                "deleted": deleted,
                "current": index == 0 and not deleted,
                "can_restore": index > 0 and not deleted,
                "view": request.resource_url(record, "history", query={"txn": txn}),
                "restore": request.resource_url(record, "restore"),
            }
            for index, (txn, when, deleted) in enumerate(revisions)
        ]
        static.history_js.need()
        static.manage_js.need()  # the question before a restore (`data-confirm`)
        return self.render(
            heading=record.title,
            subtitle=record.uri,
            revisions=shown,
            back=request.resource_url(record, "edit"),
        )

    def version(self) -> Response:
        txn = self._txn()
        found = self.store.revision(self.record.uri, txn)
        if found is None:
            raise HTTPNotFound()
        when, deleted, source = found
        text = self._turtle(source)
        before = self.store.revision_before(self.record.uri, txn)
        diff = None
        if before is not None:
            earlier = self.store.revision(self.record.uri, before)
            previous = self._turtle(earlier[2]) if earlier else ""
            diff = "\n".join(
                difflib.unified_diff(
                    previous.splitlines(),
                    text.splitlines(),
                    f"version {before}",
                    f"version {txn}",
                    lineterm="",
                    n=3,
                )
            )
        body = {
            "txn": txn,
            "when": when.strftime(SHOWN),
            "deleted": deleted,
            "source": text,
            "diff": diff,
        }
        response = Response(
            json.dumps(body), content_type="application/json", charset="utf-8"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @view_config(
        context=Record, name="restore", permission="edit", request_method="POST"
    )
    def restore(self):
        same_origin(self.request)
        record, store = self.record, self.store
        found = store.revision(record.uri, self._txn())
        if found is None or found[1]:
            # there is no such version, or it is a record that was deleted
            raise HTTPNotFound()
        when, _deleted, source = found
        record.graph = Graph(
            ox.Dataset(ox.parse(source.encode("utf-8"), format=ox.RdfFormat.N_TRIPLES))
        )
        try:
            record.save()  # a new version, made by the same path as every save
        except DuplicateValue as err:  # a value that was free then and is taken now
            flash(self.request, f"It cannot be restored: {err}", "danger")
            return HTTPFound(self.request.resource_url(record, "history"))
        flash(self.request, f"Restored the version of {when.strftime(SHOWN)}.")
        return HTTPFound(
            self.request.resource_url(record, "edit", query={"saved": "1"})
        )
