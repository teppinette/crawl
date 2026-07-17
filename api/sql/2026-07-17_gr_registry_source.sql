-- Register gr_registry in sources_catalog. It was the only country *_registry
-- missing (GR never had a collector), so evidence_add(source_id="gr_registry")
-- hit the evidence.source_id FK and 400'd, failing the GR collector.
-- Mirrors de_registry. Idempotent.

INSERT INTO sources_catalog
    (id, name, country, source_type, source_tier, auditable_for_banks, base_url, added_at)
VALUES
    ('gr_registry', 'GR Registry (via crawl-verify)', 'GR', 'gov_registry',
     'PRIMARY_GOVERNMENT', true, 'https://publicity.businessportal.gr/', now())
ON CONFLICT (id) DO NOTHING;
