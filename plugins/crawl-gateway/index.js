// @crawl/gateway-tools — OpenClaw plugin for the Crawl OSINT Research Platform
//
// Registers seven agent tools:
//   1. dark_web_search   — Search 22 dark web sources via Tor
//   2. gateway_submit    — Submit a research job to any scenario/region
//   3. gateway_status    — Check status of a gateway job
//   4. report_search     — List existing reports via gateway
//   5. platform_health   — Check platform health
//   6. verify_entity     — Real-time entity verification (PK/IN/TR/AE/CN)
//   7. adverse_media     — Multi-provider adverse media screening (GDELT, crt.sh, Wayback)

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

// --- helpers ----------------------------------------------------------------

const GATEWAY_URL = "http://20.94.45.219:8400";
const GATEWAY_KEY = "cpk_cir_2026Q2_a7f3e9d1b4c8";

async function gw(method, path, body) {
  const url = `${GATEWAY_URL}${path}`;
  const opts = {
    method,
    headers: { "Content-Type": "application/json", "X-API-Key": GATEWAY_KEY },
    signal: AbortSignal.timeout(120_000),
  };
  if (body) opts.body = JSON.stringify(body);

  const res = await fetch(url, opts);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!res.ok) throw new Error(`Gateway ${res.status}: ${JSON.stringify(data)}`);
  return data;
}

function txt(obj) {
  return [{ type: "text", text: typeof obj === "string" ? obj : JSON.stringify(obj, null, 2) }];
}

// --- polling helper ---------------------------------------------------------

async function pollJob(jobId, maxWaitSec) {
  const deadline = Date.now() + maxWaitSec * 1000;
  let result;
  while (Date.now() < deadline) {
    result = await gw("GET", `/api/v1/jobs/${jobId}`);
    if (result.status === "completed" || result.status === "failed" || result.status === "error") {
      return result;
    }
    await new Promise(r => setTimeout(r, 5000));
  }
  return result;
}

// --- plugin entry -----------------------------------------------------------

export default definePluginEntry({
  id: "crawl-gateway",
  name: "Crawl Gateway Tools",
  description: "Tools for the Crawl OSINT research platform",

  register(api) {

    // -----------------------------------------------------------------------
    // 1. dark_web_search
    // -----------------------------------------------------------------------
    api.registerTool({
      name: "dark_web_search",
      description: `Search 22 dark web and OSINT sources for an entity, person, or domain.
Sources include: Tor search engines (Ahmia, Torch), breach databases (Dehashed, LeakCheck, BreachDirectory),
infostealer exposure (HudsonRock), ransomware feeds (Ransomlook), investigative databases (OCCRP Aleph,
ICIJ Offshore Leaks), sanctions (OpenSanctions), leaked documents (WikiLeaks), Telegram channels,
Web Archive, court records, threat intel (PulseDive, FullHunt, Greynoise), and more.
Takes 30-90 seconds. Returns structured findings with risk classification.`,
      parameters: {
        type: "object",
        properties: {
          entity_name: {
            type: "string",
            description: "Company or entity name to search",
          },
          owners: {
            type: "string",
            description: "Comma-separated list of owner/director/UBO names to search (optional)",
          },
          domain: {
            type: "string",
            description: "Domain name to search for breaches and exposure (optional)",
          },
          country: {
            type: "string",
            description: "Two-letter country code (optional, for context)",
          },
          wait: {
            type: "boolean",
            description: "If true, poll until results are ready (up to 120s). Default true.",
          },
        },
        required: ["entity_name"],
      },
      async execute(_id, params) {
        try {
          const payload = {
            scenario: "dark-web",
            payload: {
              entity_name: params.entity_name,
            },
          };
          if (params.owners) payload.payload.owners = params.owners;
          if (params.domain) payload.payload.domain = params.domain;
          if (params.country) payload.payload.country_code = params.country;

          const job = await gw("POST", "/api/v1/jobs", payload);
          const jobId = job.job_id;

          const shouldWait = params.wait !== false;
          if (!shouldWait) {
            return { content: txt({ message: "Dark web search submitted", job_id: jobId, status: "queued", check_with: "gateway_status" }) };
          }

          const result = await pollJob(jobId, 120);
          return { content: txt(result) };
        } catch (e) {
          return { content: txt({ error: e.message }) };
        }
      },
    });

    // -----------------------------------------------------------------------
    // 2. gateway_submit
    // -----------------------------------------------------------------------
    api.registerTool({
      name: "gateway_submit",
      description: `Submit a research job to the Crawl gateway for any scenario.
Scenarios: "cir" (counterparty intelligence/due diligence), "product-intel" (pricing/sourcing/competitors), "dark-web" (dark web OSINT).
For CIR: provide entity_name and country_code. Dark web enrichment runs automatically after CIR.
For product-intel: provide product details and target regions.
Returns a job_id to check status with gateway_status.`,
      parameters: {
        type: "object",
        properties: {
          scenario: {
            type: "string",
            enum: ["cir", "product-intel", "dark-web"],
            description: "Research scenario type",
          },
          entity_name: {
            type: "string",
            description: "Entity/company name for CIR or dark-web",
          },
          country_code: {
            type: "string",
            description: "Two-letter country code for jurisdiction routing",
          },
          payload: {
            type: "object",
            description: "Full payload object (advanced — overrides other fields)",
          },
        },
        required: ["scenario"],
      },
      async execute(_id, params) {
        try {
          let body;
          if (params.payload) {
            body = { scenario: params.scenario, payload: params.payload };
          } else {
            const p = {};
            if (params.entity_name) p.entity_name = params.entity_name;
            if (params.country_code) p.country_code = params.country_code;
            body = { scenario: params.scenario, payload: p };
          }
          const job = await gw("POST", "/api/v1/jobs", body);
          return { content: txt(job) };
        } catch (e) {
          return { content: txt({ error: e.message }) };
        }
      },
    });

    // -----------------------------------------------------------------------
    // 3. gateway_status
    // -----------------------------------------------------------------------
    api.registerTool({
      name: "gateway_status",
      description: `Check the status of a Crawl gateway job. Returns status, results, and dark web findings if available.
Can also list recent jobs if no job_id is provided.`,
      parameters: {
        type: "object",
        properties: {
          job_id: {
            type: "string",
            description: "Job ID to check. If omitted, lists recent jobs.",
          },
          scenario: {
            type: "string",
            description: "Filter recent jobs by scenario (only when listing)",
          },
        },
      },
      async execute(_id, params) {
        try {
          if (params.job_id) {
            const result = await gw("GET", `/api/v1/jobs/${params.job_id}`);
            return { content: txt(result) };
          }
          const query = params.scenario ? `?scenario=${params.scenario}` : "";
          const result = await gw("GET", `/api/v1/jobs${query}`);
          return { content: txt(result) };
        } catch (e) {
          return { content: txt({ error: e.message }) };
        }
      },
    });

    // -----------------------------------------------------------------------
    // 4. report_search
    // -----------------------------------------------------------------------
    api.registerTool({
      name: "report_search",
      description: `Search existing OSINT reports. Lists recent research jobs filtered by scenario or entity name.
Use this to check if an entity has already been researched before starting a new job.`,
      parameters: {
        type: "object",
        properties: {
          scenario: {
            type: "string",
            description: "Filter by scenario (cir, product-intel, dark-web)",
          },
        },
      },
      async execute(_id, params) {
        try {
          const query = params.scenario ? `?scenario=${params.scenario}` : "";
          const result = await gw("GET", `/api/v1/jobs${query}`);
          return { content: txt(result) };
        } catch (e) {
          return { content: txt({ error: e.message }) };
        }
      },
    });

    // -----------------------------------------------------------------------
    // 5. platform_health
    // -----------------------------------------------------------------------
    api.registerTool({
      name: "platform_health",
      description: "Check the health of the Crawl research platform — gateway status, available scenarios, active threads, and regional VM availability.",
      parameters: {
        type: "object",
        properties: {},
      },
      async execute() {
        try {
          const health = await gw("GET", "/api/v1/health");
          const regions = await gw("GET", "/api/v1/regions");
          return { content: txt({ gateway: health, regions }) };
        } catch (e) {
          return { content: txt({ error: e.message }) };
        }
      },
    });

    // -----------------------------------------------------------------------
    // 6. verify_entity — Real-time entity verification against gov registries
    // -----------------------------------------------------------------------
    api.registerTool({
      name: "verify_entity",
      description: `Real-time entity verification against government registries. Returns in 5-15 seconds.
Supported countries:
  PK — SECP (direct gov query) + FBR tax status
  IN — CIN/directors/status via Tofler (MCA21 data)
  TR — MERSIS + GIB portal reachability (via TR residential proxy)
  AE — DIFC + JAFZA + MOEC portal reachability (via AE residential proxy)
  CN — SAMR portal reachability (via CN residential proxy)
For PK: returns legal name, SECP registration number, status, company type, CRO, registration date.
For IN: returns CIN, legal name, status, directors, address, capital.
For TR/AE/CN: confirms registry portal is reachable (full search requires CIR job).`,
      parameters: {
        type: "object",
        properties: {
          entity_name: {
            type: "string",
            description: "Company or entity name to verify",
          },
          country_code: {
            type: "string",
            description: "Two-letter country code: PK, IN, TR, AE, or CN",
          },
          ntn: {
            type: "string",
            description: "Pakistan NTN number for FBR tax verification (optional, PK only)",
          },
          cin: {
            type: "string",
            description: "India Corporate Identification Number for direct lookup (optional, IN only)",
          },
        },
        required: ["entity_name", "country_code"],
      },
      async execute(_id, params) {
        try {
          const body = { entity_name: params.entity_name, country_code: params.country_code };
          if (params.ntn) body.ntn = params.ntn;
          if (params.cin) body.cin = params.cin;
          const result = await gw("POST", "/api/v1/verify", body);
          return { content: txt(result) };
        } catch (e) {
          return { content: txt({ error: e.message }) };
        }
      },
    });

    // -----------------------------------------------------------------------
    // 7. adverse_media — Multi-provider adverse media screening
    // -----------------------------------------------------------------------
    api.registerTool({
      name: "adverse_media",
      description: `Screen an entity for adverse media coverage across multiple providers.
Providers: GDELT (65 languages, bilingual queries via AI translation, negative-tone filter),
crt.sh (SSL certificate transparency — shell company signal), Wayback Machine (domain age — shell company signal).
Bing News and SerpAPI available when API keys are provisioned.
Returns structured articles with title, URL, source, language, publication date, and provider.
Also returns shell_signals: certificate count, earliest cert date, first Wayback capture, domain age in days.
Use this during counterparty research to check for fraud, corruption, sanctions, lawsuits, and other adverse coverage.
Takes 15-30 seconds depending on number of languages searched.`,
      parameters: {
        type: "object",
        properties: {
          company_name: {
            type: "string",
            description: "Full legal name of the entity to screen",
          },
          country: {
            type: "string",
            description: "Two-letter country code (determines default search languages: CN→en+zh, PK→en+ur, etc.)",
          },
          website: {
            type: "string",
            description: "Entity website domain for shell company checks via crt.sh and Wayback (optional but recommended)",
          },
          languages: {
            type: "array",
            items: { type: "string" },
            description: "ISO 639-1 language codes to search (optional, defaults by country)",
          },
          days_back: {
            type: "number",
            description: "Lookback window in days (1-90, default 30)",
          },
          max_results: {
            type: "number",
            description: "Maximum articles to return (default 20)",
          },
        },
        required: ["company_name", "country"],
      },
      async execute(_id, params) {
        try {
          const body = {
            company_name: params.company_name,
            country: params.country,
          };
          if (params.website) body.website = params.website;
          if (params.languages) body.languages = params.languages;
          if (params.days_back) body.days_back = params.days_back;
          if (params.max_results) body.max_results = params.max_results;
          body.tier = "ENHANCED";

          const result = await gw("POST", "/tools/adverse_media", body);

          // Build a human-readable summary
          const arts = result.articles || [];
          const shell = result.shell_signals;
          let summary = `Adverse media scan: ${result.status} (${result.duration_ms}ms)\n`;
          summary += `Articles found: ${arts.length}\n`;

          // Provider breakdown
          if (result.providers) {
            summary += "\nProviders:\n";
            for (const [name, info] of Object.entries(result.providers)) {
              summary += `  ${name}: ${info.status} (${info.count} results, ${info.latency_ms}ms)`;
              if (info.error) summary += ` — ${info.error}`;
              summary += "\n";
            }
          }

          // Shell company signals
          if (shell) {
            summary += `\nShell company signals:\n`;
            summary += `  SSL certificates: ${shell.cert_count}`;
            if (shell.earliest_cert_date) summary += ` (earliest: ${shell.earliest_cert_date})`;
            summary += "\n";
            if (shell.wayback_first_capture) summary += `  Wayback first capture: ${shell.wayback_first_capture}\n`;
            if (shell.domain_age_days != null) summary += `  Domain age: ${shell.domain_age_days} days\n`;
          }

          // Article list
          if (arts.length > 0) {
            summary += "\nArticles:\n";
            for (const a of arts.slice(0, 20)) {
              const date = a.published_at ? a.published_at.slice(0, 10) : "?";
              summary += `  [${a.language}|${date}] ${a.source}: ${a.title}\n`;
              summary += `    ${a.url}\n`;
            }
          }

          return { content: [{ type: "text", text: summary }, { type: "text", text: JSON.stringify(result, null, 2) }] };
        } catch (e) {
          return { content: txt({ error: e.message }) };
        }
      },
    });
  },
});
