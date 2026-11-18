-- Move the counter of every type past the numbers of the records that are being stored: a record that
-- comes in with its number (an import, a copy from another repository, a record made by code) is
-- not minted, so without this the next number minted for its type could be one that is taken, and
-- the record made with it would replace the one that came in. Counters only go up. Run in the
-- transaction of the load, on the staged subjects; any type works, none is listed anywhere.
INSERT INTO @IDS@ (type, last)
SELECT type, max(number) FROM (
    SELECT regexp_extract(s, '^rec:([a-z][a-z0-9-]*):([1-9][0-9]{0,17})$', 1) AS type,
           CAST(regexp_extract(s, '^rec:([a-z][a-z0-9-]*):([1-9][0-9]{0,17})$', 2) AS BIGINT) AS number
    FROM @STAGING@
    WHERE s_type = 'uri' AND s LIKE 'rec:%'
      AND regexp_matches(s, '^rec:([a-z][a-z0-9-]*):([1-9][0-9]{0,17})$')
)
GROUP BY type
ON CONFLICT (type) DO UPDATE SET last = greatest(@IDS@.last, excluded.last);
