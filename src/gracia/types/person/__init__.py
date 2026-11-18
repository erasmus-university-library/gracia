"""Persons: a content type on `foaf:Person`, the first one for a domain. Its records are generic
records, so there is nothing to register; the schema it starts on is `shacl/Person-minimal-01.ttl`
(a few strings). `shacl/Person-full-01.ttl` is the fuller profile (the name in two parts required,
and mailbox, phone, homepage, picture and knows); switching to it is a change of the type's
`gracia:schemaTemplate`, which the manage page of the type does, after a Validate job has shown what it
would reject."""

TYPE = {
    "name": "Person",
    "slug": "person",
    "class": "http://xmlns.com/foaf/0.1/Person",
    "schema": "Person-minimal-01.ttl",
    "doc": "doc-full-01.xsl",  # the file in doc/ that makes the docs of its records
}
