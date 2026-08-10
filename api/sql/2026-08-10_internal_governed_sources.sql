-- Register the two internal, governed evidence feeds used by CIR orchestration.
--
-- evidence.source_id has a foreign key to sources_catalog.  Without these rows,
-- add_evidence() fails and the report silently falls back to external sources.
-- Apply this migration before deploying the matching gateway revision.

INSERT INTO sources_catalog
    (id, name, country, source_type, source_tier, auditable_for_banks,
     base_url, notes)
VALUES
    ('onboarding_governed',
     'COPAP Onboarding Governed Record',
     NULL,
     'internal_system_of_record',
     'INTERNAL_GOVERNED',
     true,
     'onboarding://counterparty/',
     'Versioned request-time context: declared/linked parent, subsidiaries, '
     'relationships, active directors/UBOs, registrations and labelled stored intelligence.'),
    ('mdm_governed',
     'COPAP Master Data Governed Relationship',
     NULL,
     'internal_system_of_record',
     'INTERNAL_GOVERNED',
     true,
     'https://copapmasterdata.azurewebsites.net/api/m2m/',
     'Counterparty role and trade linkage resolved by CpID or ComplianceEntityID/GID.')
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    source_type = EXCLUDED.source_type,
    source_tier = EXCLUDED.source_tier,
    auditable_for_banks = EXCLUDED.auditable_for_banks,
    base_url = EXCLUDED.base_url,
    notes = EXCLUDED.notes;
