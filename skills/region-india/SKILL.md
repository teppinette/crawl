---
name: region_india
description: India OSINT registries for MCA21, ROC, GST, courts, SEBI, RBI. Sarvam AI for Hindi court records. DIN cross-referencing for hidden group structures.
metadata:
  openclaw:
    requires:
      env: ["ANTHROPIC_API_KEY"]
---

# India Region -- Registry Guide

This skill supplements `counterparty_research` with India-specific sources.

## Model Routing
- **Default**: claude-sonnet-4-6
- **Hindi/regional language court records**: sarvam-2b-v0.5 (Sarvam AI)
- **Bulk processing**: deepseek-chat (cost optimization)

## India

### Corporate Registries (PRIMARY)
- **MCA21**: mca.gov.in -- Ministry of Corporate Affairs
  - Company master data (CIN, incorporation date, authorized/paid-up capital, status)
  - Director Information (DIN lookup): check ALL directors
  - Charge documents: who has security interest
  - Annual returns and financial statements (when filed)
  - Strike-off and dormant status
- **ROC filings**: available via MCA21 -- check for delayed filings (red flag)

### Tax and Registration
- **GST Portal**: gst.gov.in -- verify active GSTIN, registered address, business type
  - GSTIN format: 2-digit state + 10-digit PAN + entity code + check digit
  - Cross-reference GST address with MCA21 registered office
- **PAN verification**: incometaxindia.gov.in (limited public access)
- **IEC (Import Export Code)**: dgft.gov.in -- Directorate General of Foreign Trade
  - Counterparties engaged in international trade MUST have valid IEC
  - IEC search shows authorized ports and HS codes

### Director Intelligence (CRITICAL)
- **DIN cross-referencing**: Look up each director's DIN on MCA21
  - Find ALL companies where that DIN appears as director
  - This reveals hidden group structures and shell company networks
  - Flag: director serving on 10+ boards = potential shell network
  - Flag: same registered address across multiple companies
- **Disqualified directors list**: check MCA disqualification orders
  - Section 164(2): non-filing of returns
  - Section 167: vacation of office

### Courts and Legal
- **eCourts**: ecourts.gov.in -- district and high court case search
  - Search by party name (company + directors)
  - Pendency data shows unresolved litigation
- **Indian Kanoon**: indiankanoon.org -- judgment search engine
  - Full text search across Supreme Court, High Courts, tribunals
  - Good for finding enforcement actions and regulatory orders
- **NCLT**: nclt.gov.in -- National Company Law Tribunal
  - Insolvency/bankruptcy proceedings (IBC cases)
  - Oppression and mismanagement petitions
- **SAT**: sat.gov.in -- Securities Appellate Tribunal

### Regulatory
- **SEBI**: sebi.gov.in -- Securities and Exchange Board (for listed entities)
  - Enforcement orders and debarment
  - Insider trading investigations
- **RBI**: rbi.org.in -- Reserve Bank (for banking/NBFC entities)
  - Caution list (entities flagged for forex violations)
  - NBFC registration check
- **ED (Enforcement Directorate)**: enforcementdirectorate.gov.in
  - PMLA (Prevention of Money Laundering Act) cases
  - FEMA (Foreign Exchange Management Act) violations

### Employment and Business Verification
- **EPFO**: epfindia.gov.in -- Employee Provident Fund Organization
  - Employee count as proxy for actual business operations
  - Very low count + large trade volumes = shell indicator

### India-Specific Research Patterns
India is a high-volume counterparty region. Key patterns to check:
- **Shell company indicators**: paid-up capital < 1 lakh, zero employees,
  common DIN network, dormant status at ROC
- **Related party transactions**: check annual filings for transactions
  with directors' other companies
- **IEC validity**: expired IEC = cannot legally trade, major red flag
- **Chemical handling**: check for valid drug precursor licenses (NDPS Act)
  if trading controlled chemicals

### Indian Name Handling
- Store names in both English and Devanagari when available
- For court records in Hindi, use Sarvam AI model for translation
- Common suffixes: Pvt Ltd, LLP, OPC, Partnership (not limited company types)
- PAN-based matching: 4th character indicates entity type (C=company, P=person, F=firm)
