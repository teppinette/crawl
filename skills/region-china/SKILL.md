---
name: region_china
description: China/APAC OSINT registries. DeepSeek primary model for Chinese sources. Covers Qichacha, Tianyancha, NECIPS, Hong Kong ICRIS, Vietnam. Traces VIE structures, SOE connections, UFLPA risk.
metadata:
  openclaw:
    requires:
      env: ["DEEPSEEK_API_KEY"]
---

# China / APAC Region -- Registry Guide

This skill supplements `counterparty_research` with China/APAC-specific sources.

## Model Routing
- **Default**: deepseek-chat (understands Chinese corporate structures natively)
- **Complex Chinese legal analysis**: moonshot-v1-128k (Kimi -- 128k context for long filings)
- **English-language APAC sources**: claude-sonnet-4-6

## CRITICAL: Chinese Entity Name Output
ALL Chinese entities must be recorded in THREE name forms:
1. **Simplified Chinese characters** (e.g., 上海化工有限公司)
2. **Pinyin romanization** (e.g., Shanghai Huagong Youxian Gongsi)
3. **English trade name** if available (e.g., Shanghai Chemical Co., Ltd.)

Store all three in the JSON output `name_local_script`, `name` (pinyin), and add
English trade name to the notes.

## China

### Corporate Registries
- **Qichacha**: qcc.com -- most comprehensive commercial database
- **Tianyancha**: tianyancha.com -- alternative commercial database
- **NECIPS/GSXT**: gsxt.gov.cn -- National Enterprise Credit Information Publicity System (official)
- **SAIC/AIC**: provincial Administration for Industry and Commerce registries
- **SAMR**: samr.gov.cn -- State Administration for Market Regulation

### Key Data Points to Extract
- Unified Social Credit Code (USCC) -- 18-digit unique identifier
- Legal representative (法定代表人) -- NOT the same as beneficial owner
- Registered capital vs paid-in capital (large gap = shell indicator)
- Business scope (经营范围) -- check for chemical/hazmat permits
- Annual reports filed vs missing
- Branch offices and subsidiaries

### Ownership Analysis (CRITICAL)
- **VIE structures**: Variable Interest Entity -- common for Chinese companies,
  creates complex indirect control chains. Trace through all contractual arrangements.
- **Cross-holdings**: Chinese groups often have circular shareholdings
- **State-owned enterprises (SOE)**: identify SASAC connection at any level
  - Central SOEs: sasac.gov.cn
  - Provincial/municipal SOEs: vary by location
- **CCP/PLA connections**: check for military-civil fusion entities
- **UFLPA risk**: flag any entity in Xinjiang Uyghur Autonomous Region or
  connected to entities on the UFLPA Entity List

### Sanctions Screening (China-specific)
- **OFAC SDN**: check Chinese military-related designations
- **BIS Entity List**: export control restrictions (critical for chemicals)
- **Military End-User List**: commerce.gov MEU list
- **UFLPA Entity List**: dhs.gov/uflpa-entity-list
- **Chinese Military Companies (CMC) list**: Treasury/OFAC designation
- **NS-CMIC List**: Non-SDN Chinese Military-Industrial Complex

### Courts
- **China Judgments Online**: wenshu.court.gov.cn -- Supreme Court judgment database
- **China Execution Info**: zxgk.court.gov.cn -- persons subject to enforcement
- **Credit China**: creditchina.gov.cn -- dishonesty blacklist

## Hong Kong

### Corporate Registries
- **ICRIS**: icris.cr.gov.hk -- Integrated Companies Registry Information System
- **GovHK**: cr.gov.hk -- Companies Registry

### Special Attention
- HK entities often serve as holding companies for mainland operations
- Check for connections to sanctioned Chinese entities via HK shell companies

## Vietnam

### Corporate Registries
- **National Business Registration Portal**: dangkykinhdoanh.gov.vn
- **Ministry of Planning and Investment**: mpi.gov.vn

### Special Attention
- Growing China+1 manufacturing hub -- check for PRC-controlled entities
- Chemical import/export licenses

## Myanmar

### Corporate Registries
- **DICA**: dica.gov.mm -- Directorate of Investment and Company Administration
- Limited online access since 2021

### Special Attention (CRITICAL)
- Military junta sanctions -- check OFAC Myanmar-related designations
- State Administrative Council (SAC) connected entities
- Jade and gem trade (frequently sanctioned)
- Chemical precursor trade routes via Myanmar

## General APAC

### Regional Sanctions Aggregators
- **OpenSanctions**: opensanctions.org -- cross-references all lists
- **ICIJ Offshore Leaks**: offshoreleaks.icij.org -- Panama/Pandora papers
- **World Bank debarment list**: worldbank.org/en/projects-operations/procurement/debarred-firms
