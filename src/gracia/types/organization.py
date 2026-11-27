from gracia.types.type import ContentType, Record


class OrganizationRecord(Record):
    """The node of a organization: nothing of its own yet."""

    content_type = ContentType(
        name="Organization",
        slug="organization",
        class_uri="https://schema.org/Organization",
        schema="organization-schemaorg-01.ttl",
    )
