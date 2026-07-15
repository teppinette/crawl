-- New evidence sources for the deepened CIR collection (2026-07-15).
--
-- add_evidence() enforces evidence.source_id REFERENCES sources_catalog(id),
-- so these rows MUST exist before the new collectors write, or the insert 400s.
--
--   web_profile    — official-website discovery + crawl (SearXNG + Crawl4AI,
--                    self-hosted, free tools only). Tier OSINT.
--   cn_affiliates  — depth-1 affiliate/UBO expansion (对外投资 / corporate
--                    shareholders / branches) re-queried via the CN registry.
--                    Tier COMMERCIAL_AGGREGATOR.
--
-- cn_registry is included defensively: it is written by the live CN collector
-- but is absent from the original 2026-06-22 seed (added out-of-band on the
-- live DB). Idempotent — safe to re-run.

INSERT INTO sources_catalog (id, name, country, source_type, source_tier, auditable_for_banks, base_url, notes) VALUES
  ('web_profile',   'Website Profile (SearXNG + Crawl4AI)',  NULL, 'osint',        'OSINT',                 false, 'https://',                    'Self-hosted SearXNG discovery + Crawl4AI fetch; free tools only'),
  ('cn_affiliates', 'CN Affiliate Expansion',                'CN', 'aggregator',   'COMMERCIAL_AGGREGATOR', false, 'https://www.tianyancha.com/', 'Depth-1 outbound-investment / corporate-shareholder / branch expansion via CN registry lookup'),
  ('cn_registry',   'CN Gov Registry (GSXT/SAMR via verify)','CN', 'gov_registry', 'PRIMARY_GOVERNMENT',    true,  'https://www.gsxt.gov.cn/',    'CN gov registry primary block via crawl-verify Multilogin')
ON CONFLICT (id) DO NOTHING;
