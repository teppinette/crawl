---
name: region_europe
description: Europe region OSINT registries for Turkey, Russia, Belarus, Serbia, EU, and Nigeria counterparty research. DeepSeek model routing for Russian-language sources.
---

# Europe Region -- Registry Guide

This skill supplements `counterparty_research` with Europe/Africa-specific sources.

## Model Routing
- **Default**: claude-sonnet-4-6
- **Russian-language sources** (EGRUL, Spark-Interfax, court records): switch to deepseek-chat
- **Turkish sources**: Claude + Azure Translator if needed
- Store all name variants: original script + English transliteration + aliases

## TURKEY (DEEP COVERAGE)

Turkish government portals can be slow. Retry at least 3x with 30-second delays.

### Corporate Registries
- **MERSIS**: mersis.gtb.gov.tr -- central commercial registry
  - Company search by name or MERSIS number
  - Returns: registration status, authorized capital, shareholders, activity codes
  - NOTE: Portal may require Turkish IP or be intermittent. Retry 3x.
- **TTSG (Trade Gazette)**: ilan.gov.tr -- official gazette announcements
  - Company formation, capital changes, board changes, liquidation notices
  - Search by company name or tax ID
- **e-Devlet**: turkiye.gov.tr -- government portal (limited public access)
- **KAP**: kap.org.tr -- public disclosure platform for listed companies
  - Financial statements, material event disclosures, board decisions
  - Free, comprehensive data for Borsa Istanbul listed entities
- **Borsa Istanbul**: borsaistanbul.com -- stock exchange
- **Istanbul Chamber of Commerce**: ito.org.tr -- member directory
- **Istanbul Chamber of Industry**: iso.org.tr -- top 500/1000 manufacturers
- **TOBB**: tobb.org.tr -- Union of Chambers (chamber of commerce network)
- **GIB (Revenue Administration)**: gib.gov.tr -- tax ID verification
  - Verify tax number (Vergi Kimlik Numarasi) validity

### Financial Regulators
- **BDDK (BRSA)**: bddk.org.tr -- Banking Regulation and Supervision Agency
  - Licensed banks, financial leasing, factoring companies
  - Enforcement actions and fines
- **SPK (CMB)**: spk.gov.tr -- Capital Markets Board
  - Licensed intermediaries, enforcement actions, fines
  - Insider trading cases, market manipulation
- **MASAK**: masak.hmb.gov.tr -- Financial Crimes Investigation Board (Turkey's FIU)
  - AML/CFT enforcement, asset freezing orders
  - CRITICAL: Check for entity + directors against MASAK actions
- **TCMB**: tcmb.gov.tr -- Central Bank of Turkey
  - Licensed payment institutions, electronic money companies
- **SDIF (TMSF)**: tmsf.org.tr -- Savings Deposit Insurance Fund
  - Seized companies, assets under SDIF management
  - CRITICAL for DD: seized entities are high-risk

### Courts and Legal
- **UYAP**: uyap.gov.tr -- National Judiciary Informatics System
  - Case search (limited public access)
  - Search entity AND all directors as parties
- **Constitutional Court**: anayasa.gov.tr -- individual applications
- **Council of State (Danistay)**: danistay.gov.tr -- administrative court
- **Court of Cassation (Yargitay)**: yargitay.gov.tr -- supreme court decisions
- **Competition Board**: rekabet.gov.tr -- antitrust decisions
  - Searchable database of competition enforcement
- **Turkish Patent Office (TURKPATENT)**: turkpatent.gov.tr -- trademark/patent search
- **Enforcement and Bankruptcy Offices**: Search for bankruptcy proceedings

### Sanctions & Compliance
- **MASAK frozen assets list**: Check entity + directors
- **EU sanctions**: Turkey is NOT aligned with EU Russia sanctions — flag this
- **OFAC**: Check for Turkey-related designations (Iran/Russia routing)
- **UN sanctions**: Turkey implements UN sanctions

### Turkish Media (CRITICAL — search all)
- **Daily Sabah**: dailysabah.com — English-language
- **Hurriyet Daily News**: hurriyetdailynews.com — English
- **Bianet**: bianet.org — independent news (English section)
- **Anadolu Agency**: aa.com.tr/en — state news agency
- **Bloomberg HT**: bloomberght.com — financial news (Turkish)
- **Dunya**: dunya.com — business daily (Turkish)
- Search: "{entity}" site:dailysabah.com OR site:hurriyetdailynews.com
- Search: "{director}" fraud OR arrested OR MASAK OR seized

### Trade & Customs
- **Turkish Exporters Assembly (TIM)**: tim.org.tr -- exporter database
- **GTB (Trade Ministry)**: ticaret.gov.tr -- trade statistics, importer/exporter verification
- **Turkish Customs**: gumrukler.gov.tr -- customs enforcement, seizures

### Turkey Risk Factors
- Major transshipment hub for Russia sanctions evasion since 2022
- Turkish free zones (Mersin, Antalya, Istanbul) used for re-export
- Dual-use chemical trade: caustic soda, calcium hypochlorite, industrial chemicals
- Iran-Turkey trade corridors — gold-for-gas schemes (historical)
- TMSF (SDIF) seized companies: hundreds of companies seized post-2016
- Director screening against FETO-related entity lists
- Currency risk: Lira volatility may affect financial statement analysis
- Shell companies in Mersin Free Zone — flag if entity registered there

### Turkey Output Requirements
- Always check MERSIS registration status
- Verify tax ID (Vergi Kimlik No) if provided
- Check MASAK frozen assets for entity + all directors
- Flag any TMSF/SDIF connection
- Note if entity operates from a free zone
- Search all directors individually against courts + media

## Russia

### Corporate Registries
- **EGRUL**: egrul.nalog.ru -- unified state register (USE DEEPSEEK MODEL)
- **Spark-Interfax**: spark-interfax.ru -- commercial database
- **RSPP**: rspp.ru -- Russian Union of Industrialists

### Sanctions (CRITICAL)
- **OFAC SDN**: primary -- many Russian entities sanctioned since 2022
- **EU consolidated list**: check all Russian sanctions packages (1-14+)
- **UK Russia sanctions**: gov.uk/government/publications/the-uk-sanctions-list
- **Swiss SECO**: seco.admin.ch -- follows EU but with exceptions
- Check for sector-specific: energy, finance, defense, luxury goods

### Special Attention
- Gazpromneft/NIS (Serbia) -- OFAC 50% rule applies to subsidiaries
- Russian state-owned enterprises: Rosneft, Gazprom, Transneft subsidiaries
- Shell companies in Cyprus, UAE, Turkey used for sanctions evasion
- Parallel imports schemes

## Belarus

### Corporate Registries
- **EGR**: egr.gov.by -- unified state register
- Closely linked to Russian sanctions regime
- Check for transshipment of sanctioned goods via Belarus

## Serbia

### Corporate Registries
- **APR**: apr.gov.rs -- Serbian Business Registers Agency
- **NBS**: nbs.rs -- National Bank (for financial entities)

### Special Attention
- NIS a.d. Novi Sad (55% Gazpromneft-owned) -- OFAC 50% rule flag
- Serbia not aligned with EU sanctions on Russia

## EU General

### Sanctions
- **EU Sanctions Map**: sanctionsmap.eu -- consolidated EU restrictive measures
- **Europol**: europol.europa.eu -- most wanted, organized crime
- **OpenSanctions**: opensanctions.org -- aggregated global sanctions data

## Nigeria

### Corporate Registries
- **CAC**: cac.gov.ng -- Corporate Affairs Commission
- **SEC Nigeria**: sec.gov.ng -- Securities and Exchange Commission

### Special Attention
- Oil trading entities: check NNPC connections
- PEP risk is elevated -- cross-reference directors against political figures
- Check for connections to sanctioned individuals via shell structures
