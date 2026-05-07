"""
Aggressive search for Super Save General Trading clearing agent / freight forwarder.
Try: Google searches, UAE directories, freight forwarder DBs, import data platforms.
"""

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("clearing")

import os

def get_secret(name: str) -> str:
    try:
        result = subprocess.run(
            ["az", "keyvault", "secret", "show", "--vault-name", "crawlkeyvault",
             "--name", name, "--query", "value", "-o", "tsv"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip()
    except Exception:
        return os.environ.get(name.upper().replace("-", "_"), "")


MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
MLX_PASSWORD = get_secret("multilogin-password")
MLX_FOLDER_ID = get_secret("multilogin-folder-id")
CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

OUTPUT_DIR = Path("/home/copapadmin/crawl/output/investigations/super-save-general-trading")
BL_NUMBER = "MEDUFX870746"
CONTAINERS = [
    "DFSU6580527", "FSCU8108794", "MEDU4958469", "MEDU7419218",
    "MSDU6153938", "MSDU7384361", "MSMU7788246", "MSNU5478137",
    "MSNU5529424", "MSNU6792965", "MSNU7760767", "MSNU9153090",
    "MSNU9166158", "SEKU6825445", "SEKU6842139", "TCNU7279232",
    "TCNU8790592", "TGBU5852746", "TIIU4035545", "TXGU8624573",
]


def get_token() -> str:
    resp = requests.post(
        "https://api.multilogin.com/user/signin",
        json={"email": MLX_EMAIL, "password": hashlib.md5(MLX_PASSWORD.encode()).hexdigest()},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["token"]


def create_profile_no_proxy(token: str, name: str) -> str:
    resp = requests.post(
        "https://api.multilogin.com/profile/create",
        json={"name": name, "browser_type": "mimic", "folder_id": MLX_FOLDER_ID,
              "parameters": {"fingerprint": {}}},
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data = resp.json()
    return data["data"]["ids"][0]


def launch_profile(token: str, profile_id: str) -> int:
    url = (f"https://launcher.mlx.yt:45001/api/v2/profile"
           f"/f/{MLX_FOLDER_ID}/p/{profile_id}"
           f"/start?automation_type=playwright&headless_mode=true")
    resp = requests.get(url, headers={"Accept": "application/json",
                                       "Authorization": f"Bearer {token}"},
                        verify=False, timeout=90)
    data = resp.json()
    return int(data["data"]["port"])


def stop_profile(profile_id: str):
    try:
        subprocess.run([str(CLI_PATH), "profile-stop", "--profile-id", profile_id],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def delete_profile(token: str, profile_id: str):
    try:
        requests.delete(f"https://api.multilogin.com/profile/delete",
                        json={"ids": [profile_id], "permanently": True},
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        timeout=15)
    except Exception:
        pass


def safe_nav(page, url, wait=8):
    try:
        page.goto("about:blank", timeout=5000)
        time.sleep(0.5)
    except:
        pass
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(wait)
    return page.evaluate("() => document.body ? document.body.innerText : ''") or ""


def main():
    log.info("=== Clearing Agent / Freight Forwarder Search ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    token = get_token()
    profile_id = create_profile_no_proxy(token, "clearing-agent-search")
    log.info(f"Profile: {profile_id}")
    
    results = {}
    
    try:
        port = launch_profile(token, profile_id)
        log.info(f"CDP port: {port}")
        
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(60000)
            
            # ---------------------------------------------------------------
            # 1. Google: "Super Save General Trading" clearing agent dubai
            # ---------------------------------------------------------------
            log.info("\n[1] Google — clearing agent search")
            queries = [
                '"Super Save General Trading" "clearing agent" dubai',
                '"Super Save General Trading" "freight forwarder" dubai',
                '"Super Save General Trading" customs broker jebel ali',
                '"Super Save General Trading LLC" logistics dubai',
                '"MEDUFX870746" haulier OR transport OR truck OR delivery',
                '"Super Save General Trading" suzano import dubai',
                '"supersavetrading.com" freight OR logistics OR transport',
            ]
            
            for i, query in enumerate(queries):
                log.info(f"  Google [{i+1}]: {query[:60]}...")
                try:
                    text = safe_nav(page, f"https://www.google.com/search?q={requests.utils.quote(query)}&num=20", wait=5)
                    page.screenshot(path=str(OUTPUT_DIR / f"clearing_google_{i+1}.png"), full_page=True)
                    results[f"google_{i+1}"] = {"query": query, "text": text[:6000]}
                    
                    # Extract any URLs from results
                    urls = page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                            href: a.href, text: a.innerText.substring(0, 200)
                        })).filter(u => !u.href.includes('google.com') && u.text.length > 5).slice(0, 20);
                    }""")
                    results[f"google_{i+1}"]["urls"] = urls
                    
                    found_relevant = any(kw in text.lower() for kw in ["clearing", "freight", "transport", "haulier", "logistics", "customs broker"])
                    log.info(f"    {len(text)} chars, relevant={found_relevant}")
                    
                    if found_relevant:
                        for line in text.split('\n'):
                            if any(kw in line.lower() for kw in ["clearing", "freight", "transport", "haulier", "logistics"]):
                                log.info(f"    >>> {line.strip()[:150]}")
                    
                except Exception as e:
                    results[f"google_{i+1}"] = {"query": query, "error": str(e)}
                    log.warning(f"    ERROR: {e}")
            
            # ---------------------------------------------------------------
            # 2. supersavetrading.com — check about page, contact, partners
            # ---------------------------------------------------------------
            log.info("\n[2] supersavetrading.com — full site crawl")
            site_pages = [
                ("home", "https://supersavetrading.com/"),
                ("about", "https://supersavetrading.com/about"),
                ("about-us", "https://supersavetrading.com/about-us"),
                ("contact", "https://supersavetrading.com/contact"),
                ("contact-us", "https://supersavetrading.com/contact-us"),
                ("services", "https://supersavetrading.com/services"),
                ("partners", "https://supersavetrading.com/partners"),
            ]
            
            for name, url in site_pages:
                try:
                    text = safe_nav(page, url, wait=5)
                    if len(text) > 100:
                        results[f"ssgt_{name}"] = text[:5000]
                        page.screenshot(path=str(OUTPUT_DIR / f"clearing_ssgt_{name}.png"), full_page=True)
                        log.info(f"  {name}: {len(text)} chars")
                        
                        # Look for ANY company names, addresses, phone numbers
                        for line in text.split('\n'):
                            line = line.strip()
                            if len(line) > 10 and any(kw in line.lower() for kw in 
                                ["address", "location", "warehouse", "office", "phone", "+971",
                                 "transport", "logistics", "freight", "clearing", "partner",
                                 "jebel ali", "jafza", "al quoz", "deira", "bur dubai"]):
                                log.info(f"    >>> {line[:200]}")
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # ---------------------------------------------------------------
            # 3. Dubai DED / JAFZA — company details
            # ---------------------------------------------------------------
            log.info("\n[3] UAE business directories")
            dir_urls = [
                ("yellowpages_ae", "https://www.yellowpages.ae/search/super+save+general+trading"),
                ("yp_uae", "https://www.yp.ae/search/super+save+general+trading"),
                ("dnb_uae", "https://www.dnb.com/business-directory/company-profiles.super_save_general_trading_llc.html"),
                ("zauba_uae", "https://www.zaubacorp.com/company/SUPER-SAVE-GENERAL-TRADING"),
                ("opencorporates", "https://opencorporates.com/companies?q=super+save+general+trading&jurisdiction_code=ae"),
                ("dun_dubai", "https://www.dubaibusinessguide.com/search?q=super+save+general+trading"),
                ("emirates_directory", "https://www.emiratesdirectory.com/search?q=super+save+general+trading"),
                ("uae_companies", "https://www.uaecompanies.com/search?q=super+save+general+trading"),
            ]
            
            for name, url in dir_urls:
                try:
                    text = safe_nav(page, url, wait=6)
                    if len(text) > 100 and "Access Denied" not in text:
                        results[f"dir_{name}"] = text[:5000]
                        page.screenshot(path=str(OUTPUT_DIR / f"clearing_dir_{name}.png"), full_page=True)
                        log.info(f"  {name}: {len(text)} chars")
                    else:
                        log.info(f"  {name}: blocked or empty")
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # ---------------------------------------------------------------
            # 4. Import/Export data platforms — Zauba, ImportYeti, etc.
            # ---------------------------------------------------------------
            log.info("\n[4] Import/Export data platforms")
            trade_urls = [
                ("importyeti", "https://www.importyeti.com/company/super-save-general-trading"),
                ("importyeti_bl", f"https://www.importyeti.com/search?q={BL_NUMBER}"),
                ("export_genius_bl", f"https://www.exportgenius.in/search?q={BL_NUMBER}"),
                ("export_genius_co", "https://www.exportgenius.in/search?q=super+save+general+trading"),
                ("volza_bl", f"https://www.volza.com/p/bl-{BL_NUMBER.lower()}/import/import-in-uae/"),
                ("cybex_bl", f"https://www.cybex.in/search?q={BL_NUMBER}"),
                ("tradeint", "https://www.tradeintel.com/search?q=super+save+general+trading+dubai"),
                ("enigma_uae", f"https://connect.data.com/company/search?q=super+save+general+trading"),
            ]
            
            for name, url in trade_urls:
                try:
                    text = safe_nav(page, url, wait=8)
                    if len(text) > 100:
                        results[f"trade_{name}"] = text[:8000]
                        page.screenshot(path=str(OUTPUT_DIR / f"clearing_trade_{name}.png"), full_page=True)
                        log.info(f"  {name}: {len(text)} chars")
                        
                        for kw in ["clearing", "freight", "forwarder", "agent", "customs",
                                   "notify", "consignee", "shipper", "haulier", "transport",
                                   "suzano", "super save", "address", "warehouse"]:
                            for line in text.split('\n'):
                                if kw.lower() in line.lower() and len(line.strip()) > 10:
                                    log.info(f"    [{name}] >>> {line.strip()[:200]}")
                                    break
                    else:
                        log.info(f"  {name}: empty/blocked")
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # ---------------------------------------------------------------
            # 5. LinkedIn company search
            # ---------------------------------------------------------------
            log.info("\n[5] LinkedIn company search")
            try:
                text = safe_nav(page, "https://www.linkedin.com/company/super-save-general-trading/", wait=8)
                results["linkedin_co"] = text[:5000]
                page.screenshot(path=str(OUTPUT_DIR / "clearing_linkedin.png"), full_page=True)
                log.info(f"  LinkedIn company: {len(text)} chars")
            except Exception as e:
                log.info(f"  LinkedIn: {e}")
            
            # Try LinkedIn search for employees
            try:
                text = safe_nav(page, 'https://www.linkedin.com/search/results/people/?keywords="super save general trading"', wait=8)
                results["linkedin_people"] = text[:5000]
                page.screenshot(path=str(OUTPUT_DIR / "clearing_linkedin_people.png"), full_page=True)
                log.info(f"  LinkedIn people: {len(text)} chars")
            except Exception as e:
                log.info(f"  LinkedIn people: {e}")
            
            # ---------------------------------------------------------------
            # 6. MSC eBooking / myMSC — delivery order info
            # ---------------------------------------------------------------
            log.info("\n[6] MSC eBooking/myMSC")
            try:
                text = safe_nav(page, "https://www.msc.com/en/track-a-shipment", wait=8)
                
                # Try to access the delivery order section
                # After loading tracking, look for "Delivery Order" or "DO" links
                page.fill('#trackingNumber', BL_NUMBER)
                page.press('#trackingNumber', 'Enter')
                time.sleep(12)
                
                # Look for any delivery order or documents section
                page_text = page.evaluate("() => document.body.innerText") or ""
                results["msc_tracking_full"] = page_text[:10000]
                
                # Check for document links
                doc_links = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a')).map(a => ({
                        href: a.href, text: a.innerText.trim()
                    })).filter(a => a.text.length > 0 && 
                        (a.text.toLowerCase().includes('delivery') || 
                         a.text.toLowerCase().includes('document') ||
                         a.text.toLowerCase().includes('order') ||
                         a.text.toLowerCase().includes('release') ||
                         a.text.toLowerCase().includes('invoice') ||
                         a.text.toLowerCase().includes('detail')))
                    ;
                }""")
                results["msc_doc_links"] = doc_links
                log.info(f"  MSC doc links: {json.dumps(doc_links)}")
                
                page.screenshot(path=str(OUTPUT_DIR / "clearing_msc_full.png"), full_page=True)
                
            except Exception as e:
                log.info(f"  MSC: {e}")
            
            # ---------------------------------------------------------------
            # 7. MarineTraffic — vessel/port details
            # ---------------------------------------------------------------
            log.info("\n[7] MarineTraffic — port calls")
            try:
                # MSC ANAHITA at Jebel Ali
                text = safe_nav(page, "https://www.marinetraffic.com/en/ais/details/ships/shipid:362073/mmsi:636091703/imo:9302085/vessel:MSC_ANAHITA", wait=10)
                results["marinetraffic_anahita"] = text[:5000]
                page.screenshot(path=str(OUTPUT_DIR / "clearing_marinetraffic.png"), full_page=True)
                log.info(f"  MarineTraffic: {len(text)} chars")
            except Exception as e:
                log.info(f"  MarineTraffic: {e}")
            
            # ---------------------------------------------------------------
            # 8. Google: "MEDUFX870746" OR container numbers + any results
            # ---------------------------------------------------------------
            log.info("\n[8] Google — BL/container deep search")
            deep_queries = [
                f'"{BL_NUMBER}"',
                f'"{CONTAINERS[0]}" jebel ali delivery',
                f'"super save general trading" "jebel ali" warehouse address',
                f'"super save general trading" JAFZA OR "al quoz" OR warehouse',
                f'"super save general trading" importer dubai customs',
                f'"super save general trading" LLC dubai registered address',
            ]
            
            for i, query in enumerate(deep_queries):
                try:
                    text = safe_nav(page, f"https://www.google.com/search?q={requests.utils.quote(query)}&num=20", wait=5)
                    page.screenshot(path=str(OUTPUT_DIR / f"clearing_deep_{i+1}.png"), full_page=True)
                    results[f"deep_{i+1}"] = {"query": query, "text": text[:5000]}
                    
                    # Extract search result URLs
                    urls = page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                            href: a.href, text: a.innerText.substring(0, 200)
                        })).filter(u => !u.href.includes('google.com') && u.text.length > 5).slice(0, 15);
                    }""")
                    results[f"deep_{i+1}"]["urls"] = urls
                    log.info(f"  Deep [{i+1}]: {len(text)} chars, {len(urls)} URLs")
                    
                except Exception as e:
                    log.info(f"  Deep [{i+1}]: {e}")
            
            page.close()
        
        # Save all
        with open(OUTPUT_DIR / "clearing_agent_search.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        
        # Print summary
        print("\n" + "=" * 80)
        print("CLEARING AGENT / FREIGHT FORWARDER SEARCH RESULTS")
        print("=" * 80)
        
        keywords = ["clearing", "freight", "forwarder", "customs broker", "haulier",
                     "transport", "logistics", "truck", "warehouse", "address",
                     "super save", "jebel ali", "jafza", "notify party",
                     "+971", "dubai", "al quoz", "deira"]
        
        for key, val in results.items():
            text = ""
            if isinstance(val, dict):
                text = val.get("text", "")
            elif isinstance(val, str):
                text = val
            
            if text:
                found = [kw for kw in keywords if kw.lower() in text.lower()]
                if found and key.startswith(("google_", "trade_", "deep_", "dir_", "ssgt_")):
                    print(f"\n--- {key} (keywords: {', '.join(found[:5])}) ---")
                    for line in text.split('\n'):
                        if any(kw.lower() in line.lower() for kw in found):
                            cleaned = line.strip()
                            if len(cleaned) > 10:
                                print(f"  {cleaned[:250]}")
        
        # Print any URLs found
        print(f"\n{'='*80}")
        print("EXTRACTED URLs")
        print("=" * 80)
        for key, val in results.items():
            if isinstance(val, dict) and "urls" in val:
                for url_info in val["urls"]:
                    href = url_info.get("href", "")
                    text = url_info.get("text", "")[:100]
                    if any(kw in text.lower() for kw in ["super save", "freight", "clearing", "transport", "logistics", "haulier"]):
                        print(f"  [{key}] {text}")
                        print(f"         {href}")
        
        print(f"\n{'='*80}")
        
    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Done")


if __name__ == "__main__":
    main()
