"""
Fixed version — proper navigation, handle cookie overlays, hit key sites.
"""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dpw-v2")

def get_secret(name):
    try:
        r = subprocess.run(["az","keyvault","secret","show","--vault-name","crawlkeyvault",
            "--name",name,"--query","value","-o","tsv"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip()
    except: return os.environ.get(name.upper().replace("-","_"), "")

MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
MLX_PASSWORD = get_secret("multilogin-password")
MLX_FOLDER_ID = get_secret("multilogin-folder-id")
CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")
OUTPUT_DIR = Path("/home/copapadmin/crawl/output/investigations/super-save-general-trading")
CONTAINERS = ["DFSU6580527","FSCU8108794","MEDU4958469"]
BL_NUMBER = "MEDUFX870746"

def mlx_auth():
    resp = requests.post("https://api.multilogin.com/user/signin",
        json={"email":MLX_EMAIL,"password":hashlib.md5(MLX_PASSWORD.encode()).hexdigest()},
        headers={"Accept":"application/json","Content-Type":"application/json"}, timeout=15)
    return resp.json()["data"]["token"]

def mlx_create(token, name):
    resp = requests.post("https://api.multilogin.com/profile/create",
        json={"name":name,"browser_type":"mimic","folder_id":MLX_FOLDER_ID,
              "parameters":{"fingerprint":{}}},
        headers={"Accept":"application/json","Content-Type":"application/json",
                 "Authorization":f"Bearer {token}"}, timeout=30)
    return resp.json()["data"]["ids"][0]

def mlx_launch(token, pid):
    resp = requests.get(f"https://launcher.mlx.yt:45001/api/v2/profile/f/{MLX_FOLDER_ID}/p/{pid}/start?automation_type=playwright&headless_mode=true",
        headers={"Accept":"application/json","Authorization":f"Bearer {token}"}, verify=False, timeout=90)
    return int(resp.json()["data"]["port"])

def mlx_stop(pid):
    try: subprocess.run([str(CLI_PATH),"profile-stop","--profile-id",pid], capture_output=True, timeout=15)
    except: pass

def mlx_delete(token, pid):
    try: requests.delete("https://api.multilogin.com/profile/delete",
        json={"ids":[pid],"permanently":True},
        headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}, timeout=15)
    except: pass

def main():
    log.info("=== Deep Site Search v2 ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "dpw-v2")
    results = {}
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(45000)
            
            sites = [
                ("importyeti", "https://www.importyeti.com/company/super-save-general-trading"),
                ("importyeti_search", "https://www.importyeti.com/search?q=super+save+general+trading"),
                ("volza", "https://www.volza.com/p/super-save-general-trading-llc/import/import-in-united-arab-emirates/"),
                ("dcciinfo", "https://dcciinfo.com/co/super-save-general-trading-llc-dubai/251100"),
                ("buy2send", "https://www.buy2send.com"),
                ("colombomail", "https://colombomail.lk"),
                ("wttl", "https://supersavetrading.com/wttl.html"),
                ("about", "https://supersavetrading.com/about.html"),
                ("paper", "https://supersavetrading.com/paper.html"),
                ("dubaicolist", "https://dubaicompanieslist.com/business/super-save-general-trading-llc/"),
                ("exportersdubai", "https://exportersdubai.com/super-save-general-trading-llc"),
                ("dnb", "https://www.dnb.com/business-directory/company-profiles.super_save_general_trading_llc.html"),
                ("opencorporates", "https://opencorporates.com/companies?q=super+save+general+trading&jurisdiction_code=ae"),
                ("dubaitrade", "https://www.dubaitrade.ae/"),
                ("dpworld_smart", "https://www.dpworld.com/en/smart-services/track-trace"),
                ("gmaps", "https://www.google.com/maps/search/super+save+general+trading+dubai"),
            ]
            
            for name, url in sites:
                log.info(f"\n[{name}] {url[:60]}...")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    time.sleep(8)
                    
                    # Dismiss cookie consents
                    for sel in ['button:has-text("Accept")', 'button:has-text("Accept All")',
                                'button:has-text("I agree")', '[id*="cookie"] button',
                                'button:has-text("Got it")', 'button:has-text("OK")']:
                        try:
                            btn = page.locator(sel)
                            if btn.count() > 0:
                                btn.first.click(timeout=2000)
                                time.sleep(0.5)
                                break
                        except: pass
                    
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / f"v2_{name}.png"), full_page=True)
                    
                    if len(text) > 100:
                        results[name] = text[:10000]
                        log.info(f"  {len(text)} chars")
                        
                        # Print relevant lines
                        for kw in ["clearing","freight","forwarder","customs broker","haulier",
                                   "transport","logistics","truck","warehouse","address",
                                   "jebel ali","jafza","gate","delivery","suzano","paper",
                                   "phone","+971","super save","notify","agent","office",
                                   "sri lanka","colombo","import","export","shipment"]:
                            for line in text.split('\n'):
                                if kw.lower() in line.lower() and len(line.strip()) > 10:
                                    log.info(f"  >>> {line.strip()[:200]}")
                                    break
                    else:
                        log.info(f"  Only {len(text)} chars")
                        
                except Exception as e:
                    log.warning(f"  ERROR: {e}")
                    results[name] = f"ERROR: {e}"
            
            # Also try Cargoes.com with JS to dismiss overlay
            log.info("\n[cargoes] Container tracking with overlay fix")
            try:
                page.goto("https://www.cargoes.com/container-tracking", wait_until="domcontentloaded", timeout=45000)
                time.sleep(6)
                
                # Remove the cookie overlay via JS
                page.evaluate("""() => {
                    // Remove usercentrics overlay
                    const uc = document.getElementById('usercentrics-root');
                    if (uc) uc.remove();
                    // Remove any other overlays
                    document.querySelectorAll('[class*="backdrop"], [class*="overlay"], [class*="modal"]').forEach(e => {
                        if (e.style.position === 'fixed' || getComputedStyle(e).position === 'fixed') e.remove();
                    });
                }""")
                time.sleep(1)
                
                # Switch to Container Number mode
                try:
                    dd = page.locator('text="Booking ID"').first
                    dd.click(force=True)
                    time.sleep(2)
                    cn = page.locator('text="Container Number"')
                    if cn.count() > 0:
                        cn.first.click(force=True)
                        time.sleep(1)
                        log.info("  Switched to Container Number mode")
                except Exception as e:
                    log.warning(f"  Mode switch: {e}")
                
                # Remove overlay again
                page.evaluate("""() => {
                    const uc = document.getElementById('usercentrics-root');
                    if (uc) uc.remove();
                    document.querySelectorAll('[class*="Backdrop"], [class*="overlay"]').forEach(e => e.remove());
                }""")
                time.sleep(1)
                
                # Fill container number using force
                inp = page.locator('input[type="text"]:visible').first
                inp.click(force=True)
                time.sleep(0.5)
                inp.fill(CONTAINERS[0])
                time.sleep(1)
                
                try:
                    page.locator('button:has-text("Track")').first.click(force=True)
                except:
                    inp.press("Enter")
                
                time.sleep(10)
                text = page.evaluate("() => document.body.innerText") or ""
                page.screenshot(path=str(OUTPUT_DIR / "v2_cargoes_result.png"), full_page=True)
                results["cargoes_result"] = text[:5000]
                log.info(f"  Cargoes result: {len(text)} chars")
                log.info(f"  Preview: {text[:500]}")
                
            except Exception as e:
                log.warning(f"  Cargoes: {e}")
            
            page.close()
        
        with open(OUTPUT_DIR / "dpworld_v2_results.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        
        print("\n" + "="*80)
        print("KEY FINDINGS")
        print("="*80)
        for key, text in results.items():
            if isinstance(text, str) and len(text) > 200 and not text.startswith("ERROR"):
                kws = [kw for kw in ["clearing","freight","customs broker","haulier","transport",
                    "warehouse","jebel ali","jafza","gate","delivery","suzano","paper",
                    "notify","agent","office address","phone","+971","sri lanka"]
                    if kw.lower() in text.lower()]
                if kws:
                    print(f"\n--- {key} ({len(text)} chars) ---")
                    print(f"  Keywords: {', '.join(kws)}")
                    seen = set()
                    for line in text.split('\n'):
                        if any(kw.lower() in line.lower() for kw in kws):
                            cleaned = line.strip()
                            if len(cleaned) > 10 and cleaned not in seen:
                                seen.add(cleaned)
                                print(f"  {cleaned[:250]}")
        print(f"\n{'='*80}")
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
