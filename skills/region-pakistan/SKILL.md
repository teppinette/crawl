---
name: region_pakistan
description: Pakistan-dedicated OSINT registries for counterparty research. SECP, FBR, NAB, SBP, eCourts, DRAP, Pakistani media. Aggressive retry on PK government portals.
---

# Pakistan Region -- Registry Guide

This skill supplements `counterparty_research` with Pakistan-specific sources.
Pakistan government portals are frequently slow or intermittently down.
ALWAYS retry failed requests at least 3 times with 10-second delays.

## Model Configuration
- Primary: claude-sonnet-4-6
- Urdu sources: transliterate all Urdu names to Latin script in output
- All Pakistani government sites are SLOW — use 60-second timeouts minimum

## Corporate Registries

### SECP (Securities and Exchange Commission of Pakistan)
- **eServices Portal**: eservices.secp.gov.pk
  - Company Name Search: search by exact or partial name
  - Company Number Search: use SECP registration number (e.g., 0089758)
  - Returns: incorporation date, status, registered office, company type
  - NOTE: Portal has frequent DNS issues. If unavailable:
    1. Try again after 30 seconds
    2. Try alternative URL: https://www.secp.gov.pk/company-name-search/
    3. Fall back to OpenCorporates: opencorporates.com/companies/pk/{number}
- **Form A (Directors)**: Not publicly accessible — note as gap
- **Form 29 (Annual Returns)**: Not publicly accessible — note as gap
- **Beneficial Ownership Register**: SECP requires BO declarations but they are NOT public
- SECP company numbers: typically 7 digits (e.g., 0089758)

### FBR (Federal Board of Revenue) -- TAX VERIFICATION
- **Active Taxpayer List (ATL)**: e.fbr.gov.pk
  - NTN Verification: Enter NTN number (format: 7digits-checkdigit, e.g., 4334750-9)
  - Returns: taxpayer name, NTN, status (Active/Inactive), tax office, registration date
  - Alternative: https://e.fbr.gov.pk/Registration/STRVerification.aspx
  - IRIS portal: iris.fbr.gov.pk -> Inquiry -> Verification of Registration
  - If portal is down, note NTN as UNVERIFIED and flag for manual check
- **Sales Tax Registration**: Check if entity is registered for sales tax
- **Customs registration**: Verify importer/exporter code

### Pakistan Stock Exchange (PSX)
- **Listed companies**: psx.com.pk
- **Company disclosures**: Check if entity or parent group is listed
- **Annual reports**: Available for listed companies

## Regulatory & Compliance Databases

### NAB (National Accountability Bureau)
- **Website**: nab.gov.pk
- **Mega corruption cases list**: Check if entity/directors appear
- **Voluntary Return program**: Check if directors participated
- NAB has jurisdiction over corruption, fraud, and money laundering
- Search entity name AND all director names individually

### SBP (State Bank of Pakistan)
- **Banking license verification**: sbp.org.pk
- **Schedule of banks**: Verify if entity claims banking relationships
- **AML/CFT circulars**: Check for relevant sector warnings
- **Exchange companies list**: If entity deals in forex
- **Green list / sanctions**: SBP maintains its own sanctions watchlist

### DRAP (Drug Regulatory Authority of Pakistan)
- **Website**: drap.gov.pk
- **Product registration search**: Verify if agrochemical/pharma products are registered
- **Manufacturing license**: Check if entity holds valid manufacturing license
- **Import license**: Critical for companies importing chemicals/drugs
- For agrochemical companies, also check:
  - Department of Plant Protection (DPP): plantprotection.gov.pk
  - Pesticide registration certificates

### SECP Specialized Registries
- **Insurance companies**: Check SECP insurance division
- **NBFCs (Non-Banking Finance Companies)**: SECP regulated
- **Modaraba companies**: Islamic finance entities
- **Private equity/VC funds**: SECP regulated

## Court Records & Legal

### eCourts Pakistan
- **Supreme Court**: supremecourt.gov.pk/cause-list
- **Lahore High Court**: lhc.gov.pk -- case search
- **Sindh High Court**: sindhhighcourt.gov.pk
- **Islamabad High Court**: ihc.gov.pk
- **Peshawar High Court**: peshawarhighcourt.gov.pk
- **Balochistan High Court**: bhc.gov.pk
- Search by: party name, advocate name, case number
- Check both as plaintiff AND defendant

### NCLT Pakistan (National Company Law Tribunal equivalent)
- Company winding-up petitions
- Corporate restructuring cases

### Pakistani Kanoon (Legal Database)
- pakistanlawsite.com — case law database
- Search for entity and director names in judgments

## Media Sources

### Pakistani English-Language Media
- **Dawn**: dawn.com — Pakistan's leading English daily
- **The News International**: thenews.com.pk
- **Geo News**: geo.tv
- **Express Tribune**: tribune.com.pk
- **Business Recorder**: brecorder.com — financial/business news
- **Pakistan Today**: pakistantoday.com.pk

### Urdu Media (translate findings)
- **Jang**: jang.com.pk
- **Daily Express**: express.com.pk

### Search Strategy
- Search: "{entity_name}" site:dawn.com OR site:thenews.com.pk OR site:geo.tv
- Search: "{director_name}" fraud OR scam OR arrest OR NAB
- Search: "{entity_name}" FIR OR case OR court OR investigation

## Trade & Import Data

### Pakistan Customs (WeBOC)
- **Web Based One Customs (WeBOC)**: wms.customs.gov.pk
- Import/export declarations
- Customs duty payments

### NBD Trade Data
- en.nbd.ltd — China-Pakistan customs records
- Search by company name for import volumes, suppliers, products
- Critical for verifying claimed business activity

### TDAP (Trade Development Authority)
- tdap.gov.pk
- Registered exporters database

## Tax & Financial

### FBR Additional Checks
- **Withholding tax agent**: Check if entity is WHT agent
- **Import/export code (IEC)**: Verify with customs
- **STRN (Sales Tax Registration Number)**: Verify active status

### Provincial Revenue
- **Punjab Revenue Authority**: pra.punjab.gov.pk
- **Sindh Revenue Board**: srb.gos.pk
- **KP Revenue Authority**: kpra.kp.gov.pk

## Property & Land Records

### Provincial Land Records
- **Punjab**: zameen.punjab.gov.pk — Punjab Land Records Authority
- **Sindh**: Board of Revenue Sindh
- **KP**: Board of Revenue KP
- Cross-reference registered address with land ownership

## Sanctions Context

### Pakistan-Specific Risk Factors
- Pakistan was on FATF grey list (removed Oct 2022) but elevated AML/CFT risk remains
- Chemical/agrochemical sector has dual-use concerns (CWC precursors)
- China-Pakistan trade corridor (CPEC) creates complex commercial relationships
- Military-linked businesses (Fauji Foundation, Army Welfare Trust, Shaheen Foundation)
- Religious seminary-linked entities — check for designated entity connections
- Hawala/hundi networks — informal value transfer systems

### Key Sanctions Lists to Cross-Reference
- OFAC SDN/SSI (US)
- UN Security Council consolidated list
- Pakistan's own proscribed organizations list (Ministry of Interior)
- SBP sanctions list
- NACTA (National Counter Terrorism Authority) proscribed list

## Research Output Format

Always output ALL of these fields, even if data is unavailable (mark as NOT_FOUND_PUBLIC):

```json
{
  "counterparty_name": "",
  "country": "Pakistan",
  "country_code": "PK",
  "research_date": "YYYY-MM-DD",
  "research_region": "pakistan",
  "corporate_registry": {
    "secp_number": "",
    "ntn_number": "",
    "ntn_status": "VERIFIED_ACTIVE | VERIFIED_INACTIVE | UNVERIFIED",
    "strn_number": "",
    "status": "",
    "incorporation_date": "",
    "registered_address": "",
    "legal_form": "",
    "directors": [],
    "shareholders": [],
    "websites": [],
    "parent_group": "",
    "source_url": "",
    "source_date": ""
  },
  "business_profile": {},
  "beneficial_ownership": {},
  "sanctions_screening": {},
  "nab_screening": {
    "entity_check": "CLEAR | HIT | UNVERIFIED",
    "director_checks": []
  },
  "adverse_media": [],
  "litigation": [],
  "pep_connections": [],
  "trade_risk": {},
  "risk_assessment": "",
  "risk_score": 0,
  "risk_rationale": "",
  "sources": [],
  "research_notes": ""
}
```

## CRITICAL RULES
1. NEVER include COPAP, customer names, or any internal references in output
2. ALWAYS retry Pakistani government portals at least 3 times before marking as unavailable
3. ALWAYS search directors/UBOs individually against NAB, courts, and media
4. For NTN verification: explicitly state VERIFIED or UNVERIFIED with reason
5. Transliterate ALL Urdu text to Latin script
6. Note SECP portal status in research_notes (up/down/partial)
