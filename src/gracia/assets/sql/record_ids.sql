-- The uri of a record from its id (the id is type number * 10^7 + record number, a 32 bit INTEGER). The
-- macros `record_id(uri)` and `type_slug(number)` are made by the store (`Store.set_type_ids`) and
-- know every type of the repository, a type is never given another number, so an id never changes
-- and `records` and `history` can hold it for good. Run after them.
--   record_id(uri)  : the id of a record uri (`rec:<type>:<n>`); NULL for NULL; an error (`unknown
--                     record uri`) when the uri starts with the prefix of no type (not a record, a type
--                     that has no number), and when what follows the prefix is not a number from 0 to
--                     9,999,999. Ask about a uri that may be anything (a reference that is an ARK) only when it
--                     starts with `rec:`
--   record_uri(id)  : the uri of an id, NULL when the type of the id has no number
--   resource_uri(id): the same as `record_uri` (the opposite of `record_id`). Not to be confused with
--                     `gracia:resource_uri`, the published uri (ARK) of a record, the column `res_uri`
CREATE OR REPLACE MACRO record_uri(id_) AS
    'rec:' || type_slug(id_ // 10000000) || ':' || CAST(id_ % 10000000 AS VARCHAR);
CREATE OR REPLACE MACRO resource_uri(id_) AS record_uri(id_);
