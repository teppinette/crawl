---
name: region_americas
description: Americas-specific OSINT registries and sources for US, Canada, and Colombia counterparty research.
---

# Americas Region -- Registry Guide

This skill supplements `counterparty_research` with Americas-specific sources.

## United States

### Corporate Registries
- **Delaware Division of Corporations**: icis.corp.delaware.gov -- most US entities incorporate here
- **New York DOS**: appext20.dos.ny.gov/corp_public
- **Texas SOS**: mycpa.cpa.state.tx.us/coa
- **Florida Sunbiz**: search.sunbiz.org
- **OpenCorporates**: opencorporates.com (aggregator)

### Sanctions and Enforcement
- **OFAC SDN Search**: sanctionssearch.ofac.treas.gov -- PRIMARY source
- **OFAC Sectoral (SSI)**: same search tool, check SSI program
- **BIS Entity List**: efoia.bis.gov/index.php/electronic-foia
- **SEC EDGAR**: sec.gov/cgi-bin/browse-edgar -- public company filings, insider trades
- **FinCEN**: check for enforcement actions

### Courts
- **PACER**: pacer.uscourts.gov -- federal court records
- **State courts**: varies by state, check individual court websites

### Trade
- **CBP CTPAT**: check trusted trader status
- **Census trade data**: for import/export patterns

## Canada

### Corporate Registries
- **CBCA Federal**: ised-isde.canada.ca/cc/lgcy/fdrlCrpSrch.html
- **Ontario**: ontario.ca/page/business-name-search
- **Quebec**: registreentreprises.gouv.qc.ca
- **British Columbia**: bcregistry.gov.bc.ca

### Sanctions
- **Canada SEMA**: international.gc.ca/world-monde/international_relations -- Canadian sanctions list
- **FINTRAC**: fintrac-canafe.gc.ca -- financial intelligence

## Colombia

### Corporate Registries
- **RUES**: rfrfrfruf.rfrues.org.co -- unified commercial registry
- **Confecamaras**: confecamaras.org.co -- chamber of commerce federation
- **Superintendencia de Sociedades**: supersociedades.gov.co

### Sanctions and Enforcement
- **OFAC Colombia Narcotics**: check Narcotics Trafficking program
- **Procuraduria**: procuraduria.gov.co -- disciplinary records
- **Contraloria**: contraloria.gov.co -- fiscal responsibility

## Model Configuration
- Primary: claude-sonnet-4-6
- All searches in English
- For Colombia: search in both English and Spanish
