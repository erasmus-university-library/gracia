-- Flattened triples, one row per value. Uris are stored as they are (strings): a resource is
-- found, ordered and exported by its subject (`rec:<type>:<n>`), so a table kept sorted on s keeps
-- the rows of a type together. Subjects and objects are never blank nodes.
--   s    : the subject uri, the resource the row belongs to
--   loc  : where in the resource the value sits: [] for a property of the resource itself,
--          otherwise alternating predicate uri and bnode label, from the resource down to the
--          bnode that has the value: [p1, '_:1', p2, '_:2'] (an even number of entries).
--          Bnodes are numbered per resource ('_:1', '_:2', ...): a bnode is identified by
--          (s, label) only. A bnode shared through a name is duplicated, once per path.
--   p    : the predicate of the value (always the last one of the path)
--   o    : the object when it is a uri, else NULL
--   lit  : the text of the object when it is a literal, else NULL
--   lang : language tag of the literal, dt: its datatype uri
--   rdf lists are flattened to positional predicates rdf:_1, rdf:_2, ...
CREATE TABLE IF NOT EXISTS @TRIPLES@ (
    s    VARCHAR NOT NULL,
    loc  VARCHAR[] NOT NULL DEFAULT [],
    p    VARCHAR NOT NULL,
    o    VARCHAR,
    lit  VARCHAR,
    lang VARCHAR,
    dt   VARCHAR
);
