-- The etags of the pages of the records, made when something is written (see `Store.refresh_etags`)
-- so that a request reads one row and does not walk the references. Derived data: `rebuild_etags`
-- makes all of it again from `records` and `records_references`. Idempotent; run after records.sql.
--
--   record_id        : the id of the record, the one of `records` (records.sql): a 32 bit INTEGER, type
--                      number * 10^7 + record number. The number of a type is the number of its type
--                      record (rec:type:<n>), told to the database by the repository (`record_id`). Python makes the same id (`gracia.uri.record_id`); the ids stay
--                      as they are for ever, record numbers are never used twice
--   record_uri       : the record
--   record_type      : the slug of its type
--   res_uri          : its gracia:resource_uri (a reference to it can be by that uri)
--   sort_key         : what a listing orders by: when the record was created, in microseconds since the
--                      epoch (a number: the zone maps of a text column keep only a short prefix, which
--                      cannot tell timestamps of one day apart). An edit does not change it, so the
--                      pages of a listing do not move when a record is edited
--   is_published     : gracia:is_published
--   etag             : the XOR of the `local_hash` of the record and of every record it points at
--                      (`deep_references`), an unsigned 64 bit integer (the page shows it as 16 hex
--                      digits; the version of the schema files of the types is added when a page is
--                      made). XOR over a set counts each record once and has no order, so a record
--                      that changes without changing what it points at changes the etag of every
--                      page made of it by `old_local XOR new_local`, without walking anything
--   local_hash       : hash(record_id, modified_at): the part of the etags of the pages that the
--                      record itself is
--   modified_at      : when the record itself last changed (the time of a write, never set from outside)
--   last_modified_at : the latest modification of the record and of those records, and the time of the
--                      write that last changed the etag. Like the time of a record it only goes up: a
--                      page that loses a record it was made of (deleted, or another resource uri) gets
--                      the time of that write, so the etag and the time change together, and a time that
--                      was read says whether a page is newer (the etag says whether it is different)
--   bytes            : the size of the record's own `source` (`records.bytes`)
--   closure_bytes    : the size of the sources of the record and of every record it is made of: what
--                      reading its graph costs, so that a batch of work can be measured in bytes
--   "references"     : the ids of the records it points at, directly: an object of its resource that is
--                      the resource uri of a record
--   deep_references  : the ids of every record reached by following those, the record itself left out
--   deep_types       : the slugs of the types of the record and of those records
--   backref_totals   : how many records point at this one, directly, by the type of the record that
--                      does: {type slug: {total_all, total_published}}. It is read with the row
--                      (one lookup gives the etag and the number of pages of its listings) and kept
--                      by changes: a write that gives a record another set of references, or another
--                      published flag, or makes or removes it, adds and takes off one for each record
--                      it points at, and nothing else is counted again (`Store._backref_changes`)
--
-- The ids are in the arrays so that "which records are made of this one" is a scan of one column
-- (`list_contains`), the way a write finds the etags it has to make again.
CREATE TABLE IF NOT EXISTS etags (
    record_id        INTEGER NOT NULL,
    record_uri       VARCHAR NOT NULL,
    record_type      VARCHAR NOT NULL,
    res_uri          VARCHAR,
    sort_key         BIGINT NOT NULL,
    is_published     BOOLEAN NOT NULL,
    etag             UBIGINT NOT NULL,
    local_hash       UBIGINT NOT NULL,
    modified_at      TIMESTAMP NOT NULL,
    last_modified_at TIMESTAMP NOT NULL,
    "references"     INTEGER[] NOT NULL,
    deep_references  INTEGER[] NOT NULL,
    deep_types       VARCHAR[] NOT NULL,
    backref_totals   MAP(VARCHAR, STRUCT(total_all BIGINT, total_published BIGINT)) NOT NULL,
    bytes            INTEGER NOT NULL,
    closure_bytes    BIGINT NOT NULL
);
-- The table is kept in the order a listing reads it, newest created first (`record_type`, `sort_key`
-- DESC, `record_id`): every write sorts it again before it commits (`Store._sort_etags`), so that
-- `Store.referrers` can read it with LIMIT and no ORDER BY, and stop after one page.
--
-- No index on it, and none must be made: a write that recomputes many etags deletes and inserts that many
-- rows, and with an ART index the commit of that (measured: 746,000 rows, 5.6 s of a 9.4 s write) is
-- what holds up the commits of every other repository of the database. A lookup by `record_id` scans
-- one column of integers, 0.5 ms for a million records, as fast as the index was.

-- what a refresh works on, in the transaction of the write (emptied when it is done)
CREATE TABLE IF NOT EXISTS etags_seed (record_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS etags_todo (record_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS etags_refs (record_id INTEGER NOT NULL, refs INTEGER[] NOT NULL);
-- the etags as they were, while `rebuild_etags` makes them again (to tell which were wrong)
CREATE TABLE IF NOT EXISTS etags_before (record_uri VARCHAR NOT NULL, etag UBIGINT NOT NULL, last_modified_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS etags_new (
    record_id INTEGER NOT NULL, record_uri VARCHAR NOT NULL, record_type VARCHAR NOT NULL,
    res_uri VARCHAR, sort_key BIGINT NOT NULL, is_published BOOLEAN NOT NULL, etag UBIGINT NOT NULL,
    local_hash UBIGINT NOT NULL, modified_at TIMESTAMP NOT NULL, last_modified_at TIMESTAMP NOT NULL,
    "references" INTEGER[] NOT NULL,
    deep_references INTEGER[] NOT NULL, deep_types VARCHAR[] NOT NULL,
    backref_totals MAP(VARCHAR, STRUCT(total_all BIGINT, total_published BIGINT)) NOT NULL,
    bytes INTEGER NOT NULL, closure_bytes BIGINT NOT NULL
);
-- for a record that a write touched (a seed): what its local hash was and is, what that changes
-- (`delta`) and whether what it points at changed (`struct`: also a record that is new or gone); then,
-- for the records made of seeds, what to XOR into their etag and whether they have to be walked
-- again (`bad`)
CREATE TABLE IF NOT EXISTS etags_delta (
    record_id INTEGER NOT NULL, old_local UBIGINT, new_local UBIGINT, delta UBIGINT NOT NULL,
    struct BOOLEAN NOT NULL, new_modified TIMESTAMP, bytes_delta BIGINT NOT NULL
);
-- the changes of the totals of the records that were pointed at or are not now, and the totals of those
-- records as they are when the write is done (`etags_backnew`, put in the table when it is sorted)
CREATE TABLE IF NOT EXISTS etags_backdelta (
    record_id INTEGER NOT NULL, source_type VARCHAR NOT NULL, d_all BIGINT NOT NULL, d_pub BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS etags_backnew (
    record_id INTEGER NOT NULL, backref_totals MAP(VARCHAR, STRUCT(total_all BIGINT, total_published BIGINT)) NOT NULL
);
CREATE TABLE IF NOT EXISTS etags_holders (
    record_id INTEGER NOT NULL, delta UBIGINT NOT NULL, bad BOOLEAN NOT NULL, newest TIMESTAMP,
    bytes_delta BIGINT NOT NULL
);

-- record_graph(): one row per record of `etags`, with the record and everything its page is made of as
-- one TriG graph, named by the record: the N-Triples `source` of the record itself, then of every record it
-- points at (`references` and `deep_references`, by record id), joined by a newline. Filter it like a view:
--     SELECT * FROM record_graph() WHERE record_id = 123
--     SELECT * FROM record_graph() WHERE record_type = 'work' AND last_modified_at > TIMESTAMP '...'
-- A filter on the columns of `etags` is applied before the sources are read, so only the rows asked for
-- cost anything. It is a macro and not a view because the table `etags` is made again by every write
-- (`Store._sort_etags`), which a view on it would stop. The blank nodes of a source are labelled per record
-- (uuids), so a record that is in several graphs has the same blank nodes in each.
CREATE OR REPLACE MACRO record_graph() AS TABLE
    WITH picked AS (
        SELECT record_id, record_uri, record_type, etag, last_modified_at,
               unnest(list_distinct(list_concat([record_id], "references", deep_references))) AS part
        FROM etags
    )
    SELECT p.record_id, p.record_uri, p.record_type, p.etag, p.last_modified_at,
           encode_ntriples_uri(p.record_uri) || ' {' || chr(10)
           || string_agg(r.source, chr(10) ORDER BY p.part <> p.record_id, p.part)
           || chr(10) || '}' AS graph
    FROM picked p
    JOIN @RECORDS@ r ON r.record_id = p.part
    GROUP BY p.record_id, p.record_uri, p.record_type, p.etag, p.last_modified_at;
