-- Append the flattened triples of the batch (`Store.load` has made them, and run the hooks on them).
INSERT INTO @TRIPLES@ SELECT * FROM @FLAT@;
