-- One row per record (a subject rec:<type>:<n>; other subjects have none), kept by triggers on the
-- triples table: any insert, update or delete of its rows rewrites the whole row, `source` included;
-- a record that has no rows any more has no row (the history has it, as deleted). Run after macros.sql.
--   record_id   : the subject rec:<type>:<n> as a 32 bit INTEGER, type number * 10^7 + n (`record_id(uri)`
--                 and `record_uri(id)` go from one to the other, `type_slug(number)` is the slug of
--                 a type): the type number 0 to 99, n 1 to 9,999,999. A record of a type that has no
--                 number, or with a number outside that, cannot be stored
--   rec_type    : the slug of its type (the number of the record is `record_id % 10000000`)
--   res_uri     : gracia:resource_uri
--   slug        : the current slug, gracia:slug of the record (not of a type's content)
--   created_at  : gracia:created_at, when the record was first saved (UTC)
--   modified_at : gracia:modified_at, when the rows last changed (UTC, the time of the transaction):
--                 the same value as the property, made by its predicate; a record that has none
--                 gets the time of the transaction
--   is_published: gracia:is_published (true when there is none)
--   label       : gracia:label
--   identifiers : the values of the properties of the record that have the role KeyInfoRole
--                 in the shape of its type (`property_roles`, below)
--   "references": every uri that is the object of a row of the resource of the record (its content, with the
--                 blank nodes below it), which are the resource uris of other records, or other uris: a
--                 resource never points at a record (a `rec:` object there is refused)
--   text        : the literals of the properties with the role DescriptionRole, joined by newlines
--   source      : the rows of the record as N-Triples (blank nodes are `b<record id>_<n>`)
--   bytes       : the size of `source` in bytes: what a batch of work is measured in (`etags.closure_bytes`)
--   state       : gracia:state, a word (NULL when the record has none). The last column (the
--                 order of the columns is the order of `record_rows`)
-- The triggers are made by the store (`Store._record_triggers`). Statement-level triggers only (DuckDB has no row-level update triggers), and MERGE INTO is
-- refused on a table that has triggers: loads and deletes are DELETE and INSERT.
CREATE TABLE IF NOT EXISTS @RECORDS@ (
    record_id   INTEGER PRIMARY KEY,
    rec_type    VARCHAR NOT NULL,
    res_uri     VARCHAR,
    slug        VARCHAR,
    created_at  TIMESTAMP NOT NULL,
    modified_at TIMESTAMP NOT NULL,
    is_published BOOLEAN NOT NULL,
    label       VARCHAR,
    identifiers VARCHAR[] NOT NULL,
    "references" VARCHAR[] NOT NULL,
    text        VARCHAR,
    source      VARCHAR NOT NULL,
    bytes       INTEGER NOT NULL,
    state       VARCHAR
);


-- The list tables: one row per value of `identifiers` and of "references", so a lookup by value is a
-- scan of one column (DuckDB cannot index a list). The triggers keep them in step with `records`:
-- delete the rows of the changed records, then insert the current ones. No index on them, and none
-- must be made but to enforce a unique constraint: an index makes the commit of every large write
-- slower (measured: 2.8 s against 1.2 s for 300,000 records), and the commit is what holds up the
-- commits of every other repository. The scans cost 1 to 5 ms at 300,000 records.
CREATE TABLE IF NOT EXISTS @RECORDS@_identifiers (identifier VARCHAR NOT NULL, record_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS @RECORDS@_references (reference VARCHAR NOT NULL, record_id INTEGER NOT NULL);
-- The ids of the records that a statement on the triples changed, which the triggers make first and every
-- later step reads, so that the subjects of the (many) changed rows are turned into ids once (empty when the
-- statement is done).
CREATE TABLE IF NOT EXISTS @RECORDS@_changes (record_id INTEGER NOT NULL);

-- What the shapes of the types say about their properties, kept here so that the triggers can use it
-- (the store is given it by the repository, see `Store.set_property_roles`):
--   role: label | key_info | description | unique   (dash:LabelRole, dash:KeyInfoRole,
--         dash:DescriptionRole, dash:uniqueValueForClass), ord: their sh:order
CREATE TABLE IF NOT EXISTS property_roles (
    rec_type VARCHAR NOT NULL, role VARCHAR NOT NULL, p VARCHAR NOT NULL, ord INTEGER NOT NULL
);
-- The values that may be used by one record of a type only (a userid): the database refuses the second
-- record that has one, whoever writes it, and a rolled back transaction leaves nothing behind.
CREATE TABLE IF NOT EXISTS unique_values (
    rec_type VARCHAR NOT NULL, p VARCHAR NOT NULL, value VARCHAR NOT NULL, s VARCHAR NOT NULL,
    UNIQUE (rec_type, p, value)
);

-- What changed, transaction by transaction (see history.py): every record that a statement changed,
-- as `records` has it after the statement, with the number of the transaction (0: written outside the
-- store). One row for a record and a transaction. The numbers only go up, so the rows of a
-- transaction are together at the end of the table, and a reader that has seen up to a number reads the
-- rest by `txn_id > number`.
CREATE TABLE IF NOT EXISTS history (
    record_id   INTEGER NOT NULL,
    txn_id      BIGINT NOT NULL,
    modified_at TIMESTAMP NOT NULL,
    is_deleted  BOOLEAN NOT NULL,
    source      VARCHAR NOT NULL
);

-- What the things that follow the data (the search index, the graph cache: see `gracia.change_events`) still have
-- to do, one row for a record and a transaction, written by the same triggers as the history, in the
-- transaction of the write. `kind` is `insert` (the record is new, or was deleted before), `update`,
-- `delete`, or `ref_update` (the record was not written, but its page changed because something it is made
-- of did: `Store.refresh_etags`). A row says that a record changed, not how: whoever handles it reads the
-- record as it is then. No index: the rows are appended in the order of the transactions, and taken from the
-- front 100 at a time.
CREATE TABLE IF NOT EXISTS change_events (
    transaction_id BIGINT NOT NULL,
    record_id      INTEGER NOT NULL,
    kind           VARCHAR NOT NULL
);

-- Every slug a record has had, for ever (see predicates/gracia_slug.py): a slug is a name for a record
-- in the address of its type (`/person/john-doe`), and an old one keeps working. A slug is of one type
-- (`rec_type`); the record that has it is `record_uri`, and `is_deleted` says that record is gone. A
-- record that is deleted also gets its number as a slug (`23`, flagged deleted), so that the address
-- of a number that was used can be told from one that never was. Nothing is ever removed.
-- `modified_at` is when the row was made or last flagged (UTC, the time of the transaction): of the
-- number slug of a deleted record it is when the record was deleted, which is what a listing of
-- the deleted records of a type sorts on (`Store.referencing_records`).
CREATE TABLE IF NOT EXISTS slugs (
    rec_type   VARCHAR NOT NULL,
    slug       VARCHAR NOT NULL,
    record_uri VARCHAR NOT NULL,
    is_deleted BOOLEAN NOT NULL DEFAULT false,
    modified_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    PRIMARY KEY (rec_type, slug)
);
