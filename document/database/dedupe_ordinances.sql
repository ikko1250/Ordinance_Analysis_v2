-- Dedupe ordinances and implementation_dates before adding unique constraints.
-- Recommended: make a backup of data/ordinance_data.db before running.

BEGIN;

-- Re-point implementation_dates to the canonical ordinance (min id per key).
UPDATE implementation_dates
SET ordinance_id = (
    SELECT MIN(o2.id)
    FROM ordinances o2
    WHERE o2.municipality_id = (
        SELECT o1.municipality_id FROM ordinances o1 WHERE o1.id = implementation_dates.ordinance_id
    )
      AND o2.ordinance_name = (
        SELECT o1.ordinance_name FROM ordinances o1 WHERE o1.id = implementation_dates.ordinance_id
    )
      AND o2.enactment_year = (
        SELECT o1.enactment_year FROM ordinances o1 WHERE o1.id = implementation_dates.ordinance_id
    )
);

-- Delete duplicate ordinances, keeping the smallest id per key.
DELETE FROM ordinances
WHERE id NOT IN (
    SELECT MIN(id)
    FROM ordinances
    GROUP BY municipality_id, ordinance_name, enactment_year
);

-- Remove duplicate implementation_dates after re-pointing.
DELETE FROM implementation_dates
WHERE id NOT IN (
    SELECT MIN(id)
    FROM implementation_dates
    GROUP BY ordinance_id, implementation_date, description
);

-- Add unique indexes to prevent re-duplication.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ordinances_unique
ON ordinances (municipality_id, ordinance_name, enactment_year);

CREATE UNIQUE INDEX IF NOT EXISTS idx_implementation_dates_unique
ON implementation_dates (ordinance_id, implementation_date, description);

COMMIT;
