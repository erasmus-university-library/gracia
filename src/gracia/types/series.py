from gracia.types.type import ContentType, Record


class SeriesRecord(Record):
    """The node of a series: nothing of its own yet."""

    content_type = ContentType(
        name="Series",
        slug="series",
        class_uri="https://schema.org/CreativeWorkSeries",
        schema="series-schemaorg-01.ttl",
    )
