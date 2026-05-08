"""
Deep DP World / Dubai Trade search for gate-out details.
Try every possible DP World endpoint and portal.
Also try: ExportGenius, ImportYeti with proper navigation (not URL-only).
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
log = logging.getLogger("dpworld-deep")

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
MLX_PROXY_USER = get_secret("multilogin-proxy-user")
MLX_PROXY_PASS = get_secret("multilogin-proxy-pass")
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
    data = resp.json()
    return data["data"]["token"]

def create_profile_no_proxy(token, name):
    params = {"fingerprint": {}}
    if MLX_PROXY_USER and MLX_PROXY_PASS:
        params["proxy"] = {"type": "http", "host": "gate.multilogin.com", "port": 8080,
                           "username": MLX_PROXY_USER, "password": MLX_PROXY_PASS}
    resp = requests.post(
        "https://api.multilogin.com/profile/create",
        json={"name": name, "browser_type": "mimic", "folder_id": MLX_FOLDER_ID,
              "parameters": params},
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return resp.json()["data"]["ids"][0]

def launch_profile(token, profile_id):
    url = (f"https://launcher.mlx.yt:45001/api/v2/profile"
           f"/f/{MLX_FOLDER_ID}/p/{profile_id}"
           f"/start?automation_type=playwright&headless_mode=true")
    resp = requests.get(url, headers={"Accept": "application/json",
                                       "Authorization": f"Bearer {token}"},
                        verify=False, timeout=90)
    return int(resp.json()["data"]["port"])

def stop_profile(profile_id):
    try:
        subprocess.run([str(CLI_PATH), "profile-stop", "--profile-id", profile_id],
                       capture_output=True, timeout=15)
    except: pass

def delete_profile(token, profile_id):
    try:
        requests.delete("https://api.multilogin.com/profile/delete",
                        json={"ids": [profile_id], "permanently": True},
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"}, timeout=15)
    except: pass

def safe_nav(page, url, wait=8):
    try:
        page.goto("about:blank", timeout=5000)
        time.sleep(0.5)
    except: pass
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(wait)
    return page.evaluate("() => document.body ? document.body.innerText : ''") or ""


def main():
    log.info("=== DP World / Trade Data Deep Search ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    token = get_token()
    profile_id = create_profile_no_proxy(token, "dpworld-deep")
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
            # 1. Cargoes.com — try the newer container inquiry page
            # ---------------------------------------------------------------
            log.info("\n[1] Cargoes.com — container inquiry")
            try:
                text = safe_nav(page, "https://www.cargoes.com/container-tracking", wait=8)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_cargoes_home.png"), full_page=True)
                
                # Try to find all tracking modes
                modes = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('*')).filter(e => 
                        e.innerText && (e.innerText.includes('Container') || e.innerText.includes('Booking') || e.innerText.includes('Bill'))
                        && e.children.length === 0
                    ).map(e => ({tag: e.tagName, text: e.innerText.trim(), class: e.className})).slice(0, 30);
                }""")
                log.info(f"  Tracking modes: {json.dumps(modes[:10])}")
                results["cargoes_modes"] = modes
                
                # Try container number search
                for cid in CONTAINERS[:3]:
                    log.info(f"  Trying container {cid}...")
                    try:
                        page.goto("about:blank", timeout=5000)
                        time.sleep(0.5)
                        page.goto("https://www.cargoes.com/container-tracking", wait_until="domcontentloaded", timeout=30000)
                        time.sleep(5)
                        
                        # Look for dropdown and switch to Container Number
                        try:
                            dd = page.locator('text="Booking ID"')
                            if dd.count() > 0:
                                dd.first.click()
                                time.sleep(1)
                                cn_opt = page.locator('text="Container Number"')
                                if cn_opt.count() > 0:
                                    cn_opt.first.click()
                                    time.sleep(1)
                        except: pass
                        
                        # Find input and fill
                        inp = page.locator('input[type="text"]:visible').first
                        inp.click()
                        inp.fill(cid)
                        time.sleep(1)
                        
                        # Click track button
                        try:
                            page.locator('button:has-text("Track")').first.click()
                        except:
                            inp.press("Enter")
                        
                        time.sleep(8)
                        result_text = page.evaluate("() => document.body.innerText") or ""
                        page.screenshot(path=str(OUTPUT_DIR / f"dpw_cargoes_{cid}.png"), full_page=True)
                        results[f"cargoes_{cid}"] = result_text[:5000]
                        log.info(f"    Result: {len(result_text)} chars")
                        
                        # Look for gate-out, delivery, truck info
                        for kw in ["gate", "delivery", "truck", "transport", "haulier", "driver"]:
                            for line in result_text.split('\n'):
                                if kw in line.lower():
                                    log.info(f"    >>> {line.strip()[:200]}")
                                    
                    except Exception as e:
                        log.warning(f"    {cid}: {e}")
                        
            except Exception as e:
                log.warning(f"  Cargoes: {e}")
            
            # ---------------------------------------------------------------
            # 2. DP World direct container search (various endpoints)
            # ---------------------------------------------------------------
            log.info("\n[2] DP World container search")
            dpw_urls = [
                ("dpw_flow", "https://flow.dpworld.com/"),
                ("dpw_flow_track", f"https://flow.dpworld.com/tracking?containerNumber={CONTAINERS[0]}"),
                ("dpw_track", f"https://www.dpworld.com/en/track-and-trace?containerNumber={CONTAINERS[0]}"),
                ("dpw_smart", "https://www.dpworld.com/en/smart-services/track-trace"),
            ]
            
            for name, url in dpw_urls:
                try:
                    text = safe_nav(page, url, wait=8)
                    page.screenshot(path=str(OUTPUT_DIR / f"dpw_{name}.png"), full_page=True)
                    results[name] = text[:5000]
                    log.info(f"  {name}: {len(text)} chars")
                    
                    # Look for input fields to fill container number
                    inputs = page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('input')).map(i => ({
                            type: i.type, placeholder: i.placeholder || '', id: i.id,
                            name: i.name, visible: i.offsetParent !== null, value: i.value
                        })).filter(i => i.visible);
                    }""")
                    if inputs:
                        log.info(f"  Inputs: {json.dumps(inputs[:5])}")
                        # Try filling first text input with container number
                        for inp_info in inputs:
                            if inp_info['type'] in ('text', 'search', 'tel'):
                                sel = f"#{inp_info['id']}" if inp_info['id'] else f"input[name='{inp_info['name']}']" if inp_info['name'] else "input[type='text']:visible"
                                try:
                                    field = page.locator(sel).first
                                    field.click()
                                    field.fill(CONTAINERS[0])
                                    time.sleep(1)
                                    field.press("Enter")
                                    time.sleep(10)
                                    result_text = page.evaluate("() => document.body.innerText") or ""
                                    page.screenshot(path=str(OUTPUT_DIR / f"dpw_{name}_result.png"), full_page=True)
                                    results[f"{name}_result"] = result_text[:5000]
                                    log.info(f"  Result: {len(result_text)} chars")
                                except Exception as e2:
                                    log.warning(f"  Search failed: {e2}")
                                break
                                
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # ---------------------------------------------------------------
            # 3. Dubai Trade portal  
            # ---------------------------------------------------------------
            log.info("\n[3] Dubai Trade")
            try:
                text = safe_nav(page, "https://www.dubaitrade.ae/", wait=8)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_dubaitrade.png"), full_page=True)
                results["dubaitrade"] = text[:5000]
                log.info(f"  Dubai Trade: {len(text)} chars")
                
                # Look for container inquiry or tracking
                links = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a')).map(a => ({
                        href: a.href, text: a.innerText.trim()
                    })).filter(a => a.text.length > 0 && (
                        a.text.toLowerCase().includes('container') ||
                        a.text.toLowerCase().includes('tracking') ||
                        a.text.toLowerCase().includes('inquiry') ||
                        a.text.toLowerCase().includes('cargo') ||
                        a.text.toLowerCase().includes('customs')
                    ));
                }""")
                results["dubaitrade_links"] = links
                log.info(f"  Links: {json.dumps(links[:10])}")
                
            except Exception as e:
                log.info(f"  Dubai Trade: {e}")
            
            # ---------------------------------------------------------------
            # 4. ImportYeti — full company page with shipment records
            # ---------------------------------------------------------------
            log.info("\n[4] ImportYeti — company shipments")
            try:
                text = safe_nav(page, "https://www.importyeti.com/company/super-save-general-trading", wait=10)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_importyeti.png"), full_page=True)
                results["importyeti"] = text[:10000]
                log.info(f"  ImportYeti: {len(text)} chars")
                
                # Check for specific shipment details
                for kw in ["suzano", "MEDUFX", "clearing", "freight", "notify", "consignee",
                           "haulier", "transport", "customs broker", "shipper", "forwarder"]:
                    for line in text.split('\n'):
                        if kw.lower() in line.lower():
                            log.info(f"    >>> {line.strip()[:200]}")
                            
            except Exception as e:
                log.info(f"  ImportYeti: {e}")
            
            # Also try ImportYeti search
            try:
                text = safe_nav(page, "https://www.importyeti.com/search?q=super+save+general+trading", wait=10)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_importyeti_search.png"), full_page=True)
                results["importyeti_search"] = text[:5000]
                log.info(f"  ImportYeti search: {len(text)} chars")
            except Exception as e:
                log.info(f"  ImportYeti search: {e}")
            
            # ---------------------------------------------------------------
            # 5. ExportGenius / Volza — actual navigation with interaction
            # ---------------------------------------------------------------
            log.info("\n[5] Volza — detailed")
            try:
                text = safe_nav(page, "https://www.volza.com/p/super-save-general-trading-llc/import/import-in-united-arab-emirates/", wait=10)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_volza_detail.png"), full_page=True)
                results["volza_detail"] = text[:10000]
                log.info(f"  Volza detail: {len(text)} chars")
                
                for kw in ["suzano", "MEDUFX", "clearing", "freight", "notify",
                           "haulier", "transport", "customs", "forwarder", "address"]:
                    for line in text.split('\n'):
                        if kw.lower() in line.lower():
                            log.info(f"    >>> {line.strip()[:200]}")
            except Exception as e:
                log.info(f"  Volza: {e}")
            
            # ---------------------------------------------------------------
            # 6. DcciInfo — DCCI business directory 
            # ---------------------------------------------------------------
            log.info("\n[6] DcciInfo — DCCI directory")
            try:
                text = safe_nav(page, "https://dcciinfo.com/co/super-save-general-trading-llc-dubai/251100", wait=8)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_dcciinfo.png"), full_page=True)
                results["dcciinfo"] = text[:8000]
                log.info(f"  DcciInfo: {len(text)} chars")
                for line in text.split('\n'):
                    line = line.strip()
                    if len(line) > 5:
                        log.info(f"    {line[:200]}")
            except Exception as e:
                log.info(f"  DcciInfo: {e}")
            
            # ---------------------------------------------------------------
            # 7. buy2send.com / colombomail.lk — associated websites
            # ---------------------------------------------------------------
            log.info("\n[7] Associated websites")
            assoc_urls = [
                ("buy2send", "https://www.buy2send.com"),
                ("colombomail", "https://colombomail.lk"),
                ("wttl", "https://supersavetrading.com/wttl.html"),
                ("about", "https://supersavetrading.com/about.html"),
                ("paper", "https://supersavetrading.com/paper.html"),
            ]
            
            for name, url in assoc_urls:
                try:
                    text = safe_nav(page, url, wait=6)
                    if len(text) > 50:
                        page.screenshot(path=str(OUTPUT_DIR / f"dpw_assoc_{name}.png"), full_page=True)
                        results[f"assoc_{name}"] = text[:5000]
                        log.info(f"  {name}: {len(text)} chars")
                        for kw in ["warehouse", "address", "office", "logistics", "transport",
                                   "clearing", "freight", "jebel", "jafza", "+971"]:
                            for line in text.split('\n'):
                                if kw.lower() in line.lower():
                                    log.info(f"    [{name}] >>> {line.strip()[:200]}")
                                    break
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # ---------------------------------------------------------------
            # 8. Google Maps — warehouse location
            # ---------------------------------------------------------------
            log.info("\n[8] Google Maps — location search")
            try:
                text = safe_nav(page, "https://www.google.com/maps/search/super+save+general+trading+dubai", wait=10)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_gmaps.png"), full_page=True)
                results["gmaps"] = text[:5000]
                log.info(f"  Google Maps: {len(text)} chars")
                
                # Extract all visible text for addresses
                all_text = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('*')).filter(e => 
                        e.children.length === 0 && e.innerText && e.innerText.trim().length > 5
                    ).map(e => e.innerText.trim()).filter(t => 
                        t.includes('Super Save') || t.includes('+971') || t.includes('Dubai') ||
                        t.includes('Jebel') || t.includes('JAFZA') || t.includes('warehouse') ||
                        t.includes('logistics') || t.includes('Gate')
                    );
                }""")
                results["gmaps_texts"] = all_text
                for t in all_text:
                    log.info(f"    >>> {t[:200]}")
                    
            except Exception as e:
                log.info(f"  Google Maps: {e}")
            
            # ---------------------------------------------------------------
            # 9. dubaicompanieslist.com — detailed company page
            # ---------------------------------------------------------------
            log.info("\n[9] Dubai Companies List")
            try:
                text = safe_nav(page, "https://dubaicompanieslist.com/business/super-save-general-trading-llc/", wait=8)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_dubaicompanieslist.png"), full_page=True)
                results["dubaicompanieslist"] = text[:8000]
                log.info(f"  DubaiCompaniesList: {len(text)} chars")
                for line in text.split('\n'):
                    if len(line.strip()) > 10:
                        log.info(f"    {line.strip()[:200]}")
            except Exception as e:
                log.info(f"  DubaiCompaniesList: {e}")
            
            # ---------------------------------------------------------------
            # 10. exportersdubai.com — company page from sitemap
            # ---------------------------------------------------------------
            log.info("\n[10] ExportersDubai")
            try:
                text = safe_nav(page, "https://exportersdubai.com/super-save-general-trading-llc", wait=8)
                page.screenshot(path=str(OUTPUT_DIR / "dpw_exportersdubai.png"), full_page=True)
                results["exportersdubai"] = text[:8000]
                log.info(f"  ExportersDubai: {len(text)} chars")
                for line in text.split('\n'):
                    if len(line.strip()) > 10:
                        log.info(f"    {line.strip()[:200]}")
            except Exception as e:
                log.info(f"  ExportersDubai: {e}")
            
            page.close()
        
        # Save
        with open(OUTPUT_DIR / "dpworld_deep_search.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        
        # Print key findings
        print("\n" + "="*80)
        print("DP WORLD / TRADE DATA DEEP SEARCH RESULTS")
        print("="*80)
        
        for key, val in results.items():
            text = ""
            if isinstance(val, str):
                text = val
            elif isinstance(val, dict):
                text = val.get("text", "")
            
            if text and len(text) > 200:
                kws_found = []
                for kw in ["gate out", "truck", "haulier", "transport", "clearing agent",
                           "freight forwarder", "customs broker", "delivery order",
                           "notify party", "warehouse", "jebel ali", "jafza",
                           "suzano", "paper", "art board", "super save"]:
                    if kw.lower() in text.lower():
                        kws_found.append(kw)
                if kws_found:
                    print(f"\n--- {key} ({len(text)} chars, keywords: {', '.join(kws_found)}) ---")
                    for line in text.split('\n'):
                        if any(kw.lower() in line.lower() for kw in kws_found):
                            cleaned = line.strip()
                            if len(cleaned) > 10:
                                print(f"  {cleaned[:250]}")
        
        print(f"\n{'='*80}")
        
    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Done")

if __name__ == "__main__":
    main()
