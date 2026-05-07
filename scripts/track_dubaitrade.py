"""Explore Dubai Trade portal — check login, public pages, APIs, container inquiry."""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dubaitrade")

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
    log.info("=== Dubai Trade Portal Exploration ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "dubaitrade-explore")
    results = {}
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(45000)
            
            # 1. Main portal
            log.info("\n[1] dubaitrade.ae — main page")
            page.goto("https://www.dubaitrade.ae/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            page.screenshot(path=str(OUTPUT_DIR / "dt_main.png"), full_page=True)
            results["main"] = text[:10000]
            log.info(f"  Main: {len(text)} chars")
            
            # Get ALL links on the page
            links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href, text: a.innerText.trim().substring(0, 100)
            })).filter(a => a.text.length > 0)""")
            results["main_links"] = links
            log.info(f"  Links: {len(links)}")
            
            # Print interesting links
            for link in links:
                href = link['href'].lower()
                txt = link['text'].lower()
                if any(kw in href + txt for kw in ['container', 'track', 'inquiry', 'cargo', 'import',
                    'customs', 'delivery', 'gate', 'login', 'signin', 'register', 'signup',
                    'port', 'terminal', 'manifest', 'search', 'services']):
                    log.info(f"  >>> {link['text']}: {link['href']}")
            
            # 2. Try known Dubai Trade subpages
            log.info("\n[2] Dubai Trade — subpages")
            subpages = [
                ("services", "https://www.dubaitrade.ae/services"),
                ("eservices", "https://www.dubaitrade.ae/e-services"),
                ("container_tracking", "https://www.dubaitrade.ae/container-tracking"),
                ("cargo_tracking", "https://www.dubaitrade.ae/cargo-tracking"),
                ("import_services", "https://www.dubaitrade.ae/import"),
                ("customs", "https://www.dubaitrade.ae/customs"),
                ("port_services", "https://www.dubaitrade.ae/port-services"),
                ("login", "https://www.dubaitrade.ae/login"),
                ("signin", "https://www.dubaitrade.ae/signin"),
                ("register", "https://www.dubaitrade.ae/register"),
                ("signup", "https://www.dubaitrade.ae/signup"),
                ("account", "https://www.dubaitrade.ae/account"),
            ]
            
            for name, url in subpages:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(4)
                    sub_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    final_url = page.url
                    if len(sub_text) > 100:
                        page.screenshot(path=str(OUTPUT_DIR / f"dt_{name}.png"), full_page=True)
                        results[name] = {"text": sub_text[:5000], "final_url": final_url}
                        log.info(f"  {name}: {len(sub_text)} chars -> {final_url}")
                    else:
                        log.info(f"  {name}: {len(sub_text)} chars (empty) -> {final_url}")
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # 3. Try the JAFZA portal (part of Dubai Trade ecosystem)
            log.info("\n[3] JAFZA portal")
            try:
                page.goto("https://eservices.jafza.ae/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "dt_jafza_eservices.png"), full_page=True)
                results["jafza_eservices"] = text[:8000]
                log.info(f"  JAFZA eServices: {len(text)} chars")
                
                # Get links
                jafza_links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    href: a.href, text: a.innerText.trim().substring(0, 100)
                })).filter(a => a.text.length > 0)""")
                for link in jafza_links:
                    if any(kw in (link['href'] + link['text']).lower() for kw in 
                        ['container', 'cargo', 'delivery', 'gate', 'login', 'register', 'track', 'inquiry']):
                        log.info(f"  >>> {link['text']}: {link['href']}")
            except Exception as e:
                log.info(f"  JAFZA eServices: {e}")
            
            # 4. Try Dubai Customs
            log.info("\n[4] Dubai Customs")
            try:
                page.goto("https://www.dubaicustoms.gov.ae/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "dt_dubaicustoms.png"), full_page=True)
                results["dubaicustoms"] = text[:8000]
                log.info(f"  Dubai Customs: {len(text)} chars")
                
                # Look for any public inquiry service
                dc_links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    href: a.href, text: a.innerText.trim().substring(0, 100)
                })).filter(a => a.text.length > 0 && (
                    a.text.toLowerCase().includes('container') || a.text.toLowerCase().includes('inquiry') ||
                    a.text.toLowerCase().includes('track') || a.text.toLowerCase().includes('cargo') ||
                    a.text.toLowerCase().includes('manifest') || a.text.toLowerCase().includes('import') ||
                    a.text.toLowerCase().includes('delivery') || a.text.toLowerCase().includes('service')
                ))""")
                for link in dc_links:
                    log.info(f"  >>> {link['text']}: {link['href']}")
                results["dubaicustoms_links"] = dc_links
            except Exception as e:
                log.info(f"  Dubai Customs: {e}")
            
            # 5. Try Dubai Customs e-services / Mirsal
            log.info("\n[5] Mirsal / Dubai Customs e-services")
            mirsal_urls = [
                ("mirsal2", "https://mirsal2.dubaicustoms.gov.ae/"),
                ("mirsal", "https://mirsal.dubaicustoms.gov.ae/"),
                ("eservices_dc", "https://eservices.dubaicustoms.gov.ae/"),
                ("dc_inquiry", "https://www.dubaicustoms.gov.ae/en/eServices/Pages/ContainerInquiry.aspx"),
                ("dc_cargo", "https://www.dubaicustoms.gov.ae/en/eServices/Pages/CargoInquiry.aspx"),
                ("dc_manifest", "https://www.dubaicustoms.gov.ae/en/eServices/Pages/ManifestInquiry.aspx"),
            ]
            
            for name, url in mirsal_urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(5)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    final_url = page.url
                    page.screenshot(path=str(OUTPUT_DIR / f"dt_{name}.png"), full_page=True)
                    results[name] = {"text": text[:5000], "final_url": final_url}
                    log.info(f"  {name}: {len(text)} chars -> {final_url}")
                    
                    # Check for input fields
                    inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input:not([type="hidden"])')).map(i => ({
                        type: i.type, placeholder: i.placeholder || '', id: i.id,
                        name: i.name, visible: i.offsetParent !== null
                    })).filter(i => i.visible)""")
                    if inputs:
                        log.info(f"  Inputs: {json.dumps(inputs[:5])}")
                        results[f"{name}_inputs"] = inputs
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # 6. Try DP World UAE specific pages
            log.info("\n[6] DP World UAE")
            dpw_urls = [
                ("dpw_uae", "https://www.dpworld.com/en/uae"),
                ("dpw_jebel_ali", "https://www.dpworld.com/en/uae/our-locations/jebel-ali"),
                ("dpw_eservices", "https://www.dpworld.ae/en/e-services"),
                ("dpw_container", "https://www.dpworld.ae/en/e-services/container-tracking"),
            ]
            
            for name, url in dpw_urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(5)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    final_url = page.url
                    if len(text) > 100:
                        page.screenshot(path=str(OUTPUT_DIR / f"dt_{name}.png"), full_page=True)
                        results[name] = {"text": text[:5000], "final_url": final_url}
                        log.info(f"  {name}: {len(text)} chars -> {final_url}")
                    else:
                        log.info(f"  {name}: empty -> {final_url}")
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            # 7. Google — "dubai trade portal" container inquiry public
            log.info("\n[7] Google — Dubai Trade public container inquiry")
            goog = [
                'dubai trade portal container inquiry public access',
                'dubaitrade.ae container tracking without login',
                'dubai customs "container inquiry" site:dubaicustoms.gov.ae',
                '"delivery order" "jebel ali" container inquiry portal',
                'DP World jebel ali container gate out inquiry',
            ]
            for i, q in enumerate(goog):
                try:
                    page.goto(f"https://www.google.com/search?q={requests.utils.quote(q)}&num=10", 
                        wait_until="domcontentloaded", timeout=20000)
                    time.sleep(4)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    results[f"goog_{i+1}"] = {"q": q, "text": text[:3000]}
                    useful = [l.strip() for l in text.split('\n') if len(l.strip()) > 30 
                        and not l.strip().startswith(('People also', 'Related', 'Raleigh', 'Mecklenburg', 'Help', 'Accessibility'))]
                    for line in useful[:8]:
                        log.info(f"  [{i+1}] {line[:200]}")
                except Exception as e:
                    log.info(f"  [{i+1}]: {e}")
            
            page.close()
        
        with open(OUTPUT_DIR / "dubaitrade_exploration.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        
        # Print summary
        print("\n" + "="*80)
        print("DUBAI TRADE PORTAL EXPLORATION")
        print("="*80)
        
        print("\n--- Main Page Links ---")
        for link in results.get("main_links", []):
            href = link['href'].lower()
            txt = link['text']
            if any(kw in href + txt.lower() for kw in ['container', 'track', 'inquiry', 'cargo', 'import',
                'customs', 'delivery', 'gate', 'login', 'register', 'services', 'port']):
                print(f"  {txt}: {link['href']}")
        
        print("\n--- Dubai Customs Links ---")
        for link in results.get("dubaicustoms_links", []):
            print(f"  {link['text']}: {link['href']}")
        
        print("\n--- Subpage Redirects ---")
        for key, val in results.items():
            if isinstance(val, dict) and 'final_url' in val:
                print(f"  {key}: {val['final_url']}")
        
        print(f"\n{'='*80}")
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
