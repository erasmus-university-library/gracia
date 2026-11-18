-- Table macros, stored in the database file (re-run this file to update them).
--   parse_triples(file)   json-triples file -> raw rows (s, s_type, p, o, o_type, o_lang, o_dt)
-- The next two work on a staging table (by name) with exactly those raw columns. gracia fills
-- it with a streaming appender (rdf files) or with parse_triples (json-triples files):
--   check_triples(staging)   one row per problem found (self-loops, bnodes with several parents)
--   flatten_triples(staging) rows in the triples table layout (s, loc, p, o, lit, lang, dt)
--   triples_rows(tbl [, uris := NULL])
--                         triples table (by name) -> one row per rebuilt triple
--                         (s, s_type, p, o, o_type, o_lang, o_dt), bnodes and rdf lists
--                         expanded again. With uris (a list): only those resources.
--   export_triples(tbl [, uris := NULL])  the same as one row holding json-triples
-- json-triples = raptor's json-triples shape.
-- Usage (see sql/insert.sql, sql/merge.sql):
--   CREATE TABLE staging AS SELECT * FROM parse_triples('data.json');   -- or fill it yourself
--   INSERT INTO triples SELECT * FROM flatten_triples('staging');

CREATE OR REPLACE MACRO parse_triples(file) AS TABLE
    WITH raw AS (
        SELECT unnest(triples) AS t FROM read_json(file,
            -- the whole file is ONE json object (raptor's shape); the default limit is 16 MB
            maximum_object_size = 2000000000,
            columns = {triples: 'STRUCT(
                subject STRUCT(value VARCHAR, type VARCHAR),
                predicate STRUCT(value VARCHAR),
                object STRUCT(value VARCHAR, type VARCHAR, lang VARCHAR, datatype VARCHAR))[]'})
    )
    SELECT t.subject.value AS s, t.subject.type AS s_type,
           t.predicate.value AS p,
           t.object.value AS o, t.object.type AS o_type,
           t.object.lang AS o_lang, t.object.datatype AS o_dt
    FROM raw;

CREATE OR REPLACE MACRO check_triples(staging) AS TABLE
    WITH tr AS (SELECT * FROM query_table(staging))
    SELECT 'self-loop: ' || s || ' <' || p || '> ' || o AS warning
    FROM tr WHERE o_type = 'bnode' AND s = o
    UNION ALL
    SELECT 'bnode ' || o || ' has ' || count(*) || ' parents (shared bnodes are duplicated; '
           || 'cycles need a bnode with >1 parent)'
    FROM tr WHERE o_type = 'bnode' GROUP BY o HAVING count(*) > 1;

CREATE OR REPLACE MACRO flatten_triples(staging) AS TABLE
    WITH RECURSIVE
    raw AS (SELECT * FROM query_table(staging)),
    rdf AS (SELECT 'http://www.w3.org/1999/02/22-rdf-syntax-ns#' AS ns),
    -- Walk down from every uri subject through bnodes.
    --   path: alternating predicate, bnode label, predicate, ... taken so far. During the walk a
    --         bnode label is '_:<random number>'; it is renumbered per resource below.
    --   pos : 1-based position inside the current rdf list
    walk(root, node, path, pos, depth) AS (
        SELECT s, o, [p, '_:' || ((hash(uuid()) >> 2)::VARCHAR)], 1, 1
        FROM raw WHERE s_type = 'uri' AND o_type = 'bnode'
      UNION ALL
        SELECT x.root, x.node,
               CASE WHEN x.p = x.ns || 'rest'  THEN x.path
                    WHEN x.p = x.ns || 'first' THEN list_concat(x.path, [x.ns || '_' || x.pos, x.bid])
                    ELSE list_concat(x.path, [x.p, x.bid]) END,
               CASE WHEN x.p = x.ns || 'rest' THEN x.pos + 1 ELSE 1 END,
               CASE WHEN x.p = x.ns || 'rest' THEN x.depth ELSE x.depth + 1 END
        FROM (
            -- fresh label per walk step: a bnode reached via N paths becomes N copies
            SELECT w.root, raw.o AS node, raw.p, w.path, w.pos, w.depth,
                   '_:' || ((hash(uuid()) >> 2)::VARCHAR) AS bid, rdf.ns
            FROM walk w
            JOIN raw ON raw.s = w.node AND raw.s_type = 'bnode'
            CROSS JOIN rdf
            WHERE raw.o_type = 'bnode'
              AND w.depth < 100        -- bnode nesting limit (cycle guard)
              AND w.pos < 10000        -- list length limit (rest-only cycle guard)
        ) x
    ),
    -- leaves: direct leaves of uri subjects (loc = []) ...
    leaves AS (
        SELECT s AS root, []::VARCHAR[] AS loc, p, o, o_type, o_lang, o_dt
        FROM raw WHERE s_type = 'uri' AND o_type <> 'bnode'
      UNION ALL
        -- ... and leaves hanging off a bnode reached by the walk (loc = the walk's path)
        SELECT w.root, w.path,
               CASE WHEN raw.p = rdf.ns || 'first' THEN rdf.ns || '_' || w.pos ELSE raw.p END,
               raw.o, raw.o_type, raw.o_lang, raw.o_dt
        FROM walk w
        JOIN raw ON raw.s = w.node AND raw.s_type = 'bnode'
        CROSS JOIN rdf
        WHERE raw.o_type <> 'bnode'
          AND NOT (raw.p = rdf.ns || 'rest' AND raw.o = rdf.ns || 'nil')   -- list terminator
    ),
    -- renumber the bnodes per resource: _:1, _:2, _:3, ... The outer ones come first, those at
    -- one depth in the order of their predicate path (bnodes that tie are numbered at random)
    inst AS (
        SELECT DISTINCT root, loc[k] AS temp, k,
               list_filter(loc[1:k-1], lambda x: NOT starts_with(x, '_:')) AS via
        FROM leaves, LATERAL (SELECT unnest(range(2, len(loc) + 1, 2)) AS k)
    ),
    ranked AS (
        SELECT root, temp, '_:' || (row_number() OVER (PARTITION BY root ORDER BY k, via, temp)) AS n
        FROM inst
    ),
    maps AS (SELECT root, map(list(temp), list(n)) AS m FROM ranked GROUP BY root),
    final AS (
        SELECT l.*, list_transform(l.loc, lambda x: CASE WHEN starts_with(x, '_:') THEN m[x] ELSE x END) AS nloc
        FROM leaves l LEFT JOIN maps USING (root)
    )
    SELECT root AS s, nloc AS loc, p,
           CASE WHEN o_type = 'uri' THEN o END      AS o,
           CASE WHEN o_type = 'literal' THEN o END  AS lit,
           o_lang AS lang, o_dt AS dt
    FROM final;

-- Reverse of flatten_triples, as rows. A bnode is named '_:b<r>_<n>' (r numbers the resources of
-- the result, n is the bnode of loc), unique inside one result. Positional list predicates
-- (rdf:_N under a bnode) become rdf:first / rdf:rest cells again
-- (cell 1 reuses the head bnode label, cell N>1 is '<head>_cN'). The last column, root, is the
-- subject uri of the resource the row belongs to (the same as s for a row of a uri subject).
CREATE OR REPLACE MACRO triples_rows(tbl, uris := NULL) AS TABLE
    WITH
    rdf AS (SELECT 'http://www.w3.org/1999/02/22-rdf-syntax-ns#' AS ns),
    paths AS (
        SELECT s AS root, list_append(loc, p) AS path, o, lit, lang, dt
        FROM query_table(tbl)
        WHERE uris IS NULL OR s IN (SELECT unnest(uris))
    ),
    roots AS (SELECT root, dense_rank() OVER (ORDER BY root) AS r FROM (SELECT DISTINCT root FROM paths)),
    -- one edge per (predicate, following node) step along every path
    -- (s_ref / o_ref: a uri, or a bnode label '_:<n>' of resource `root`)
    edge_ids AS (
        SELECT DISTINCT
            root,
            CASE WHEN i = 1 THEN root ELSE path[i-1] END AS s_ref,
            path[i] AS p,
            CASE WHEN i < len(path) THEN path[i+1] ELSE o END AS o_ref,
            CASE WHEN i < len(path) THEN 'bnode'
                 WHEN o IS NOT NULL THEN 'uri'
                 ELSE 'literal' END AS o_type,
            CASE WHEN i = len(path) THEN lit END  AS lit,
            CASE WHEN i = len(path) THEN lang END AS lang,
            CASE WHEN i = len(path) THEN dt END   AS dt
        FROM paths, LATERAL (SELECT unnest(range(1, len(path) + 1, 2)) AS i)
    ),
    edges AS (
        SELECT e.root,
               CASE WHEN starts_with(e.s_ref, '_:') THEN '_:b' || r.r || '_' || substr(e.s_ref, 3)
                    ELSE e.s_ref END AS s,
               CASE WHEN starts_with(e.s_ref, '_:') THEN 'bnode' ELSE 'uri' END AS s_type,
               e.p,
               CASE e.o_type WHEN 'uri' THEN e.o_ref
                             WHEN 'bnode' THEN '_:b' || r.r || '_' || substr(e.o_ref, 3)
                             ELSE e.lit END AS o,
               e.o_type, e.lang AS o_lang, e.dt AS o_dt
        FROM edge_ids e JOIN roots r USING (root)
    ),
    flagged AS (
        SELECT *, s_type = 'bnode' AND regexp_matches(p, '^' || (SELECT ns FROM rdf) || '_[0-9]+$') AS is_item
        FROM edges
    ),
    items AS (
        SELECT *, CAST(regexp_extract(p, '_([0-9]+)$', 1) AS INTEGER) AS n,
               max(CAST(regexp_extract(p, '_([0-9]+)$', 1) AS INTEGER)) OVER (PARTITION BY s) AS last_n
        FROM flagged WHERE is_item
    ),
    cells AS (
        SELECT root, CASE WHEN n = 1 THEN s ELSE s || '_c' || n END AS cell,
               CASE WHEN n = last_n THEN (SELECT ns FROM rdf) || 'nil'
                    ELSE s || '_c' || (n + 1) END AS next,
               n = last_n AS is_last,
               o, o_type, o_lang, o_dt
        FROM items
    ),
    out_triples AS (
        SELECT s, s_type, p, o, o_type, o_lang, o_dt, root FROM flagged WHERE NOT is_item
      UNION ALL
        SELECT cell, 'bnode', (SELECT ns FROM rdf) || 'first', o, o_type, o_lang, o_dt, root FROM cells
      UNION ALL
        SELECT cell, 'bnode', (SELECT ns FROM rdf) || 'rest', next,
               CASE WHEN is_last THEN 'uri' ELSE 'bnode' END, NULL, NULL, root FROM cells
    )
    SELECT s, s_type, p, o, o_type, o_lang, o_dt, root FROM out_triples;

CREATE OR REPLACE MACRO export_triples(tbl, uris := NULL) AS TABLE
    SELECT json_pretty(json_object('triples', coalesce(to_json(list(
        json_merge_patch('{}', json_object(
            'subject',   json_object('value', s, 'type', s_type),
            'predicate', json_object('value', p, 'type', 'uri'),
            'object',    json_merge_patch('{}', json_object(
                            'value', o, 'type', o_type, 'lang', o_lang, 'datatype', o_dt))))
        ORDER BY s, p, o, o_lang, o_dt)), '[]'::JSON))) AS json
    FROM triples_rows(tbl, uris);

-- ---- the records table ------------------------------------------------------------------------
-- slugify(text): a slug of a text: lower case letters and digits, one dash between them. Accents
-- are taken off (é is e), anything else that is not a letter of ours or a digit (other scripts,
-- punctuation, symbols) goes, and 80 characters are enough. Empty when nothing is left.
CREATE OR REPLACE MACRO slugify(x) AS
    regexp_replace(
        regexp_replace(
            left(regexp_replace(lower(strip_accents(x)), '[^a-z0-9]+', '-', 'g'), 80),
            '^-+', '', 'g'),
        '-+$', '', 'g');

-- N-Triples text. A literal is its JSON string (the same escapes), then @lang or ^^<datatype>. A uri
-- goes in <>: the characters that N-Triples does not allow in an IRI are percent-encoded, and the
-- control characters, DEL and the backtick are removed. The `%` is left alone, so a uri that is
-- already encoded stays as it is.
CREATE OR REPLACE MACRO nt_literal(v) AS to_json(v)::VARCHAR;
CREATE OR REPLACE MACRO encode_ntriples_uri(uri) AS (
    '<' ||
    regexp_replace(
        replace(replace(replace(replace(replace(replace(replace(replace(replace(
            uri::VARCHAR,
            ' ', '%20'),
            '<', '%3C'),
            '>', '%3E'),
            '"', '%22'),
            '{', '%7B'),
            '}', '%7D'),
            '|', '%7C'),
            '\', '%5C'),
            '^', '%5E'),
        '[\x00-\x20\x7F`]', '', 'g'
    ) || '>'
);

-- record_rows(tbl, subs): the rows of the `records` table for the subject uris `subs` (a list) of the
-- triples table `tbl`. Only subjects of the form rec:<type>:<n> are records. A subject that has
-- no rows in the table any more comes out as a deleted record (no content, only its keys).
-- Blank nodes in `source` are `_:b<record id>_<n>` (n is the number of the bnode in the resource, kept
-- in `loc`): the same every time, unique across records, and valid as an `rdf:nodeID`, so the sources
-- of many records can be put together and the same record twice is the same nodes. Its macros
-- `record_id` and `type_slug` (the numbers of the types) are made by the store before this file.
CREATE OR REPLACE MACRO record_rows(tbl, subs) AS TABLE
    WITH
    rec AS MATERIALIZED (
        SELECT s, regexp_extract(s, '^rec:([a-z][a-z0-9-]*):([1-9][0-9]{0,17})$', 1) AS rec_type,
               -- a record of a type without a number, or with a number of 10^7 or more, has no id: it raises
               record_id(s) AS record_id
        FROM (SELECT DISTINCT unnest(subs) AS s)
        WHERE regexp_matches(s, '^rec:([a-z][a-z0-9-]*):([1-9][0-9]{0,17})$')
    ),
    t AS (SELECT * FROM query_table(tbl) WHERE s IN (SELECT s FROM rec)),
    -- the values of the properties that have a role in the shape of the type of the record
    valued AS (
        SELECT t.s, r.role, coalesce(t.o, t.lit) AS v, t.lit, r.ord, t.loc, t.p
        FROM t JOIN rec ON rec.s = t.s JOIN property_roles r ON r.rec_type = rec.rec_type AND r.p = t.p
    ),
    parts AS (
        SELECT s,
               max(o) FILTER (WHERE len(loc) = 0 AND p = 'https://gracia.pub/ns/resource_uri') AS res_uri,
               max(lit) FILTER (WHERE len(loc) = 0 AND p = 'https://gracia.pub/ns/label') AS label,
               max(lit) FILTER (WHERE len(loc) = 0 AND p = 'https://gracia.pub/ns/slug') AS slug,
               max(lit) FILTER (WHERE len(loc) = 0 AND p = 'https://gracia.pub/ns/created_at') AS created,
               max(lit) FILTER (WHERE len(loc) = 0 AND p = 'https://gracia.pub/ns/modified_at') AS modified,
               bool_or(lower(lit) IN ('true', '1')) FILTER (
                   WHERE len(loc) = 0 AND p = 'https://gracia.pub/ns/is_published') AS published,
               -- what the resource points at (its content, below `gracia:resource`): the rest of the record is the
               -- software's, a resource points at the resource uris of other records, never at records
               list_sort(list(DISTINCT o) FILTER (
                   WHERE o IS NOT NULL AND len(loc) >= 2 AND loc[1] = 'https://gracia.pub/ns/resource')) AS refs
        FROM t GROUP BY s
    ),
    ids AS (
        SELECT s, list_sort(list(DISTINCT v)) AS ids FROM valued WHERE role = 'key_info' GROUP BY s
    ),
    texts AS (
        SELECT s, string_agg(lit, chr(10) ORDER BY ord, loc, p, lit) AS text
        FROM valued WHERE role = 'description' AND lit IS NOT NULL GROUP BY s
    ),
    -- no records is an empty list, not NULL: NULL would mean every resource of the table
    tr AS (SELECT * FROM triples_rows(tbl, uris := coalesce((SELECT list(s) FROM rec), []::VARCHAR[]))),
    bnodes AS MATERIALIZED (
        SELECT label, 'b' || record_id(root) || '_' || regexp_replace(label, '^_:b[0-9]+_', '') AS id
        FROM (SELECT root, s AS label FROM tr WHERE s_type = 'bnode'
              UNION SELECT root, o FROM tr WHERE o_type = 'bnode')
    ),
    sources AS (
        SELECT tr.root AS s,
               string_agg(
                   CASE WHEN tr.s_type = 'bnode' THEN '_:' || bs.id ELSE encode_ntriples_uri(tr.s) END || ' '
                   || encode_ntriples_uri(tr.p) || ' '
                   || CASE tr.o_type WHEN 'uri' THEN encode_ntriples_uri(tr.o)
                                     WHEN 'bnode' THEN '_:' || bo.id
                                     ELSE nt_literal(tr.o)
                                          || CASE WHEN tr.o_lang IS NOT NULL THEN '@' || tr.o_lang
                                                  WHEN tr.o_dt IS NOT NULL THEN '^^' || encode_ntriples_uri(tr.o_dt)
                                                  ELSE '' END END
                   || ' .' || chr(10),
                   '' ORDER BY tr.s <> tr.root, tr.s, tr.p <> 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type', tr.p, tr.o
               ) AS source
        FROM tr
        LEFT JOIN bnodes bs ON tr.s_type = 'bnode' AND bs.label = tr.s
        LEFT JOIN bnodes bo ON tr.o_type = 'bnode' AND bo.label = tr.o
        GROUP BY tr.root
    )
    -- created_at and modified_at are the values of the properties of the record (the predicates make
    -- them, in UTC: the column and the property are one value); a record that has none (written
    -- without them) was modified now, and created when its first version was written
    SELECT rec.record_id, rec.rec_type, parts.res_uri, parts.slug,
           coalesce(try_cast(rtrim(parts.created, 'Z') AS TIMESTAMP),
                    (SELECT min(h.modified_at) FROM history h WHERE h.record_id = rec.record_id),
                    now() AT TIME ZONE 'UTC') AS created_at,
           coalesce(try_cast(rtrim(parts.modified, 'Z') AS TIMESTAMP), now() AT TIME ZONE 'UTC') AS modified_at,
           coalesce(parts.published, true) AS is_published,
           parts.label,
           coalesce(ids.ids, []) AS identifiers,
           coalesce(parts.refs, []) AS "references",
           texts.text,
           coalesce(sources.source, '') AS source,
           strlen(coalesce(sources.source, ''))::INTEGER AS bytes
    FROM rec
    JOIN parts ON parts.s = rec.s
    LEFT JOIN ids ON ids.s = rec.s
    LEFT JOIN texts ON texts.s = rec.s
    LEFT JOIN sources ON sources.s = rec.s;
