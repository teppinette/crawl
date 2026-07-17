-- Run -> collector-version audit link.
-- "How was entity X verified on date D, and what changed?" — record WHICH collector
-- agent (id) and which audited VERSION + content_hash produced each CIR run.
-- Mirrors synthesis_runs.system_prompt_hash, which already versions the synth prompt.
--
-- Fully additive + backward-compatible: nullable columns, IF NOT EXISTS. Existing
-- runs stay NULL. Safe to apply before or after the container that writes them
-- (api/evidence_db.set_run_collector_version swallows the error if absent).

ALTER TABLE cir_runs ADD COLUMN IF NOT EXISTS collector_agent_id      text;
ALTER TABLE cir_runs ADD COLUMN IF NOT EXISTS collector_agent_version text;
ALTER TABLE cir_runs ADD COLUMN IF NOT EXISTS collector_content_hash  text;
