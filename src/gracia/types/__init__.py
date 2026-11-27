"""The types of records: a module for each (`person.py`), with the node of its records
(`PersonRecord`, which declares the type), and the views of them when they have any, and the
modules that are no type of their own: `application` and `repository` hold the nodes of
those, `type` the node of a type and the base classes of every record. A class of records
that implements `IRecord` and declares a `content_type` is found by `gracia.scan`."""
