-- Migration: person-scope clinical_records
-- Take Five · 2026-05-21
--
-- Clinical records belong to a person (care recipient), not a circle.
-- circle_id is retained as nullable provenance — records created from
-- chat messages retain the source circle; records created via admin
-- or other non-chat paths have circle_id = NULL.
--
-- Visibility by circle is determined at query time by resolving
-- seniors in the circle via circle_memberships, not by filtering
-- on circle_id in clinical_records.
--
-- Also adds CareTeamMember to the resource_type allowlist.

-- 1. Drop the old composite index that required circle_id
DROP INDEX IF EXISTS idx_clinical_type_status;

-- 2. Make circle_id nullable (retains existing data)
ALTER TABLE clinical_records
    ALTER COLUMN circle_id DROP NOT NULL;

-- 3. Expand resource_type CHECK to include CareTeamMember
ALTER TABLE clinical_records
    DROP CONSTRAINT IF EXISTS clinical_records_resource_type_check;

ALTER TABLE clinical_records
    ADD CONSTRAINT clinical_records_resource_type_check
    CHECK (resource_type IN (
        'MedicationStatement',
        'Condition',
        'Observation',
        'Appointment',
        'AllergyIntolerance',
        'Procedure',
        'CareTeamMember'
    ));

-- 4. New composite index — person + type + status (replaces circle-based one)
CREATE INDEX IF NOT EXISTS idx_clinical_person_type_status
    ON clinical_records(person_id, resource_type, status);

-- 5. Provenance index — still useful to find records sourced from a circle
CREATE INDEX IF NOT EXISTS idx_clinical_circle_provenance
    ON clinical_records(circle_id)
    WHERE circle_id IS NOT NULL;
