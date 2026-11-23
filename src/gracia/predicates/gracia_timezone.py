from zoneinfo import available_timezones

import colander
import deform
import deform.widget

from gracia import uri
from gracia.predicates import Predicate, predicate

# the regions of the IANA names people choose from; the legacy aliases (US/Eastern,
# EST5EDT,
# Etc/...)
# are left out, they are the same zones under older names
REGIONS = (
    "Africa",
    "America",
    "Antarctica",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)


class TimezoneWidget(deform.widget.SelectWidget):
    def __init__(self, **kw):
        names = sorted(
            n
            for n in available_timezones()
            if n.split("/", 1)[0] in REGIONS and "/" in n
        )
        values = [("", "(none: UTC)")] + [
            deform.widget.OptGroup(
                region,
                *[
                    (n, n.split("/", 1)[1].replace("_", " "))
                    for n in names
                    if n.startswith(region + "/")
                ],
            )
            for region in REGIONS
        ]
        super().__init__(values=values, **kw)


def valid_zone(node, value) -> None:
    if value and not (
        value.split("/", 1)[0] in REGIONS
        and "/" in value
        and value in available_timezones()
    ):
        raise colander.Invalid(node, "Not a time zone")


@predicate(uri.TIMEZONE)
class TimezonePredicate(Predicate):
    """`gracia:timezone`: the zone times are shown in, or none for UTC."""

    def schema_node(self, record, request, schema_node=None):
        if schema_node is None:
            return schema_node
        schema_node.widget = TimezoneWidget()
        schema_node.validator = valid_zone
        return schema_node
