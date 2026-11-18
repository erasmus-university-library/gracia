-- Rows per resource that delete_resource.sql is about to delete.
-- @URIS@ = SQL list of subject uris.
SELECT s, count(*) AS deleted_rows
FROM @TRIPLES@
WHERE s IN (SELECT unnest(@URIS@))
GROUP BY s ORDER BY s;
