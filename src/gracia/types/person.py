from gracia.types.type import ContentType, Record


class PersonRecord(Record):
    """The node of a person: nothing of its own yet."""

    content_type = ContentType(
        name="Person",
        slug="person",
        class_uri="https://schema.org/Person",
        schema="person-schemaorg-01.ttl",
        doc="person-schemaorg-01.xsl",
    )
