-- Minted ids, one counter per type of record (`rec:<type>:<n>`): `last` is the number given
-- out most recently. Numbers are never reused, a failed save leaves a gap. A type needs no setup:
-- its row appears when its first number is minted or its first record is stored.
-- `reserved` is a floor that a person sets (the Identification tab of the type): the numbers up to it
-- are kept for whatever is going to be imported, and the next number minted is past it. The next
-- number is past the greatest of `last`, `reserved` and the highest record there is.
CREATE TABLE IF NOT EXISTS @IDS@ (
    type VARCHAR PRIMARY KEY,
    last BIGINT NOT NULL,
    reserved BIGINT NOT NULL DEFAULT 0
);
