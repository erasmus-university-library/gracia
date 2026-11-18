"""The ARK extension: the resources of a repository are published under ARKs
(`ark:/<authority number>/<root prefix>/<type>/<number>`, the default `gracia:resource_uri` of a
record, made by its predicate in `predicates/gracia_resource_uri.py`), and `/<repo>/ark:/...` sends a caller to the record.

It is always on and cannot be disabled: the ARKs that were given out have to keep resolving.

The two settings are not editable yet, and that is a gap that is meant to be closed: changing them
is changing the `gracia:resource_uri` of every record, and every place in the graph where one is
used as an object, which is a bulk operation that is not there yet. Until it is, the validators
only match `99999` and `ir` (so the form, an import and `set_settings` refuse anything else and
say so), and the real validators are commented out beside them. The settings a repository was
*made* with are kept (`create_repository(ark=...)`; they are read, not validated). When the bulk
operation exists, the strict validators go and the real ones come back.
The authority number `99999` is the one ARK keeps for tests: ARKs made with it are not persistent
until it is a number that was registered.
"""

import functools

import colander
from pyramid.httpexceptions import HTTPFound, HTTPNotFound
from pyramid.view import view_config

from gracia import uri
from gracia.extensions import Extension, extension
from gracia.resources import Repository


class ArkSettings(colander.MappingSchema):
    schema_type = functools.partial(colander.Mapping, unknown="raise")

    naan = colander.SchemaNode(
        colander.String(),
        title="Authority number",
        description=(
            "The authority number of the ARKs of this repository (fixed for now). 99999 is for "
            "tests: the ARKs are not persistent until it is a number you have registered."
        ),
        default=uri.DEFAULT_NAAN,
        missing=uri.DEFAULT_NAAN,
        validator=colander.Regex(r"^99999$", msg="Only 99999 for now"),
        # validator=colander.Regex(r"^[0-9A-Za-z]{1,32}$", msg="Letters and digits"),
    )
    root_prefix = colander.SchemaNode(
        colander.String(),
        title="Root prefix",
        description=(
            "The first part of the name (fixed for now): "
            "ark:/<authority number>/<root prefix>/<type>/<number>"
        ),
        default=uri.DEFAULT_ROOT_PREFIX,
        missing=uri.DEFAULT_ROOT_PREFIX,
        validator=colander.Regex(r"^ir$", msg="Only ir for now"),
        # validator=colander.Regex(r"^[0-9A-Za-z]{1,32}$", msg="Letters and digits"),
    )


@extension("ark")
class ArkExtension(Extension):
    title = "ARK"
    description = (
        "Persistent identifiers for the resources, ark:/<authority>/<prefix>/<type>/<number>, "
        "and a resolver at /<repository>/ark:/…. Always on."
    )
    settings_schema = ArkSettings

    @property
    def enabled(self) -> bool:
        return True  # whatever was stored

    @property
    def can_disable(self) -> bool:
        return False


@view_config(
    context=Repository, name="ark:", enabled_extension="ark", request_method=("GET", "HEAD")
)
def resolve_ark(request):
    """`/<repo>/ark:/99999/ir/person/34` goes to the record whose resource it names. An ARK in the
    form the repository makes is read as it is; any other (the `gracia:resource_uri` of a record
    that says its own) is looked up. A record the caller may not see is not found."""
    repository = request.context
    settings = repository.extension("ark").settings
    parts = tuple(request.subpath)
    record = None
    if (
        len(parts) == 4
        and parts[:2] == (settings["naan"], settings["root_prefix"])
        and parts[3].isdigit()
    ):
        record = f"rec:{parts[2]}:{int(parts[3])}"
    elif parts:
        record = request.store.record_with_resource_uri("ark:/" + "/".join(parts))
    parsed = uri.parse_record_uri(record) if record else None
    if parsed is None or parsed[0] not in repository.res_types:
        raise HTTPNotFound()
    try:
        target = repository.res_types[parsed[0]].by_number(str(parsed[1]))
    except KeyError:
        raise HTTPNotFound() from None
    return HTTPFound(request.resource_url(target))
