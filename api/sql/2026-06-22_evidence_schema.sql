-- 2026-06-22 evidence-collection schema
-- Database: crawl_reports (same server as cir_reports / darkweb_reports)
--
-- Architecture: CIR shifts from "the report" to "one render of the
-- evidence pool." Evidence + claims + sources_catalog = source of truth.
-- cir_reports stays for backward compat; new work lands here.
--
-- Apply:
--   psql "host=crawl-monitor-db.postgres.database.azure.com \
--         dbname=crawl_reports user=crawladmin sslmode=require" \
--        -f api/sql/2026-06-22_evidence_schema.sql

-- pgcrypto extension is NOT enabled — Azure-managed Postgres restricts it.
-- Postgres 16+ provides gen_random_uuid() in core, no extension required.

-- --------------------------------------------------------------------------
-- Source catalog: every source we collect from, tiered for bank audit
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources_catalog (
  id                  text PRIMARY KEY,
  name                text NOT NULL,
  country             text,                 -- ISO-2; NULL = supranational
  source_type         text NOT NULL,        -- gov_registry | sanctions_list | court_record | breach_db | news | darkweb | aggregator | osint
  source_tier         text NOT NULL,        -- PRIMARY_GOVERNMENT | OFFICIAL_LIST | COMMERCIAL_AGGREGATOR | OSINT | DARKWEB
  auditable_for_banks boolean NOT NULL DEFAULT false,
  base_url            text,
  notes               text,
  added_at            timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- One row per evidence-collection run for an entity
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cir_runs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id          text UNIQUE,             -- gateway job_id; nullable for direct runs
  entity_name     text NOT NULL,
  country         text NOT NULL,           -- ISO-2
  status          text NOT NULL DEFAULT 'collecting',  -- collecting | extracting | synthesizing | complete | failed
  started_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  evidence_count  int NOT NULL DEFAULT 0,
  claim_count     int NOT NULL DEFAULT 0,
  error           text,
  meta            jsonb                    -- caller tags, request_id, etc.
);
CREATE INDEX IF NOT EXISTS cir_runs_entity_idx ON cir_runs(entity_name, country);
CREATE INDEX IF NOT EXISTS cir_runs_status_idx ON cir_runs(status);

-- --------------------------------------------------------------------------
-- Atomic evidence: every source we touched. Append-only.
-- raw_blob_path points to verbatim raw bytes in osint-staging.
-- raw_content_hash (sha256) is for tamper detection vs the blob.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id                uuid NOT NULL REFERENCES cir_runs(id) ON DELETE CASCADE,
  source_id             text NOT NULL REFERENCES sources_catalog(id),
  source_url            text NOT NULL,
  source_query          text,
  fetched_at            timestamptz NOT NULL DEFAULT now(),
  status_code           int,
  raw_blob_path         text,
  raw_content_hash      text NOT NULL,
  extracted             jsonb,
  language_original     text,             -- ISO-639
  extraction_confidence numeric(3,2),     -- 0.00–1.00
  parser_version        text NOT NULL,
  error                 text
);
CREATE INDEX IF NOT EXISTS evidence_run_idx ON evidence(run_id);
CREATE INDEX IF NOT EXISTS evidence_source_idx ON evidence(source_id);
CREATE INDEX IF NOT EXISTS evidence_extracted_gin ON evidence USING gin(extracted);

-- --------------------------------------------------------------------------
-- Structured claims (what the synthesis LLM is allowed to assert)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          uuid NOT NULL REFERENCES cir_runs(id) ON DELETE CASCADE,
  claim_type      text NOT NULL,           -- director | ubo | shareholder | sanction | adverse_media | dark_web_finding | registration | address | financial | relationship
  subject         text NOT NULL,           -- entity or person
  predicate       text NOT NULL,           -- e.g. 'is_director_of', 'beneficially_owns_pct', 'is_sanctioned_by'
  object          jsonb NOT NULL,          -- typed payload
  confidence      text NOT NULL DEFAULT 'medium',  -- high | medium | low
  rationale       text,                    -- LLM short reason for grouping the evidence
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims_run_idx ON claims(run_id);
CREATE INDEX IF NOT EXISTS claims_type_idx ON claims(run_id, claim_type);

-- --------------------------------------------------------------------------
-- Many-to-many: evidence backing each claim
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claim_evidence (
  claim_id     uuid NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  evidence_id  uuid NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  support      text NOT NULL DEFAULT 'primary',  -- primary | corroborating | contradicting
  quoted_value text,                       -- exact substring/field that supports the claim
  PRIMARY KEY (claim_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS claim_evidence_evidence_idx ON claim_evidence(evidence_id);

-- --------------------------------------------------------------------------
-- Synthesis runs: exact prompt + response sent to the LLM (full reproducibility)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS synthesis_runs (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id              uuid NOT NULL REFERENCES cir_runs(id) ON DELETE CASCADE,
  model               text NOT NULL,       -- e.g. 'copapllm-v1'
  system_prompt_hash  text NOT NULL,       -- versioned prompt template
  user_prompt         text NOT NULL,       -- final assembled prompt
  evidence_ids        uuid[] NOT NULL,     -- exactly which evidence rows were shown
  response_raw        text,
  response_parsed     jsonb,
  uncited_claims      jsonb,               -- LLM claims that didn't cite evidence (rejected)
  tokens_in           int,
  tokens_out          int,
  latency_ms          int,
  cost_usd            numeric(10,4),
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS synthesis_runs_run_idx ON synthesis_runs(run_id);

-- --------------------------------------------------------------------------
-- Renders: every output produced from evidence+claims (CIR, screening, UBO, audit pack)
-- One run can produce many renders cheaply (no re-collection).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS renders (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id       uuid NOT NULL REFERENCES cir_runs(id) ON DELETE CASCADE,
  render_type  text NOT NULL,             -- cir_markdown | sanctions_screening | ubo_map | banker_audit_pack
  synthesis_id uuid REFERENCES synthesis_runs(id),
  blob_path    text,
  payload      jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS renders_run_idx ON renders(run_id);
CREATE INDEX IF NOT EXISTS renders_type_idx ON renders(run_id, render_type);

-- --------------------------------------------------------------------------
-- Seed: starter catalog. Extend as collectors come online.
-- --------------------------------------------------------------------------
INSERT INTO sources_catalog (id, name, country, source_type, source_tier, auditable_for_banks, base_url, notes) VALUES
  -- Global / supranational
  ('gleif_lei',          'GLEIF LEI',                     NULL, 'gov_registry',   'OFFICIAL_LIST',         true,  'https://api.gleif.org/api/v1/',         'Corporate hierarchy lookup, limited to LEI-issued entities'),
  ('opensanctions',      'OpenSanctions',                 NULL, 'sanctions_list', 'OFFICIAL_LIST',         true,  'https://api.opensanctions.org/',        'Aggregates 350+ sanctions/PEP lists'),
  ('ofac_sdn',           'OFAC SDN List',                 'US', 'sanctions_list', 'PRIMARY_GOVERNMENT',    true,  'https://www.treasury.gov/ofac/',        'US Treasury SDN'),
  ('ofsi_consolidated',  'OFSI Consolidated List',        'GB', 'sanctions_list', 'PRIMARY_GOVERNMENT',    true,  'https://www.gov.uk/ofsi',               'UK Treasury sanctions'),
  ('eu_sanctions',       'EU Consolidated Sanctions',     NULL, 'sanctions_list', 'PRIMARY_GOVERNMENT',    true,  'https://webgate.ec.europa.eu/fsd/fsf', 'EU FSD'),
  ('un_sanctions',       'UN Security Council Sanctions', NULL, 'sanctions_list', 'PRIMARY_GOVERNMENT',    true,  'https://scsanctions.un.org/',          'UNSC consolidated list'),
  ('worldbank_debarment','World Bank Debarred Firms',     NULL, 'sanctions_list', 'OFFICIAL_LIST',         true,  'https://projects.worldbank.org/',      'Cross-debarment list'),
  ('interpol_notices',   'Interpol Red Notices',          NULL, 'sanctions_list', 'OFFICIAL_LIST',         true,  'https://www.interpol.int/',            'Public wanted notices'),
  ('icij_offshore',      'ICIJ Offshore Leaks',           NULL, 'court_record',   'OSINT',                 false, 'https://offshoreleaks.icij.org/',      'Panama/Paradise/Pandora Papers'),
  ('occrp_aleph',        'OCCRP Aleph',                   NULL, 'court_record',   'OSINT',                 false, 'https://aleph.occrp.org/',             'Organized crime & corruption investigations'),
  ('opencorporates',     'OpenCorporates',                NULL, 'aggregator',     'COMMERCIAL_AGGREGATOR', false, 'https://opencorporates.com/',          'Cross-jurisdiction corporate aggregator'),

  -- Country-specific gov registries (seed; extend as needed)
  ('us_sec_edgar',       'SEC EDGAR',                     'US', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://www.sec.gov/edgar',            'US securities filings'),
  ('gb_companies_house', 'UK Companies House',            'GB', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://api.company-information.service.gov.uk/', 'UK corporate registry incl PSC'),
  ('hk_icris3ep',        'HK Companies Registry (ICRIS3EP)','HK','gov_registry',  'PRIMARY_GOVERNMENT',    true,  'https://www.icris.cr.gov.hk/',         'HK registry; paid HKD 22 per uncached BRN'),
  ('in_mca',             'India MCA21',                   'IN', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://www.mca.gov.in/',              'Ministry of Corporate Affairs'),
  ('in_dgft',            'India DGFT IEC',                'IN', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://www.dgft.gov.in/',             'Import/export code (Multilogin)'),
  ('cn_qichacha',        'Qichacha',                      'CN', 'aggregator',     'COMMERCIAL_AGGREGATOR', false, 'https://www.qcc.com/',                 'CN corporate aggregator (gov GSXT blocked)'),
  ('cn_tianyancha',      'Tianyancha',                    'CN', 'aggregator',     'COMMERCIAL_AGGREGATOR', false, 'https://www.tianyancha.com/',          'CN corporate aggregator'),
  ('ae_dmcc',            'UAE DMCC',                      'AE', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://portal.dmcc.ae/',              'Dubai DMCC free zone'),
  ('pk_secp',            'Pakistan SECP',                 'PK', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://www.secp.gov.pk/',             'Securities & Exchange Commission'),
  ('pk_fbr_atl',         'Pakistan FBR ATL',              'PK', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://iris.fbr.gov.pk/',             'Active Taxpayer List (Multilogin + CAPTCHA OCR)'),
  ('sg_acra_bizfile',    'Singapore ACRA Bizfile',        'SG', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://www.bizfile.gov.sg/',          'SG corporate registry'),
  ('br_cnpj',            'Brazil CNPJ (Receita Federal)', 'BR', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://www.gov.br/receitafederal/',   'BR tax / corporate registry'),
  ('tr_mersis',          'Turkey MERSIS',                 'TR', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://mersis.gtb.gov.tr/',           'TR central commercial registry'),
  ('ru_egrul',           'Russia EGRUL',                  'RU', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://egrul.nalog.ru/',              'Unified state register of legal entities'),
  ('pe_decolecta',       'Peru SUNAT via Decolecta',      'PE', 'aggregator',     'COMMERCIAL_AGGREGATOR', false, 'https://api.decolecta.com/v1/',        'SUNAT RUC lookup, 1K/mo free tier'),
  ('tw_gcis',            'Taiwan GCIS',                   'TW', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://data.gcis.nat.gov.tw/',        'TW Govt Commerce open data'),
  ('sa_mci',             'Saudi MCI',                     'SA', 'gov_registry',   'PRIMARY_GOVERNMENT',    true,  'https://mc.gov.sa/',                   'Saudi Ministry of Commerce (Multilogin + CAPTCHA OCR)'),

  -- Dark web / breach / OSINT
  ('dw_ahmia',           'Ahmia .onion search',           NULL, 'darkweb',        'DARKWEB',               false, 'http://ahmia.fi/',                     'Tor search engine'),
  ('dw_torch',           'Torch .onion search',           NULL, 'darkweb',        'DARKWEB',               false, NULL,                                   'Tor search engine'),
  ('dw_dehashed',        'Dehashed',                      NULL, 'breach_db',      'OSINT',                 false, 'https://api.dehashed.com/',            'Breach DB, $15/mo'),
  ('dw_hudsonrock',      'HudsonRock Cavalier',           NULL, 'breach_db',      'OSINT',                 false, 'https://cavalier.hudsonrock.com/',     'Infostealer/credential exposure'),
  ('dw_leakix',          'LeakIX',                        NULL, 'breach_db',      'OSINT',                 false, 'https://leakix.net/',                  'Exposed services & leaks'),
  ('dw_ransomlook',      'Ransomlook',                    NULL, 'darkweb',        'OSINT',                 false, 'https://www.ransomlook.io/',           'Ransomware group victim lists'),
  ('osint_wikileaks',    'WikiLeaks',                     NULL, 'news',           'OSINT',                 false, 'https://wikileaks.org/',               'Leaked documents (exact match)')
ON CONFLICT (id) DO NOTHING;
