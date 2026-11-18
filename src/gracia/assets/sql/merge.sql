-- Replace-by-resource, as two statements (MERGE INTO is refused on a table with triggers; run both
-- in one transaction). A resource = a subject uri together with all bnodes below it. Its identity
-- is the subject uri ALONE. First every row of every resource in the staged triples is deleted
-- (this statement returns the deleted subjects, one per row), then the staged rows are inserted
-- (`insert.sql`); other resources are untouched. Bnode labels are renumbered per load, which is
-- fine: a resource is replaced as a whole.
--
-- @STAGING@ = name of the staging table.
DELETE FROM @TRIPLES@
WHERE s IN (SELECT DISTINCT s FROM @STAGING@ WHERE s_type = 'uri')
RETURNING s;
