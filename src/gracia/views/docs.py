import json

from pyramid.httpexceptions import HTTPNotFound
from pyramid.response import Response
from pyramid.view import view_config

from gracia.resources import Record


@view_config(context=Record, name="doc", permission="manage", request_method="GET")
def doc_json(request):
    doc = request.store.get_doc(request.context.uri)
    if doc is None:
        raise HTTPNotFound("this record has no doc yet (it has no etag)")
    return Response(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json",
        charset="utf-8",
        status=500 if "error" in doc else 200,
    )
