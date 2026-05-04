---
name: region_gulf
description: Gulf region OSINT registries for UAE, Pakistan, Egypt, Iraq, Saudi Arabia, Qatar, Bahrain, Kuwait, Oman, and Jordan. Deep coverage for Pakistan (SECP, FBR, NAB, eCourts) and UAE (free zones, CBUAE, DIFC). Focus on free zone opacity and Iran sanctions evasion.
---

# Gulf Region -- Registry Guide

This skill supplements `counterparty_research` with Gulf/MENA-specific sources.

## Model Configuration
- Primary: claude-sonnet-4-6
- Arabic sources: Claude handles Arabic well; transliterate all names to Latin script
- Urdu sources: transliterate to Latin script in output
- Pakistani/UAE government portals are SLOW — use 60-second timeouts, retry 3x

---

## PAKISTAN (DEEP COVERAGE)

Pakistan government portals are frequently slow or intermittently down.
ALWAYS retry failed requests at least 3 times with 10-second delays.

### Corporate Registries

#### SECP (Securities and Exchange Commission of Pakistan)
- **eServices Portal**: eservices.secp.gov.pk — NO CAPTCHA, works programmatically
  - **IMPORTANT: Use POST method below, do NOT rely on browser-based search**
  - **NameSearch** (registration details):
    ```
    curl -s -X POST 'https://eservices.secp.gov.pk/eServices/ControllerServlet' \
      -d 'request_id=SEARCH_NAME&searchName=ENTITYNAME&searchOption=Beginning+With&requesterProcess=' \
      -H 'Referer: https://eservices.secp.gov.pk/eServices/NameSearch.jsp' \
      -H 'Content-Type: application/x-www-form-urlencoded'
    ```
    Returns HTML table with: legal name, status, CRO, registration number, registration date, Form A/B filing date
  - **CTC_CompanySearch** (company type + active status):
    ```
    curl -s -X POST 'https://eservices.secp.gov.pk/eServices/ControllerServlet' \
      -d 'request_id=CTC_SEARCH_COMPANY&searchName=ENTITYNAME&searchOption=Beginning+With&requesterProcess=null' \
      -H 'Referer: https://eservices.secp.gov.pk/eServices/CTC_CompanySearch.jsp' \
      -H 'Content-Type: application/x-www-form-urlencoded'
    ```
    Returns: CRO, registration number, date, ACTIVE/INACTIVE status, company type (Private Limited, Public, etc.)
  - **Name format quirk**: SECP stores names without spaces in the company part.
    "Agro China Pakistan" is registered as "AGROCHINA PAKISTAN (PVT.) LIMITED".
    Always try collapsed name (AGROCHINA) AND original (AGRO CHINA) as search variants.
  - searchOption values: "Exact Name", "Beginning With", "Including Exact String"
  - Parse results from `<TD class="tableText">` cells in the HTML response
  - "were found according to given criteria" in response = results exist
  - "No Company according to given criteria" = no match
- **Form A (Directors/Shareholders)**: Not publicly accessible — note as gap
- **Form 29 (Annual Returns)**: Not publicly accessible
- **Beneficial Ownership Register**: SECP requires BO declarations but NOT public
- SECP company numbers: typically 7 digits (e.g., 0089758)

#### FBR (Federal Board of Revenue) — TAX VERIFICATION (CRITICAL)
- **Active Taxpayer List (ATL)**: e.fbr.gov.pk
  - NTN Verification: Enter NTN number (format: 7digits-checkdigit, e.g., 4334750-9)
  - Returns: taxpayer name, NTN, status (Active/Inactive), tax office, registration date
  - Alternative: https://e.fbr.gov.pk/Registration/STRVerification.aspx
  - IRIS portal: iris.fbr.gov.pk -> Inquiry -> Verification of Registration
  - If portal is down, note NTN as UNVERIFIED and flag for manual check
- **Sales Tax Registration (STRN)**: Check if entity is registered for sales tax
- **Customs registration**: Verify importer/exporter code
- **Withholding tax agent**: Check if entity is WHT agent

#### Pakistan Stock Exchange (PSX)
- psx.com.pk — Check if entity or parent group is listed
- Annual reports and disclosures available for listed companies

### Regulatory & Compliance

#### NAB (National Accountability Bureau) — CORRUPTION CHECK
- nab.gov.pk — Mega corruption cases list
- Search entity name AND all director names individually
- Check Voluntary Return program participation
- NAB has jurisdiction over corruption, fraud, and money laundering

#### SBP (State Bank of Pakistan)
- sbp.org.pk — Banking license verification
- Schedule of banks, exchange companies list
- SBP maintains its own sanctions/watchlist
- AML/CFT circulars for relevant sector warnings

#### DRAP (Drug Regulatory Authority of Pakistan)
- drap.gov.pk — Product registration search
- For agrochemical companies: check Department of Plant Protection (plantprotection.gov.pk)
- Verify pesticide registration certificates, import licenses, manufacturing licenses

#### NACTA (National Counter Terrorism Authority)
- Proscribed organizations list (Ministry of Interior)
- Check entity and directors against terror financing lists

### Pakistan Court Records

- **Supreme Court**: supremecourt.gov.pk/cause-list
- **Lahore High Court**: lhc.gov.pk — case search by party name
- **Sindh High Court**: sindhhighcourt.gov.pk
- **Islamabad High Court**: ihc.gov.pk
- **Peshawar High Court**: peshawarhighcourt.gov.pk
- **Balochistan High Court**: bhc.gov.pk
- Search entity as both plaintiff AND defendant
- Search each director/UBO name individually
- pakistanlawsite.com — case law database

### Pakistan Media (CRITICAL — search all)

- **Dawn**: dawn.com — Pakistan's leading English daily
- **The News International**: thenews.com.pk
- **Geo News**: geo.tv
- **Express Tribune**: tribune.com.pk
- **Business Recorder**: brecorder.com — financial/business news
- **Pakistan Today**: pakistantoday.com.pk
- Search: "{entity}" site:dawn.com OR site:thenews.com.pk OR site:geo.tv
- Search: "{director}" fraud OR scam OR arrest OR NAB OR FIA

### Pakistan Trade Data

- **NBD Trade Data**: en.nbd.ltd — China-Pakistan customs records
- **WeBOC**: wms.customs.gov.pk — Pakistan customs import/export
- **TDAP**: tdap.gov.pk — registered exporters database

### Pakistan Risk Factors
- FATF grey list removed Oct 2022 but elevated AML/CFT risk remains
- Chemical/agrochemical sector has dual-use concerns
- CPEC creates complex China-Pakistan commercial relationships
- Military-linked businesses (Fauji Foundation, Army Welfare Trust)
- Hawala/hundi networks — informal value transfer
- Cross-border trade with Iran and Afghanistan

### Pakistan Output Requirements
Always include these fields for PK entities:
- `ntn_number`: The NTN with verification status
- `ntn_status`: "VERIFIED_ACTIVE" | "VERIFIED_INACTIVE" | "UNVERIFIED"
- `secp_number`: SECP registration number
- `nab_screening`: CLEAR | HIT | UNVERIFIED for entity + each director

---

## UAE (DEEP COVERAGE)

### Corporate Registries (by jurisdiction — CHECK ALL RELEVANT)

#### Federal / Abu Dhabi
- **Ministry of Economy**: moec.gov.ae — federal trade license registry
- **Abu Dhabi DED**: added.gov.ae — Abu Dhabi economic licenses
- **ADGM**: adgm.com — Abu Dhabi Global Market (financial free zone)
  - Company search: adgm.com/doing-business/registry-services/company-search
  - Returns: company name, license number, status, registered agent
- **Khalifa Industrial Zone (KIZAD)**: kizad.ae
- **twofour54**: twofour54.com — media zone

#### Dubai
- **Dubai DED**: dubaided.gov.ae — Department of Economic Development
  - Trade license search by name or license number
- **JAFZA**: jafza.ae — Jebel Ali Free Zone (major trading hub)
  - One of the world's largest free zones — 8,000+ companies
  - Check registration status, license type, activity codes
- **DMCC**: dmcc.ae — Dubai Multi Commodities Centre
  - Company directory: dmcc.ae/gateway-to-trade/member-directory
  - Heavy in gold, diamonds, tea, commodities trading
- **DIFC**: difc.ae — Dubai International Financial Centre
  - Company search: difc.ae/public-register
  - Financial services entities, holding companies
- **DAFZA**: dafza.ae — Dubai Airport Free Zone
- **DSO**: dso.ae — Dubai Silicon Oasis (tech)
- **Dubai South**: dubaisouth.ae — logistics/aviation zone
- **Dubai Maritime City**: dmca.ae

#### Northern Emirates
- **RAKEZ**: rakez.com — Ras Al Khaimah Economic Zone
  - Known for easy incorporation, low cost — higher risk for shell companies
- **RAK ICC**: rakicc.com — RAK International Corporate Centre
  - International business companies — offshore-like structures
- **SAIF Zone**: saif-zone.com — Sharjah Airport International Free Zone
- **Ajman Free Zone**: afz.gov.ae
- **UAQ Free Trade Zone**: uaqftz.com — Umm Al Quwain
- **Fujairah Free Zone**: fujairahfreezone.com
  - Major oil storage/bunkering — check for sanctioned oil trade

### UAE Financial Regulators
- **CBUAE**: centralbank.ae — Central Bank of UAE
  - Licensed financial institutions list
  - AML/CFT sanctions and enforcement actions
  - Exchange house licenses
- **SCA**: sca.gov.ae — Securities and Commodities Authority
  - Listed companies, licensed brokers
- **DFSA**: dfsa.ae — DIFC Financial Services Authority
  - DIFC-regulated entities, enforcement actions

### UAE Courts
- **DIFC Courts**: difccourts.ae — searchable judgment database
  - Major commercial disputes, enforcement actions
  - Check entity AND directors as parties
- **Dubai Courts**: dc.gov.ae
- **Abu Dhabi Judicial Department**: adjd.gov.ae
- **Federal Supreme Court**: fsc.gov.ae

### UAE Sanctions Context (CRITICAL)
- UAE is THE major transshipment hub for sanctions evasion
- Iranian front companies frequently register in UAE free zones
- OFAC has designated many UAE-based entities for Iran sanctions evasion
- Check for: same registered agent, shared office address, recently incorporated
- Free zone entities often have opaque ownership — note when UBO cannot be traced
- Gold/precious metals dealers: high AML risk
- Oil trading through Fujairah: check for Russian/Iranian oil circumvention
- Patterns to flag:
  - Same registered agent across multiple entities
  - Shared PO Box / virtual office addresses
  - Recently incorporated with high trade volumes
  - Trading patterns: UAE -> Iran/Syria via third-country routing

### UAE Media
- **Gulf News**: gulfnews.com
- **Khaleej Times**: khaleejtimes.com
- **The National**: thenationalnews.com
- **Arabian Business**: arabianbusiness.com
- Search: "{entity}" site:gulfnews.com OR site:khaleejtimes.com

### UAE Output Requirements
- Always identify which free zone / emirate the entity is registered in
- Flag RAK ICC and RAK free zone entities as higher risk (easier incorporation)
- Check DIFC Courts for any judgments involving the entity
- Note if entity has trade licenses in multiple free zones (potential layering)

---

## SAUDI ARABIA

### Corporate Registries
- **MCI**: mc.gov.sa — Ministry of Commerce (company search)
- **SAGIA**: sagia.gov.sa — Saudi Arabian General Investment Authority
- **Tadawul**: tadawul.com.sa — stock exchange
- **ZATCA**: zatca.gov.sa — Zakat, Tax and Customs Authority

### Special Attention
- Vision 2030 entities: check government ownership percentage
- Check for listed entity on Tadawul
- SAMA (Saudi Central Bank): licensed financial institutions

---

## EGYPT

### Corporate Registries
- **GAFI**: gafi.gov.eg — General Authority for Investment
- Commercial registry (varies by governorate)
- **EGX**: egx.com.eg — Egyptian Exchange (listed entities)
- **CBE**: cbe.org.eg — Central Bank (licensed banks)

### Special Attention
- Military-connected entities are PEP-adjacent (Egyptian military has large commercial portfolio)
- Check OFAC Egypt-related designations
- Free zone entities in Suez Canal Economic Zone

---

## IRAQ

### Corporate Registries
- Limited online registry access — focus on adverse media and sanctions screening
- Kurdistan Region has separate commercial registration
- **ISX**: isx-iq.net — Iraq Stock Exchange

### Special Attention
- Former Ba'ath party connections (historical)
- Oil smuggling networks
- Cross-border trade with Iran and Syria
- Check OFAC Iraq-related designations

---

## QATAR / BAHRAIN / KUWAIT / OMAN / JORDAN

### Qatar
- **QFC**: qfc.qa — Qatar Financial Centre (company search)
- **QFMA**: qfma.org.qa — Financial Markets Authority
- **QSE**: qe.com.qa — Qatar Stock Exchange

### Bahrain
- **MOIC**: moic.gov.bh — Ministry of Industry & Commerce (CR search)
- **CBB**: cbb.gov.bh — Central Bank of Bahrain (licensed entities)
- **Bahrain Bourse**: bahrainbourse.com

### Kuwait
- **MOCI**: moci.gov.kw — Ministry of Commerce
- **CBK**: cbk.gov.kw — Central Bank of Kuwait
- **Boursa Kuwait**: boursakuwait.com.kw

### Oman
- **MOCIIP**: mociip.gov.om — Ministry of Commerce
- **CBO**: cbo.gov.om — Central Bank of Oman
- **MSM**: msm.gov.om — Muscat Securities Market

### Jordan
- **MoIT**: mit.gov.jo — Ministry of Industry and Trade
- **CBJ**: cbj.gov.jo — Central Bank of Jordan
- **ASE**: ase.com.jo — Amman Stock Exchange

---

## Cross-Regional Patterns to Flag

- Same beneficial owner across multiple UAE free zones
- Recently incorporated entities with large trade volumes
- Registered agents known for facilitating sanctions evasion
- Trading patterns: Gulf -> Iran/Syria via third-country routing
- Chemical precursor shipments requiring end-user certificates
- Pakistan-China-UAE triangular trade patterns
- Shell companies in RAK/Ajman with no physical presence
- Directors appearing in NAB (Pakistan) + DIFC Courts (UAE)

## CRITICAL RULES
1. NEVER include COPAP, customer names, or any internal references in output
2. ALWAYS retry government portals at least 3 times before marking unavailable
3. ALWAYS search directors/UBOs individually against NAB, courts, sanctions, media
4. For Pakistan NTN: explicitly state VERIFIED or UNVERIFIED with reason
5. Transliterate ALL Arabic/Urdu text to Latin script
6. Note portal availability status in research_notes
