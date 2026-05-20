-- Migration: create clinical_records table
-- Take Five · 2026-05-20
--
-- Stores all clinical facts for care recipients — medications, diagnoses,
-- symptoms, appointments, allergies, vitals, procedures.
--
-- resource_type mirrors FHIR R4 resource names so the eventual EHR
-- integration layer only needs to translate data → fhir_resource,
-- with no schema changes required.
--
-- Supported resource_type values (extensible — add without migration):
--   MedicationStatement  — prescription or supplement
--   Condition            — diagnosis or problem list entry
--   Observation          — symptom, vital, mood score
--   Appointment          — scheduled or completed visit
--   AllergyIntolerance   — drug, food, or environmental allergy
--   Procedure            — surgery, treatment, or intervention

CREATE TABLE clinical_records (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Circle and care recipient
    circle_id         UUID        NOT NULL REFERENCES care_circles(id) ON DELETE CASCADE,
    person_id         UUID        NOT NULL REFERENCES people(id) ON DELETE CASCADE,

    -- FHIR R4 resource type as discriminator
    resource_type     TEXT        NOT NULL
                      CHECK (resource_type IN (
                          'MedicationStatement',
                          'Condition',
                          'Observation',
                          'Appointment',
                          'AllergyIntolerance',
                          'Procedure'
                      )),

    -- Record status — vocabulary covers all resource types
    status            TEXT        NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'discontinued', 'as_needed', 'resolved', 'cancelled')),

    -- Natural representation — what Claude extracts and family confirms/edits.
    -- Shape varies by resource_type (see application code for per-type schemas).
    data              JSONB       NOT NULL DEFAULT '{}',

    -- FHIR R4 representation — null until EHR integration is built.
    -- Populated by to_fhir() translator at write time or backfilled by migration.
    fhir_resource     JSONB,

    -- Free-text family additions — preferences, context, observations.
    -- e.g. "Mom prefers to take this before dinner, not at bedtime"
    notes             TEXT,

    -- Audit trail
    source_message_id UUID        REFERENCES messages(id) ON DELETE SET NULL,
    confirmed_by      UUID        REFERENCES people(id) ON DELETE SET NULL,
    confirmed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX idx_clinical_circle
    ON clinical_records(circle_id);

CREATE INDEX idx_clinical_person
    ON clinical_records(person_id);

-- Composite — most queries filter by circle + type + status
CREATE INDEX idx_clinical_type_status
    ON clinical_records(circle_id, resource_type, status);

-- GIN index for searching inside data JSONB
-- Supports: data @> '{"medication_name": "Dayvigo"}'
CREATE INDEX idx_clinical_data
    ON clinical_records USING gin(data);

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER clinical_records_updated_at
    BEFORE UPDATE ON clinical_records
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
