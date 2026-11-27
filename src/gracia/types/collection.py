from gracia.types.type import ContentType, Record


class CollectionRecord(Record):
    """The node of a collection: nothing of its own yet."""

    content_type = ContentType(
        name="Collection",
        slug="collection",
        class_uri="https://schema.org/Collection",
        schema="collection-schemaorg-01.ttl",
    )
