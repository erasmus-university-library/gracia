-- Delete resources: every row of each subject uri, including all its embedded bnodes.
-- @URIS@ = SQL list of subject uris, e.g. ['http://example.org/a', 'http://example.org/b']
-- Uris that are not in the table are ignored. (No MERGE: the table has triggers.)
DELETE FROM @TRIPLES@ WHERE s IN (SELECT unnest(@URIS@));
