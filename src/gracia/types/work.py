from pyramid.view import view_config

from gracia.types.type import (
    RECORD_TEMPLATE,
    ContentType,
    Record,
    RecordView,
)


class WorkRecord(Record):
    """The node of a work: nothing of its own yet."""

    content_type = ContentType(
        name="Work",
        slug="work",
        class_uri="https://schema.org/CreativeWork",
        schema="work-schemaorg-01.ttl",
        doc="work-schemaorg-01.xsl",
    )


class WorkView(RecordView):
    @view_config(
        context=WorkRecord,
        request_method="GET",
        accept="text/html",
        renderer=RECORD_TEMPLATE,
    )
    def index(self) -> dict:
        """The values of the page of a record, and the citation of the work when the
        extension is enabled: every work has one then (it is kept in its doc)."""
        values = super().index()
        try:
            citations = self.context.repository.extension("citations")
        except KeyError:  # not known to this application
            return values
        if citations.enabled:
            doc = self.request.store.get_doc(self.context.uri)
            if doc is not None and isinstance(doc.get("citation"), str):
                values["citation"] = doc["citation"]
        return values
