"""Check Dubai Trade Open Services — no login required."""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dt-open")

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
    log.info("=== Dubai Trade Open Services (No Login) ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "dt-open-svc")
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(45000)
            
            # 1. Open Services landing — click through each provider
            log.info("\n[1] Open Services landing")
            page.goto("https://www.dubaitrade.ae/open-services-landing", wait_until="domcontentloaded", timeout=45000)
            time.sleep(8)
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            page.screenshot(path=str(OUTPUT_DIR / "dt_open_landing.png"), full_page=True)
            
            # Get all clickable elements/links
            links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href, text: a.innerText.trim().substring(0, 100)
            })).filter(a => a.text.length > 0)""")
            
            log.info(f"  Landing: {len(text)} chars, {len(links)} links")
            for link in links:
                if any(kw in (link['href'] + link['text']).lower() for kw in 
                    ['dp-world', 'dubai-customs', 'container', 'cargo', 'inquiry', 'track',
                     'gate', 'delivery', 'manifest', 'import', 'open-service']):
                    log.info(f"  >>> {link['text']}: {link['href']}")
            
            # Click on "DP World" section
            log.info("\n[2] DP World open services")
            try:
                dpw_link = page.locator('text="DP World"').first
                dpw_link.click()
                time.sleep(5)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "dt_open_dpworld.png"), full_page=True)
                log.info(f"  DP World section: {len(text)} chars")
                print("\n=== DP WORLD OPEN SERVICES ===")
                for line in text.split('\n'):
                    if len(line.strip()) > 5:
                        print(f"  {line.strip()[:250]}")
                
                # Get sub-links
                dpw_links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    href: a.href, text: a.innerText.trim().substring(0, 150)
                })).filter(a => a.text.length > 0)""")
                
                print("\n--- DP World Links ---")
                for link in dpw_links:
                    if any(kw in (link['href'] + link['text']).lower() for kw in 
                        ['container', 'inquiry', 'track', 'gate', 'delivery', 'cargo', 
                         'vessel', 'manifest', 'import', 'export', 'registration']):
                        print(f"  {link['text']}: {link['href']}")
                        
            except Exception as e:
                log.warning(f"  DP World section: {e}")
            
            # Click on "Dubai Customs" section
            log.info("\n[3] Dubai Customs open services")
            try:
                page.goto("https://www.dubaitrade.ae/open-services-landing", wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                dc_link = page.locator('text="Dubai Customs"').first
                dc_link.click()
                time.sleep(5)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "dt_open_customs.png"), full_page=True)
                log.info(f"  Dubai Customs section: {len(text)} chars")
                print("\n=== DUBAI CUSTOMS OPEN SERVICES ===")
                for line in text.split('\n'):
                    if len(line.strip()) > 5:
                        print(f"  {line.strip()[:250]}")
                
            except Exception as e:
                log.warning(f"  Dubai Customs section: {e}")
            
            # 4. Try DP World Registration Inquiry (no login?)
            log.info("\n[4] DP World Registration Inquiry")
            try:
                page.goto("https://www.dubaitrade.ae/en/dp-registration-tools-introduction", wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                
                # Click on "Registration Inquiry" tab
                inq_tab = page.locator('text="Registration Inquiry"')
                if inq_tab.count() > 0:
                    inq_tab.first.click()
                    time.sleep(5)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / "dt_reg_inquiry.png"), full_page=True)
                    log.info(f"  Registration Inquiry: {len(text)} chars")
                    
                    # Check for input fields
                    inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input:not([type="hidden"])')).map(i => ({
                        type: i.type, id: i.id, name: i.name, placeholder: i.placeholder || '',
                        visible: i.offsetParent !== null
                    })).filter(i => i.visible)""")
                    log.info(f"  Inputs: {json.dumps(inputs)}")
                    print("\n=== REGISTRATION INQUIRY ===")
                    for line in text.split('\n'):
                        if len(line.strip()) > 5:
                            print(f"  {line.strip()[:250]}")
                            
            except Exception as e:
                log.warning(f"  Registration Inquiry: {e}")
            
            # 5. Try open service links we found
            log.info("\n[5] Exploring open service sub-links")
            open_urls = [
                ("dpw_container_inq", "https://www.dubaitrade.ae/en/open-services/dp-world/container-inquiry"),
                ("dpw_vessel_inq", "https://www.dubaitrade.ae/en/open-services/dp-world/vessel-inquiry"),
                ("dpw_cargo_inq", "https://www.dubaitrade.ae/en/open-services/dp-world/cargo-inquiry"),
                ("dpw_import_inq", "https://www.dubaitrade.ae/en/open-services/dp-world/import-inquiry"),
                ("dpw_gate_inq", "https://www.dubaitrade.ae/en/open-services/dp-world/gate-inquiry"),
                ("dpw_delivery", "https://www.dubaitrade.ae/en/open-services/dp-world/delivery-order"),
                ("dc_container", "https://www.dubaitrade.ae/en/open-services/dubai-customs/container-inquiry"),
                ("dc_manifest", "https://www.dubaitrade.ae/en/open-services/dubai-customs/manifest-inquiry"),
                ("dc_cargo", "https://www.dubaitrade.ae/en/open-services/dubai-customs/cargo-inquiry"),
            ]
            
            for name, url in open_urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(5)
                    final_url = page.url
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    if len(text) > 100:
                        page.screenshot(path=str(OUTPUT_DIR / f"dt_open_{name}.png"), full_page=True)
                        log.info(f"  {name}: {len(text)} chars -> {final_url}")
                        
                        # Check for input fields
                        inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="submit"])')).map(i => ({
                            type: i.type, id: i.id, name: i.name, placeholder: i.placeholder || '',
                            visible: i.offsetParent !== null, label: ''
                        })).filter(i => i.visible)""")
                        if inputs:
                            log.info(f"    INPUTS FOUND: {json.dumps(inputs)}")
                            print(f"\n=== {name.upper()} — HAS INPUT FIELDS ===")
                            print(f"  URL: {final_url}")
                            print(f"  Inputs: {json.dumps(inputs, indent=2)}")
                            for line in text.split('\n'):
                                if len(line.strip()) > 5:
                                    print(f"  {line.strip()[:200]}")
                    else:
                        log.info(f"  {name}: {len(text)} chars (redirect?) -> {final_url}")
                except Exception as e:
                    log.info(f"  {name}: {e}")
            
            page.close()
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
