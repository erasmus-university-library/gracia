"""`<record>/doc`: the doc of a record, the JSON that what follows the data (the search index) knows it by.

It comes from the doc cache when the doc there was made from the record as it is now and by the
stylesheets as they are now; otherwise it is made from the graph of the record by the doc stylesheet of the
repository (`gracia.docs`) and kept. For whoever may manage (`manage`).

    GET .../doc      the doc, as JSON (500 with `{"record", "error"}` when it cannot be made)
"""

import json

from pyramid.httpexceptions import HTTPNotFound
from pyramid.response import Response
from pyramid.view import view_config

from gracia.resources import Record


@view_config(context=Record, name="doc", permission="manage", request_method="GET")
def doc_json(request):
    record = request.context
    record_id = request.store.record_id_of(record.uri)
    doc = request.root.cache.doc(record.repository.__name__, record_id, record.uri) if record_id is not None else None
    if doc is None:
        raise HTTPNotFound("this record has no doc yet (it has no etag)")
    return Response(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json",
        charset="utf-8",
        status=500 if "error" in doc else 200,
    )
