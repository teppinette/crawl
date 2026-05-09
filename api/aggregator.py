"""
Aggregator Registry Lookup — Firecrawl-driven web scraper for 50+ countries.

Searches business-info aggregator sites (opencorporates, pappers, northdata, etc.)
via Firecrawl, extracts director names from scraped markdown using regex.

This is a PORT of GC's AggregatorRegistryClient pattern. Each country is ~20 lines
of config (search queries, director label regex, bad tokens). The Firecrawl engine
and name extraction logic are shared.

NOT authoritative — aggregator-sourced data must be verified against the local
government registry before sign-off. Every response includes a verify_note.

Usage:
    result = await lookup("FR", "TotalEnergies")
    # Returns: {verified: true, officers: [...], verify_note: "...", ...}
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional

import httpx

from keyvault import get_secret
import raw_store

log = logging.getLogger("aggregator")

# ---------------------------------------------------------------------------
# Firecrawl config
# ---------------------------------------------------------------------------

_FC_API_KEY = get_secret("firecrawl-api-key") or os.environ.get("FIRECRAWL_API_KEY", "")
_FC_SEARCH_URL = "https://api.firecrawl.dev/v1/search"
_FC_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"

# ---------------------------------------------------------------------------
# Default name regex: 2-4 Latin words, capitalized first letter
# ---------------------------------------------------------------------------

_DEFAULT_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+(?:[\-\']?[A-Za-z]+)?"
    r"(?:\s+[A-Z][a-z]*(?:[\-\']?[A-Za-z]+)?){1,3})\b"
)

# German umlaut support
_DE_NAME_RE = re.compile(
    r"\b([A-ZÄÖÜ][a-zäöüß]+(?:[\-\']?[A-ZÄÖÜa-zäöüß]+)?"
    r"(?:\s+[A-ZÄÖÜ][a-zäöüß]*(?:[\-\']?[A-ZÄÖÜa-zäöüß]+)?){1,3})\b"
)

# Dutch tussenvoegsel (van, de, den, der, ten, ter)
_NL_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+(?:[\-\']?[A-Za-z]+)?"
    r"(?:\s+(?:van|de|den|der|ten|ter)\s+)?"
    r"(?:\s+[A-Z][a-z]*(?:[\-\']?[A-Za-z]+)?){1,3})\b"
)

# CJK names (Chinese: 2-4 characters)
_CJK_NAME_RE = re.compile(r"([一-鿿]{2,4})")

_DEFAULT_BAD_TOKENS = {
    "director", "officer", "principal", "authorised", "authorized",
    "company", "limited", "corporation", "inc", "incorporated",
    "change", "timeline", "history", "section", "profile", "overview",
    "summary", "register", "registration", "address", "phone", "fax",
    "email", "chairman", "secretary",
}

_BAD_CJK = {
    "有限", "公司", "集团", "控股", "投资", "发展", "科技", "实业",
    "贸易", "国际", "中国", "企业", "管理", "咨询", "服务", "工程",
    "股份", "责任", "合伙", "个人", "独资", "注册", "资本", "地址",
    "电话", "邮箱", "法定", "代表", "经营", "范围",
}


# ---------------------------------------------------------------------------
# Country configs — ported from GC's per-country adapter files
# ---------------------------------------------------------------------------

COUNTRIES: dict = {
    "AR": {
        "queries": [
            ("afip", "site:afip.gob.ar {name}"),
            ("igj", "site:igj.gob.ar {name}"),
            ("cuitonline", "site:cuitonline.com {name}"),
            ("opencorp", "site:opencorporates.com Argentina {name}"),
            ("dateas", "site:dateas.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Vicepresidente|Sindico|Socio(?:s)?)\s*[:\-\|*]+',
        "bad_tokens": {"sa", "sas", "srl", "sca", "limited", "company", "group", "argentina", "buenos", "aires", "cordoba", "rosario", "director", "directorio", "apoderado", "presidente", "vicepresidente", "sindico"},
        "verify_note": "Aggregator-sourced. Authoritative via IGJ Buenos Aires extract.",
    },
    "AT": {
        "queries": [
            ("firmenbuch", "site:firmenbuch.at {name}"),
            ("justiz", "site:justiz.gv.at {name}"),
            ("opencorp", "site:opencorporates.com Austria {name}"),
            ("northdata", "site:northdata.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Geschäftsführer|Vorstand|Aufsichtsrat|Prokurist)\s*[:\-\|*]+',
        "bad_tokens": {"ag", "austria", "director", "geschaftsfuhrer", "gmbh", "kg", "limited", "og", "vienna", "vorstand", "wien"},
        "verify_note": "Aggregator-sourced. Firmenbuchauszug paid (~€3-30); northdata.com free baseline.",
        "name_re": "de",
    },
    "AU": {
        "queries": [
            ("abr", "site:abr.business.gov.au {name}"),
            ("asic", "site:asic.gov.au {name}"),
            ("opencorp", "site:opencorporates.com Australia {name}"),
            ("connectw", "site:connectweb.com.au {name}"),
            ("dnb", "site:dnb.com {name} Australia"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|CFO|Chairman|Secretary|Sole\s+Director|Public\s+Officer|Authoris(?:ed|ed)\s+Person)\s*[:\-\|*]+',
        "bad_tokens": {"pty", "ltd", "limited", "company", "group", "holdings", "corporation", "inc", "director", "officer", "australia", "sydney", "melbourne", "brisbane", "perth"},
        "verify_note": "Aggregator-sourced. Authoritative roster via ASIC current-extract (~AU$9).",
    },
    "BD": {
        "queries": [
            ("rjsc", "site:roc.gov.bd {name}"),
            ("dse", "site:dsebd.org {name}"),
            ("opencorp", "site:opencorporates.com Bangladesh {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Managing\s+Director|Chairman)\s*[:\-\|*]+',
        "bad_tokens": {"bangladesh", "chairman", "dhaka", "director", "limited", "ltd", "officer", "plc"},
        "verify_note": "Aggregator-sourced. RJSC Form XII extract authoritative.",
    },
    "BE": {
        "queries": [
            ("kbo", "site:kbopub.economie.fgov.be {name}"),
            ("moniteur", "site:ejustice.just.fgov.be {name}"),
            ("opencorp", "site:opencorporates.com Belgium {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:s|eur)?|Bestuurder(?:s)?|Officer|Gérant(?:e)?|Administrateur(?:s)?|Commissaire)\s*[:\-\|*]+',
        "bad_tokens": {"administrateur", "antwerp", "belgique", "belgium", "bestuurder", "brussels", "bvba", "director", "gerant", "limited", "sa", "sl", "sprl"},
        "verify_note": "Aggregator-sourced. KBO public consultation free; Moniteur Belge for board changes.",
    },
    "BG": {
        "queries": [
            ("brra", "site:brra.bg {name}"),
            ("papagal", "site:papagal.bg {name}"),
            ("opencorp", "site:opencorporates.com Bulgaria {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Manager|CEO|Управител|Председател|Изпълнителен)\s*[:\-\|*]+',
        "bad_tokens": {"ad", "bulgaria", "director", "eood", "limited", "manager", "ood", "plovdiv", "sofia"},
        "verify_note": "Aggregator-sourced. brra.bg FREE official trade register.",
    },
    "BO": {
        "queries": [
            ("fundempresa", "site:fundempresa.org.bo {name}"),
            ("opencorp", "site:opencorporates.com Bolivia {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente|Socio)\s*[:\-\|*]+',
        "bad_tokens": {"bolivia", "director", "gerente", "lapaz", "limited", "presidente", "sa", "santacruz", "srl"},
        "verify_note": "Aggregator-sourced. Fundempresa Certificación de Vigencia authoritative.",
    },
    "CA": {
        "queries": [
            ("corpcan", "site:corporationscanada.ic.gc.ca {name}"),
            ("opencorp", "site:opencorporates.com Canada {name}"),
            ("oncorp", "site:appmybizaccount.gov.on.ca {name}"),
            ("bcreg", "site:bcregistry.ca {name}"),
            ("quebec", "site:registreentreprises.gouv.qc.ca {name}"),
            ("canlii", "site:canlii.org {name} Canada"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:s)?|Officer(?:s)?|CEO|CFO|President|Vice\s+President|Secretary|Treasurer|Chairman|Administrateur(?:s)?|Dirigeant)\s*[:\-\|*]+',
        "bad_tokens": {"inc", "corp", "ltee", "ltd", "limited", "corporation", "company", "group", "canada", "ontario", "quebec", "alberta", "british", "columbia", "manitoba", "director", "officer", "president", "secretary", "treasurer", "chairman", "administrateur", "dirigeant"},
        "verify_note": "Aggregator-sourced. Corporations Canada free federal extract authoritative for federally-incorporated cos.",
    },
    "CL": {
        "queries": [
            ("cmf", "site:cmfchile.cl {name}"),
            ("sii", "site:sii.cl {name}"),
            ("mercantil", "site:mercantil.com {name}"),
            ("opencorp", "site:opencorporates.com Chile {name}"),
            ("dequienes", "site:dequienes.cl {name}"),
            ("rutificador", "site:nombrerutyfirma.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Vicepresidente|Gerente(?:\s+General)?|Socio(?:s)?|Accionista(?:s)?)\s*[:\-\|*]+',
        "bad_tokens": {"sa", "spa", "sapi", "srl", "limited", "company", "group", "chile", "santiago", "valparaiso", "concepcion", "director", "directorio", "apoderado", "presidente", "vicepresidente", "gerente", "general"},
        "verify_note": "Aggregator-sourced. CMF authoritative for listed cos; private cos require Chilean Notary Public extract.",
    },
    "CO": {
        "queries": [
            ("rues", "site:rues.org.co {name}"),
            ("camara", "site:ccb.org.co {name}"),
            ("siic", "site:siic.gov.co {name}"),
            ("opencorp", "site:opencorporates.com Colombia {name}"),
            ("dian", "site:dian.gov.co {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente(?:\s+General)?|Socio(?:s)?|Junta\s+Directiva)\s*[:\-\|*]+',
        "bad_tokens": {"sas", "sa", "ltda", "srl", "limited", "company", "group", "colombia", "bogota", "medellin", "cali", "cartagena", "director", "directorio", "apoderado", "presidente", "gerente", "general", "junta", "directiva"},
        "verify_note": "Aggregator-sourced. Cámara de Comercio extract (Cert Existencia y Representación Legal) is authoritative.",
    },
    "CR": {
        "queries": [
            ("rnp", "site:rnpdigital.com {name}"),
            ("hacienda", "site:hacienda.go.cr {name}"),
            ("opencorp", "site:opencorporates.com Costa Rica {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente|Socio)\s*[:\-\|*]+',
        "bad_tokens": {"apoderado", "costa", "director", "gerente", "limited", "presidente", "rica", "sa", "sanjose", "srl"},
        "verify_note": "Aggregator-sourced. RNP Personeria Juridica extract authoritative.",
    },
    "CY": {
        "queries": [
            ("drcor", "site:efiling.drcor.mcit.gov.cy {name}"),
            ("opencorp", "site:opencorporates.com Cyprus {name}"),
            ("cyreg", "site:cyprus-registry.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Secretary|CEO|Chairman)\s*[:\-\|*]+',
        "bad_tokens": {"cyprus", "director", "limassol", "limited", "ltd", "nicosia", "officer", "plc", "secretary"},
        "verify_note": "Aggregator-sourced. DRCOR e-filing free public search of officers + members.",
    },
    "CZ": {
        "queries": [
            ("justice", "site:or.justice.cz {name}"),
            ("opencorp", "site:opencorporates.com Czech {name}"),
            ("rejstrik", "site:rejstrikfirem.kurzy.cz {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Jednatel|Statutární\s+Orgán|Předseda\s+Představenstva|Člen\s+Představenstva)\s*[:\-\|*]+',
        "bad_tokens": {"as", "clen", "czech", "director", "jednatel", "ks", "limited", "officer", "prague", "praha", "sro", "vos"},
        "verify_note": "Aggregator-sourced. justice.cz public commercial register FREE with full director list.",
    },
    "DE": {
        "queries": [
            ("northdata", "site:northdata.com {name}"),
            ("handelsregister", "site:handelsregister.de {name}"),
            ("unternehmensreg", "site:unternehmensregister.de {name}"),
            ("bundesanzeiger", "site:bundesanzeiger.de {name}"),
            ("opencorp", "site:opencorporates.com Germany {name}"),
            ("dnb", "site:dnb.com {name} Germany"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:s)?|Officer(?:s)?|CEO|CFO|Chairman|Secretary|Geschäftsführer(?:in)?|Vorstand|Aufsichtsrat|Prokurist(?:in)?|Vorsitzender?|Stellvertreter)\s*[:\-\|*]+',
        "bad_tokens": {"director", "officer", "principal", "authorised", "authorized", "company", "limited", "corporation", "inc", "incorporated", "gmbh", "ag", "kg", "kgaa", "ohg", "se", "mbh", "change", "timeline", "history", "section", "profile", "overview", "summary", "register", "registration", "address", "germany", "deutschland", "address", "phone", "email", "geschäftsführer", "vorstand", "aufsichtsrat", "prokurist", "handelsregister", "amtsgericht"},
        "verify_note": "Aggregator-sourced. Authoritative roster requires HR extract (Auszug aus dem Handelsregister, ~€4.50).",
        "name_re": "de",
    },
    "DO": {
        "queries": [
            ("dgii", "site:dgii.gov.do {name}"),
            ("opencorp", "site:opencorporates.com Dominican {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente)\s*[:\-\|*]+',
        "bad_tokens": {"apoderado", "director", "dominicana", "gerente", "limited", "presidente", "sa", "santodomingo", "srl"},
        "verify_note": "Aggregator-sourced. ONAPI / Camara de Comercio for full board.",
    },
    "DZ": {
        "queries": [
            ("cnrc", "site:cnrc.org.dz {name}"),
            ("opencorp", "site:opencorporates.com Algeria {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Président|Directeur\s+Général|Gérant)\s*[:\-\|*]+',
        "bad_tokens": {"alger", "algeria", "algerie", "director", "gerant", "limited", "president", "sarl", "spa"},
        "verify_note": "Aggregator-sourced. CNRC extrait du registre de commerce authoritative.",
    },
    "EC": {
        "queries": [
            ("supercias", "site:supercias.gob.ec {name}"),
            ("sri", "site:sri.gob.ec {name}"),
            ("opencorp", "site:opencorporates.com Ecuador {name}"),
            ("cuit", "site:cuitec.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Gerente(?:\s+General)?|Presidente|Socio)\s*[:\-\|*]+',
        "bad_tokens": {"apoderado", "cia", "company", "director", "ecuador", "gerente", "guayaquil", "limited", "ltda", "presidente", "quito", "sa", "sas"},
        "verify_note": "Aggregator-sourced. Superintendencia de Compañías Vigencia free official extract authoritative.",
    },
    "EG": {
        "queries": [
            ("gafi", "site:gafi.gov.eg {name}"),
            ("egx", "site:egx.com.eg {name}"),
            ("mubasher", "site:mubasher.info {name} Egypt"),
            ("opencorp", "site:opencorporates.com Egypt {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Managing\s+Director|CEO|Chairman|Board\s+Member|رئيس\s+مجلس|عضو\s+مجلس|مدير)\s*[:\-\|*]+',
        "bad_tokens": {"sae", "saie", "llc", "limited", "company", "group", "egypt", "cairo", "alexandria", "director", "officer", "chairman"},
        "verify_note": "Aggregator-sourced. GAFI commercial register authoritative; EGX data covers listed cos only.",
    },
    "ES": {
        "queries": [
            ("rmercantil", "site:rmc.es {name}"),
            ("borme", "site:boe.es BORME {name}"),
            ("opencorp", "site:opencorporates.com Spain {name}"),
            ("einforma", "site:einforma.com {name}"),
            ("infoempresa", "site:infoempresa.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Administrador(?:es|\s+Unico)?|Consejero|Presidente|Apoderado|Socio)\s*[:\-\|*]+',
        "bad_tokens": {"administrador", "apoderado", "barcelona", "consejero", "director", "espain", "espana", "limited", "madrid", "presidente", "sa", "sl", "sll", "socio", "spain"},
        "verify_note": "Aggregator-sourced. Registro Mercantil Central nota simple paid; einforma/infoempresa free baseline.",
    },
    "FI": {
        "queries": [
            ("ytj", "site:ytj.fi {name}"),
            ("prh", "site:prh.fi {name}"),
            ("opencorp", "site:opencorporates.com Finland {name}"),
            ("finder", "site:finder.fi {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Hallituksen\s+Jäsen|Toimitusjohtaja|Hallituksen\s+Puheenjohtaja)\s*[:\-\|*]+',
        "bad_tokens": {"ab", "director", "finland", "helsinki", "ky", "limited", "officer", "oy", "oyj", "suomi", "tampere", "tmi"},
        "verify_note": "Aggregator-sourced. PRH kaupparekisteri ote authoritative.",
    },
    "FR": {
        "queries": [
            ("pappers", "site:pappers.fr {name}"),
            ("societe", "site:societe.com {name}"),
            ("infogreffe", "site:infogreffe.fr {name}"),
            ("opencorp", "site:opencorporates.com France {name}"),
            ("verif", "site:verif.com {name}"),
            ("bodacc", "site:bodacc.fr {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:s)?|Officer(?:s)?|CEO|CFO|Président(?:e|\s+Directeur\s+Général)?|Directeur\s+Général|Gérant(?:e)?|Administrateur(?:s)?|Mandataire|Commissaire\s+aux\s+Comptes)\s*[:\-\|*]+',
        "bad_tokens": {"sa", "sarl", "sas", "sasu", "sci", "sca", "eurl", "snc", "limited", "company", "france", "paris", "lyon", "marseille", "directeur", "général", "president", "gerant", "administrateur", "mandataire", "commissaire", "comptes", "directrice"},
        "verify_note": "Aggregator-sourced (pappers.fr / societe.com / infogreffe). INPI extract is authoritative.",
    },
    "GR": {
        "queries": [
            ("gemi", "site:businessportal.gr {name}"),
            ("opencorp", "site:opencorporates.com Greece {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Chairman|Πρόεδρος|Διευθυντής|Διαχειριστής)\s*[:\-\|*]+',
        "bad_tokens": {"ae", "athens", "chairman", "director", "epe", "greece", "limited", "officer", "president", "thessaloniki"},
        "verify_note": "Aggregator-sourced. GEMI businessportal.gr free certificate retrieval available.",
    },
    "GT": {
        "queries": [
            ("sat", "site:sat.gob.gt {name}"),
            ("opencorp", "site:opencorporates.com Guatemala {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente)\s*[:\-\|*]+',
        "bad_tokens": {"apoderado", "director", "gerente", "guatemala", "limited", "presidente", "sa", "srl"},
        "verify_note": "Aggregator-sourced. Registro Mercantil Patente de Comercio extract authoritative.",
    },
    "HK": {
        "queries": [
            ("verif", "site:verif.com {name}"),
            ("ltddir", "site:ltddir.com {name}"),
            ("hkcpy", "site:hkcpy.com {name}"),
            ("opencorp", "site:opencorporates.com Hong Kong {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:s)?|Officer(?:s)?|Principal(?:s)?|Key\s+Principal|Authoris(?:ed|ed)\s+Person|Sole\s+Director|Manager(?:ial\s+Officer)?)\s*[:\-\|*]+',
        "bad_tokens": {"director", "officer", "principal", "authorised", "authorized", "company", "limited", "corporation", "inc", "incorporated", "change", "timeline", "history", "section", "profile", "overview", "summary", "register", "registration", "address", "hong", "kong", "china", "address", "phone", "fax", "email"},
        "verify_note": "Aggregator-sourced. Verify against HKCR NAR1 (Annual Return) before approving trade limits.",
    },
    "HN": {
        "queries": [
            ("sar", "site:sar.gob.hn {name}"),
            ("opencorp", "site:opencorporates.com Honduras {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente)\s*[:\-\|*]+',
        "bad_tokens": {"director", "gerente", "honduras", "limited", "presidente", "sa", "sanpedro", "srl", "tegucigalpa"},
        "verify_note": "Aggregator-sourced. Camara de Comercio extract authoritative.",
    },
    "HU": {
        "queries": [
            ("cegjegyzek", "site:e-cegjegyzek.hu {name}"),
            ("cegjelzo", "site:cegjelzo.hu {name}"),
            ("opencorp", "site:opencorporates.com Hungary {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Ügyvezető|Igazgatótag|Cégvezető|Felügyelő)\s*[:\-\|*]+',
        "bad_tokens": {"bt", "budapest", "director", "hungary", "kft", "limited", "nyrt", "officer", "ugyvezeto", "zrt"},
        "verify_note": "Aggregator-sourced. Cégbíróság extract paid (~HUF 4000); cegjelzo.hu free baseline.",
    },
    "ID": {
        "queries": [
            ("idx", "site:idx.co.id {name}"),
            ("ahu", "site:ahu.go.id {name}"),
            ("ojk", "site:ojk.go.id {name}"),
            ("opencorp", "site:opencorporates.com Indonesia {name}"),
            ("idnfinancials", "site:idnfinancials.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|President\s+Director|Komisaris|Direktur(?:\s+Utama)?|Dewan\s+Komisaris|CEO|Chairman)\s*[:\-\|*：]+',
        "bad_tokens": {"pt", "tbk", "persero", "limited", "company", "group", "indonesia", "jakarta", "surabaya", "director", "officer", "komisaris", "direktur", "dewan", "utama"},
        "verify_note": "Aggregator-sourced. IDX authoritative for listed cos; private cos require AHU extract.",
    },
    "IL": {
        "queries": [
            ("ica", "site:ica.justice.gov.il {name}"),
            ("maya", "site:mayatase.tase.co.il {name}"),
            ("isa", "site:isa.gov.il {name}"),
            ("opencorp", "site:opencorporates.com Israel {name}"),
            ("dnb", "site:dnb.co.il {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|CFO|Chairman|Secretary|דירקטור|מנכ"?ל|יו"?ר|רואה\s+חשבון)\s*[:\-\|*]+',
        "bad_tokens": {"ltd", "limited", "company", "group", "israel", "tel", "aviv", "jerusalem", "haifa", "director", "officer", "chairman", "secretary"},
        "verify_note": "Aggregator-sourced. Maya authoritative for TASE-listed cos; private cos require Rasham HaChavarot extract.",
    },
    "IT": {
        "queries": [
            ("telemaco", "site:telemaco.infocamere.it {name}"),
            ("registroimp", "site:registroimprese.it {name}"),
            ("opencorp", "site:opencorporates.com Italy {name}"),
            ("italiacomp", "site:italiacompanies.com {name}"),
            ("reportaziende", "site:reportaziende.it {name}"),
            ("ufficiocamerale", "site:ufficiocamerale.it {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:s)?|Officer(?:s)?|CEO|Amministratore(?:\s+Delegato|\s+Unico)?|Presidente|Consigliere|Sindaco|Procuratore|Socio(?:\s+Unico)?)\s*[:\-\|*]+',
        "bad_tokens": {"srl", "spa", "sapa", "snc", "sas", "limited", "company", "italia", "italy", "milano", "roma", "torino", "director", "amministratore", "delegato", "unico", "presidente", "consigliere", "sindaco", "procuratore", "socio"},
        "verify_note": "Aggregator-sourced. Visura Camerale (paid, ~€8) from Camera di Commercio is authoritative.",
    },
    "JO": {
        "queries": [
            ("moit", "site:moit.gov.jo {name}"),
            ("cci", "site:jocc.org.jo {name}"),
            ("opencorp", "site:opencorporates.com Jordan {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Chairman|Managing\s+Director)\s*[:\-\|*]+',
        "bad_tokens": {"amman", "chairman", "director", "jordan", "limited", "llc", "officer", "plc", "psc"},
        "verify_note": "Aggregator-sourced. MoIT Companies Control Department extract authoritative.",
    },
    "JP": {
        "queries": [
            ("hojininfo", "site:info.gbiz.go.jp {name}"),
            ("canpan", "site:canpan.info {name}"),
            ("opencorp", "site:opencorporates.com Japan {name}"),
            ("teikoku", "site:tdb.co.jp {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|President|Representative\s+Director|代表取締役|取締役|社長|会長|監査役)\s*[:\-\|*：]+',
        "bad_tokens": {"kabushiki", "kk", "gk", "kabushikigaisha", "gaisha", "limited", "corporation", "inc", "company", "group", "holdings", "director", "officer", "president", "chairman"},
        "verify_note": "Aggregator-sourced. Verify against EDINET (listed cos) or TDB report (private cos).",
    },
    "KE": {
        "queries": [
            ("brs", "site:brs.go.ke {name}"),
            ("cma", "site:cma.or.ke {name}"),
            ("opencorp", "site:opencorporates.com Kenya {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Managing\s+Director|Chairman|Secretary)\s*[:\-\|*]+',
        "bad_tokens": {"chairman", "director", "kenya", "limited", "ltd", "nairobi", "officer", "plc"},
        "verify_note": "Aggregator-sourced. BRS eCitizen CR12 extract authoritative.",
    },
    "LK": {
        "queries": [
            ("roc", "site:drc.gov.lk {name}"),
            ("cse", "site:cse.lk {name}"),
            ("opencorp", "site:opencorporates.com Sri Lanka {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Managing\s+Director|Chairman|Secretary)\s*[:\-\|*]+',
        "bad_tokens": {"chairman", "colombo", "director", "limited", "officer", "plc", "pvt", "srilanka"},
        "verify_note": "Aggregator-sourced. ROC Form 20 extract authoritative.",
    },
    "LT": {
        "queries": [
            ("rekvizitai", "site:rekvizitai.vz.lt {name}"),
            ("registrucentras", "site:registrucentras.lt {name}"),
            ("opencorp", "site:opencorporates.com Lithuania {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Vadovas|Direktorius|Akcininkas)\s*[:\-\|*]+',
        "bad_tokens": {"ab", "director", "direktorius", "kaunas", "limited", "lithuania", "uab", "vadovas", "vilnius"},
        "verify_note": "Aggregator-sourced. Registru Centras paid extract; rekvizitai.vz.lt free.",
    },
    "LU": {
        "queries": [
            ("lbr", "site:lbr.lu {name}"),
            ("rcs", "site:rcsl.lu {name}"),
            ("opencorp", "site:opencorporates.com Luxembourg {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Administrateur(?:s)?|Gérant(?:s)?|Délégué|Commissaire)\s*[:\-\|*]+',
        "bad_tokens": {"administrateur", "commissaire", "delegue", "gerant", "limited", "luxembourg", "sa", "sarl", "sca", "scs"},
        "verify_note": "Aggregator-sourced. LBR public consultation free; RCS extract authoritative.",
    },
    "LV": {
        "queries": [
            ("ur", "site:ur.gov.lv {name}"),
            ("lursoft", "site:lursoft.lv {name}"),
            ("opencorp", "site:opencorporates.com Latvia {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Valdes\s+Loceklis|Padomes\s+Loceklis|Prokūrists)\s*[:\-\|*]+',
        "bad_tokens": {"as", "daugavpils", "director", "latvia", "limited", "officer", "riga", "sia"},
        "verify_note": "Aggregator-sourced. UR free company name search; lursoft.lv full extract paid.",
    },
    "MA": {
        "queries": [
            ("ompic", "site:ompic.ma {name}"),
            ("cnss", "site:cnss.ma {name}"),
            ("opencorp", "site:opencorporates.com Morocco {name}"),
            ("directinfo", "site:directinfo.ma {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Chairman|Président|Directeur\s+Général|Gérant)\s*[:\-\|*]+',
        "bad_tokens": {"casablanca", "director", "gerant", "limited", "maroc", "morocco", "president", "rabat", "sa", "sarl"},
        "verify_note": "Aggregator-sourced. OMPIC modèle J authoritative for officers.",
    },
    "MT": {
        "queries": [
            ("mbr", "site:mbr.mt {name}"),
            ("opencorp", "site:opencorporates.com Malta {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Secretary|CEO|Chairman|Managing\s+Director)\s*[:\-\|*]+',
        "bad_tokens": {"director", "limited", "ltd", "malta", "officer", "plc", "secretary", "sliema", "valletta"},
        "verify_note": "Aggregator-sourced. MBR Companies House free company snapshot.",
    },
    "MX": {
        "queries": [
            ("siger", "site:siger.economia.gob.mx {name}"),
            ("sat", "site:sat.gob.mx {name}"),
            ("opencorp", "site:opencorporates.com Mexico {name}"),
            ("mercantil", "site:mercantil.com {name}"),
            ("cuit", "site:cuit.mx {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es)?|Apoderado|Representante\s+Legal|Administrador(?:es)?|Socio(?:s)?|Accionista(?:s)?|Presidente|Gerente)\s*[:\-\|*]+',
        "bad_tokens": {"sa", "sapi", "cv", "srl", "sc", "limited", "company", "corporation", "group", "mexico", "guadalajara", "monterrey", "director", "directores", "apoderado", "representante", "legal", "administrador", "socio", "accionista", "presidente", "gerente"},
        "verify_note": "Aggregator-sourced. Verify via Acta Constitutiva or RPP extract.",
    },
    "MY": {
        "queries": [
            ("ssm", "site:ssm.com.my {name}"),
            ("bursa", "site:bursamalaysia.com {name}"),
            ("opencorp", "site:opencorporates.com Malaysia {name}"),
            ("mycompany", "site:mycompany.my {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Managing\s+Director|CEO|Chairman|Pengarah|Pengerusi)\s*[:\-\|*]+',
        "bad_tokens": {"sdn", "bhd", "berhad", "limited", "company", "group", "malaysia", "kuala", "lumpur", "penang", "johor", "director", "officer", "chairman", "managing", "pengarah"},
        "verify_note": "Aggregator-sourced. SSM e-Info portal (paid) authoritative.",
    },
    "NL": {
        "queries": [
            ("kvk", "site:kvk.nl {name}"),
            ("opencorp", "site:opencorporates.com Netherlands {name}"),
            ("companyinfo", "site:companyinfo.nl {name}"),
            ("dnb", "site:dnb.com {name} Netherlands"),
            ("ltddir", "site:ltddir.com {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:s)?|Bestuurder(?:s)?|Vertegenwoordiger|Officer(?:s)?|Principal(?:s)?|Authoris(?:ed|ed)\s+Person|Sole\s+Director|Manager(?:ial\s+Officer)?|Secretary|CEO|CFO|Voorzitter|Algemeen\s+Directeur)\s*[:\-\|*]+',
        "bad_tokens": {"director", "officer", "principal", "authorised", "authorized", "company", "limited", "corporation", "inc", "incorporated", "bv", "nv", "change", "timeline", "history", "section", "profile", "overview", "summary", "register", "registration", "address", "netherlands", "rotterdam", "amsterdam", "address", "phone", "email", "bestuurder", "voorzitter", "directeur", "algemeen", "kvk", "handelsregister"},
        "verify_note": "Aggregator-sourced. KVK Handelsregister Business Profile (paid, ~€2.20) authoritative.",
        "name_re": "nl",
    },
    "PA": {
        "queries": [
            ("rp", "site:rp.gob.pa {name}"),
            ("mef", "site:mef.gob.pa {name}"),
            ("opencorp", "site:opencorporates.com Panama {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Dignatario|Apoderado|Representante\s+Legal|Presidente|Tesorero|Secretario)\s*[:\-\|*]+',
        "bad_tokens": {"apoderado", "dignatario", "director", "limited", "panama", "presidente", "sa", "secretario", "srl", "tesorero"},
        "verify_note": "Aggregator-sourced. Registro Público de Panamá certificación digital available.",
    },
    "PE": {
        "queries": [
            ("sunat", "site:sunat.gob.pe {name}"),
            ("sunarp", "site:sunarp.gob.pe {name}"),
            ("opencorp", "site:opencorporates.com Peru {name}"),
            ("universidadperu", "site:universidadperu.com {name}"),
            ("rues", "site:rues.com.pe {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Gerente(?:\s+General)?|Presidente|Socio(?:s)?|Accionista(?:s)?|Administrador(?:es)?)\s*[:\-\|*]+',
        "bad_tokens": {"sa", "sac", "srl", "eirl", "limited", "company", "group", "peru", "lima", "arequipa", "trujillo", "director", "directorio", "apoderado", "presidente", "gerente", "general", "socio", "accionista", "administrador", "representante"},
        "verify_note": "Aggregator-sourced. SUNARP Vigencia de Poder extract authoritative (~PEN 25).",
    },
    "PL": {
        "queries": [
            ("krs", "site:ekrs.ms.gov.pl {name}"),
            ("rzetelnafirma", "site:rzetelnafirma.pl {name}"),
            ("opencorp", "site:opencorporates.com Poland {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Prezes|Wiceprezes|Członek\s+Zarządu|Prokurent)\s*[:\-\|*]+',
        "bad_tokens": {"director", "krakow", "limited", "poland", "prezes", "prokurent", "sa", "sk", "sp", "spk", "warsaw"},
        "verify_note": "Aggregator-sourced. KRS odpis aktualny free PDF download for any company.",
    },
    "PT": {
        "queries": [
            ("irn", "site:irn.justica.gov.pt {name}"),
            ("publicacoes", "site:publicacoes.mj.pt {name}"),
            ("opencorp", "site:opencorporates.com Portugal {name}"),
            ("einforma", "site:einforma.pt {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es)?|Administrador(?:es)?|Gerente|Presidente|Sócio)\s*[:\-\|*]+',
        "bad_tokens": {"administrador", "director", "gerente", "lda", "limited", "lisboa", "porto", "portugal", "presidente", "sa", "socio"},
        "verify_note": "Aggregator-sourced. IRN certidão permanente paid; einforma free baseline.",
    },
    "PY": {
        "queries": [
            ("set", "site:set.gov.py {name}"),
            ("opencorp", "site:opencorporates.com Paraguay {name}"),
            ("mecip", "site:mecip.gov.py {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente|Socio)\s*[:\-\|*]+',
        "bad_tokens": {"apoderado", "asuncion", "director", "gerente", "limited", "paraguay", "presidente", "sa", "srl"},
        "verify_note": "Aggregator-sourced. SET RUC free; AGN extract authoritative for Sociedad Anonima.",
    },
    "RO": {
        "queries": [
            ("onrc", "site:portal.onrc.ro {name}"),
            ("listafirme", "site:listafirme.ro {name}"),
            ("opencorp", "site:opencorporates.com Romania {name}"),
            ("mfinante", "site:mfinante.ro {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Administrator|CEO|Director\s+General|Asociat)\s*[:\-\|*]+',
        "bad_tokens": {"administrator", "asociat", "bucharest", "cluj", "director", "limited", "romania", "sa", "srl"},
        "verify_note": "Aggregator-sourced. ONRC portal.onrc.ro free company snapshot; certificat constatator paid.",
    },
    "SE": {
        "queries": [
            ("bolagsverket", "site:bolagsverket.se {name}"),
            ("allabolag", "site:allabolag.se {name}"),
            ("opencorp", "site:opencorporates.com Sweden {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|VD|Verkställande\s+Direktör|Styrelseordförande|Styrelseledamot|Suppleant)\s*[:\-\|*]+',
        "bad_tokens": {"ab", "director", "goteborg", "hb", "kb", "limited", "officer", "stockholm", "sverige", "sweden", "vd"},
        "verify_note": "Aggregator-sourced. Bolagsverket bolagsregistret extract authoritative (~SEK 60).",
    },
    "SI": {
        "queries": [
            ("ajpes", "site:ajpes.si {name}"),
            ("ers", "site:ers.gov.si {name}"),
            ("opencorp", "site:opencorporates.com Slovenia {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Direktor|Predsednik\s+Uprave|Član\s+Uprave)\s*[:\-\|*]+',
        "bad_tokens": {"dd", "director", "direktor", "doo", "limited", "ljubljana", "maribor", "officer", "slovenia", "sp"},
        "verify_note": "Aggregator-sourced. AJPES ePRS free public business register.",
    },
    "SV": {
        "queries": [
            ("mh", "site:mh.gob.sv {name}"),
            ("cnr", "site:cnr.gob.sv {name}"),
            ("opencorp", "site:opencorporates.com El Salvador {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Gerente)\s*[:\-\|*]+',
        "bad_tokens": {"director", "gerente", "limited", "presidente", "sa", "salvador", "sansalvador", "srl"},
        "verify_note": "Aggregator-sourced. CNR Certificación de Vigencia authoritative.",
    },
    "TH": {
        "queries": [
            ("dbd", "site:dbd.go.th {name}"),
            ("set", "site:set.or.th {name}"),
            ("opencorp", "site:opencorporates.com Thailand {name}"),
            ("creden", "site:creden.co {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Managing\s+Director|CEO|Chairman|Authoris(?:ed|ed)\s+Director|กรรมการ|กรรมการผู้จัดการ|ประธาน|ผู้บริหาร)\s*[:\-\|*：]+',
        "bad_tokens": {"co", "ltd", "limited", "pcl", "plc", "company", "group", "thailand", "bangkok", "director", "officer", "chairman", "managing"},
        "verify_note": "Aggregator-sourced. DBD (dbd.go.th) free certificate available; SET data authoritative for listed cos.",
    },
    "TW": {
        "queries": [
            ("moeaic", "site:gcis.nat.gov.tw {name}"),
            ("twse", "site:twse.com.tw {name}"),
            ("opencorp", "site:opencorporates.com Taiwan {name}"),
            ("findbiz", "site:findbiz.nat.gov.tw {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Chairman|代表人|董事長|董事|監察人|總經理)\s*[:\-\|*]+',
        "bad_tokens": {"chairman", "co", "director", "kaohsiung", "limited", "ltd", "officer", "taipei", "taiwan"},
        "verify_note": "Aggregator-sourced. MOEAIC gcis.nat.gov.tw free company-name lookup with directors.",
    },
    "UA": {
        "queries": [
            ("edr", "site:usr.minjust.gov.ua {name}"),
            ("opendatabot", "site:opendatabot.ua {name}"),
            ("opencorp", "site:opencorporates.com Ukraine {name}"),
            ("youcontrol", "site:youcontrol.com.ua {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Chairman|Директор|Голова|Засновник)\s*[:\-\|*]+',
        "bad_tokens": {"chairman", "director", "kiev", "kyiv", "limited", "lviv", "plt", "tov", "ukraine"},
        "verify_note": "Aggregator-sourced. EDR usr.minjust.gov.ua FREE official with directors + founders.",
    },
    "UY": {
        "queries": [
            ("dgi", "site:dgi.gub.uy {name}"),
            ("bcu", "site:bcu.gub.uy {name}"),
            ("opencorp", "site:opencorporates.com Uruguay {name}"),
            ("rues", "site:rues.com.uy {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director(?:es|io)?|Apoderado|Representante\s+Legal|Presidente|Sindico)\s*[:\-\|*]+',
        "bad_tokens": {"apoderado", "director", "limited", "montevideo", "presidente", "sa", "sas", "sindico", "srl", "uruguay"},
        "verify_note": "Aggregator-sourced. DGI RUT lookup free; Auditoria Interna de la Nación for full board.",
    },
    "VN": {
        "queries": [
            ("vietstock", "site:vietstock.vn {name}"),
            ("cafef", "site:cafef.vn {name}"),
            ("cophieu68", "site:cophieu68.vn {name}"),
            ("opencorp", "site:opencorporates.com Vietnam {name}"),
            ("thongtindoanhnghiep", "site:thongtindoanhnghiep.co {name}"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|Chairman|President|General\s+Director|Tổng\s+Giám\s+Đốc|Giám\s+Đốc|Chủ\s+Tịch|Người\s+đại\s+diện)\s*[:\-\|*：]+',
        "bad_tokens": {"jsc", "ltd", "limited", "corporation", "company", "group", "vietnam", "hanoi", "saigon", "hcmc", "director", "officer", "chairman", "president", "general"},
        "verify_note": "Aggregator-sourced. National Business Registration Portal (paid) is authoritative.",
    },
    "ZA": {
        "queries": [
            ("cipc", "site:cipc.co.za {name}"),
            ("jse", "site:jse.co.za {name}"),
            ("opencorp", "site:opencorporates.com South Africa {name}"),
            ("cipro", "site:eservices.cipc.co.za {name}"),
            ("dnb", "site:dnb.com {name} South Africa"),
        ],
        "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|CEO|CFO|Chairman|Secretary|Public\s+Officer|Managing\s+Director|Executive\s+Director)\s*[:\-\|*]+',
        "bad_tokens": {"pty", "ltd", "limited", "corporation", "company", "group", "cc", "south", "africa", "johannesburg", "capetown", "pretoria", "durban", "director", "officer", "chairman"},
        "verify_note": "Aggregator-sourced. CIPC e-services portal authoritative (paid per company report).",
    },
    # Caribbean — shared config, country_code passed per call
    "VG": {"_caribbean": True},
    "KY": {"_caribbean": True},
    "BS": {"_caribbean": True},
    "BM": {"_caribbean": True},
    "BB": {"_caribbean": True},
    "BZ": {"_caribbean": True},
    "KN": {"_caribbean": True},
    "JM": {"_caribbean": True},
    "VI": {"_caribbean": True},
    "TT": {"_caribbean": True},
    "MO": {"_alias": "HK"},  # Macau → reuse HK aggregators
}

_CARIBBEAN_CONFIG = {
    "queries": [
        ("opencorp", "site:opencorporates.com {name}"),
        ("icij", "site:offshoreleaks.icij.org {name}"),
        ("bvifsc", "site:bvifsc.vg {name}"),
        ("cima", "site:cima.ky {name}"),
        ("bma", "site:bma.bm {name}"),
    ],
    "director_labels": r'(?im)^\s*(?:\*\*|##\s*)?(?:Director|Officer|Beneficial\s+Owner|Authoris(?:ed|ed)\s+Person|Member|Shareholder)\s*[:\-\|*]+',
    "bad_tokens": {"ltd", "llc", "plc", "limited", "company", "incorporated", "offshore", "bvi", "cayman", "bahamas", "bermuda", "barbados", "belize", "jamaica", "islands", "director", "officer", "member", "shareholder"},
    "verify_note": "Caribbean offshore — official registries gated/paid (BVI FSC, Cayman CIMA, Bahamas FSC, Bermuda BMA). ICIJ Offshore Leaks supplements with leak data.",
}


def _resolve_config(country_code: str) -> Optional[dict]:
    """Resolve country config, handling aliases and Caribbean."""
    cc = country_code.upper()
    cfg = COUNTRIES.get(cc)
    if not cfg:
        return None
    if cfg.get("_alias"):
        cfg = COUNTRIES.get(cfg["_alias"])
    if cfg and cfg.get("_caribbean"):
        cfg = _CARIBBEAN_CONFIG.copy()
    return cfg


def _get_name_re(cfg: dict):
    """Get the name regex for a country config."""
    name_re_key = cfg.get("name_re", "default")
    if name_re_key == "de":
        return _DE_NAME_RE
    elif name_re_key == "nl":
        return _NL_NAME_RE
    return _DEFAULT_NAME_RE


# ---------------------------------------------------------------------------
# Firecrawl search + scrape
# ---------------------------------------------------------------------------

async def _fc_search(query: str, limit: int = 2) -> list:
    """Search via Firecrawl."""
    t0 = time.monotonic()
    req_body = {"query": query, "limit": limit}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _FC_SEARCH_URL,
            json=req_body,
            headers={"Authorization": f"Bearer {_FC_API_KEY}"},
        )
        latency = int((time.monotonic() - t0) * 1000)
        raw_store.store(
            source="firecrawl_search", entity_name=query,
            request_method="POST", request_url=_FC_SEARCH_URL,
            request_params=req_body,
            request_headers={"Authorization": f"Bearer {_FC_API_KEY}"},
            response_status=resp.status_code,
            response_headers=dict(resp.headers),
            response_body=resp.text, duration_ms=latency,
        )
        if resp.status_code != 200:
            log.warning("Firecrawl search failed: %d %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        return data.get("data", [])


async def _fc_scrape(url: str) -> str:
    """Scrape a page via Firecrawl, return markdown."""
    t0 = time.monotonic()
    req_body = {"url": url, "formats": ["markdown"]}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            _FC_SCRAPE_URL,
            json=req_body,
            headers={"Authorization": f"Bearer {_FC_API_KEY}"},
        )
        latency = int((time.monotonic() - t0) * 1000)
        raw_store.store(
            source="firecrawl_scrape", entity_name=url,
            request_method="POST", request_url=_FC_SCRAPE_URL,
            request_params=req_body,
            request_headers={"Authorization": f"Bearer {_FC_API_KEY}"},
            response_status=resp.status_code,
            response_headers=dict(resp.headers),
            response_body=resp.text, duration_ms=latency,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        return (data.get("data", {}).get("markdown") or "")[:50000]


# ---------------------------------------------------------------------------
# Director name extraction
# ---------------------------------------------------------------------------

def _looks_like_person_name(cand: str, co_tokens: set, bad_tokens: set,
                            max_words: int = 5) -> bool:
    """Check if a candidate string looks like a person name."""
    words = cand.split()
    if not (2 <= len(words) <= max_words):
        return False
    lo = cand.lower()
    if any(t in bad_tokens for t in lo.split()):
        return False
    if any(w.lower() in co_tokens for w in words):
        return False
    if cand.upper() == cand:
        return False
    return True


def _extract_directors(markdown: str, company_name: str, cfg: dict) -> list:
    """Extract director names from scraped markdown."""
    if not markdown:
        return []

    lines = markdown.splitlines()
    label_re = re.compile(cfg["director_labels"])
    name_re = _get_name_re(cfg)
    bad_tokens = cfg.get("bad_tokens", _DEFAULT_BAD_TOKENS) | _DEFAULT_BAD_TOKENS

    # Find lines matching director labels + next line
    candidate_idxs = []
    for i, ln in enumerate(lines):
        if label_re.search(ln):
            candidate_idxs.append(i)
            if i + 1 < len(lines):
                candidate_idxs.append(i + 1)

    co_tokens = set(re.sub(r"[^A-Za-z\s]", " ", company_name).lower().split())
    co_tokens.discard("limited")
    co_tokens.discard("inc")

    max_words = 5 if cfg.get("name_re") == "nl" else 5
    names = []
    seen = set()

    for idx in candidate_idxs:
        if idx >= len(lines):
            continue
        ln = lines[idx]
        for m in name_re.finditer(ln):
            cand = m.group(1).strip()
            if not _looks_like_person_name(cand, co_tokens, bad_tokens, max_words):
                continue
            key = cand.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(cand)

    return names


# ---------------------------------------------------------------------------
# Main lookup function
# ---------------------------------------------------------------------------

async def lookup(country_code: str, entity_name: str) -> Optional[dict]:
    """
    Look up an entity via aggregator web scraping.

    Returns None if country not supported.
    Returns dict with verified, officers, sources, verify_note.
    """
    if not _FC_API_KEY:
        return {"verified": False, "error": "firecrawl-api-key not configured",
                "country_code": country_code}

    cfg = _resolve_config(country_code)
    if not cfg:
        return None

    t0 = time.monotonic()
    directors_found = []  # (name, url, label)
    urls_consulted = []

    # Search all configured aggregator sites
    queries = cfg.get("queries", [])
    for label, tmpl in queries:
        q = tmpl.format(name=entity_name)
        try:
            hits = await _fc_search(q, limit=2)
        except Exception as exc:
            log.warning("Aggregator %s/%s search failed: %s", country_code, label, exc)
            continue

        for h in hits[:2]:
            url = (h.get("url") or "").strip()
            if not url:
                continue
            urls_consulted.append(url)

            # Use markdown from search result if available, else scrape
            md = (h.get("markdown") or "").strip()
            if not md:
                try:
                    md = await _fc_scrape(url)
                except Exception:
                    continue
            if not md:
                continue

            names = _extract_directors(md, entity_name, cfg)
            for n in names:
                if n not in [x[0] for x in directors_found]:
                    directors_found.append((n, url, label))

    duration_ms = int((time.monotonic() - t0) * 1000)

    officers = [
        {
            "name": n,
            "role": "Director (aggregator-sourced)",
            "source_url": url,
            "source_label": label,
        }
        for n, url, label in directors_found
    ]

    return {
        "entity_name": entity_name,
        "country_code": country_code.upper(),
        "verified": len(officers) > 0,
        "legal_name": entity_name,  # aggregators don't give us a canonical name
        "status": "unknown",
        "officers": officers,
        "sources_consulted": urls_consulted[:20],
        "verify_note": cfg.get("verify_note", "Aggregator-sourced. Not authoritative."),
        "validation_source": {
            "registry": f"Aggregator scrape ({country_code.upper()})",
            "url": urls_consulted[0] if urls_consulted else None,
            "how_to_reproduce": f"Search the listed aggregator sites for '{entity_name}'",
            "verified_at": None,
        },
        "duration_ms": duration_ms,
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }


def supported_countries() -> list:
    """Return list of supported country codes."""
    return sorted(COUNTRIES.keys())


async def health() -> dict:
    """Health check."""
    return {
        "status": "up" if _FC_API_KEY else "disabled (no firecrawl-api-key)",
        "countries": len(COUNTRIES),
        "country_list": supported_countries(),
    }
