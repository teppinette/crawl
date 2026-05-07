"""Dubai Trade Container Enquiry — the actual form."""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dt-enq")

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
CONTAINERS = [
    "DFSU6580527", "FSCU8108794", "MEDU4958469", "MEDU7419218",
    "MSDU6153938", "MSDU7384361", "MSMU7788246", "MSNU5478137",
]
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
    log.info("=== Dubai Trade Container Enquiry Form ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "dt-enquiry-form")
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(60000)
            
            # 1. Try the new enquiry URL directly
            for url_name, url in [
                ("enquiry", "https://www.dubaitrade.ae/enquiry"),
                ("en_enquiry", "https://www.dubaitrade.ae/en/enquiry"),
                ("container_enquiry_intro", "https://www.dubaitrade.ae/en/container-enquiry-introduction"),
            ]:
                log.info(f"\n[{url_name}] {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    time.sleep(10)
                    
                    final_url = page.url
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / f"dt_enq_{url_name}.png"), full_page=True)
                    log.info(f"  {len(text)} chars -> {final_url}")
                    
                    # Print all text
                    print(f"\n=== {url_name.upper()} (final: {final_url}) ===")
                    for line in text.split('\n'):
                        if len(line.strip()) > 3:
                            print(f"  {line.strip()[:250]}")
                    
                    # Check for input fields
                    inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input, select, textarea')).map(i => ({
                        tag: i.tagName, type: i.type, id: i.id, name: i.name,
                        placeholder: i.placeholder || '', visible: i.offsetParent !== null,
                        class: i.className
                    }))""")
                    visible_inputs = [i for i in inputs if i['visible'] and i['type'] not in ('hidden', 'submit', 'button')]
                    if visible_inputs:
                        print(f"\n--- VISIBLE INPUTS ---")
                        print(json.dumps(visible_inputs, indent=2))
                    
                    # Check for iframes
                    iframes = page.evaluate("""() => Array.from(document.querySelectorAll('iframe')).map(f => ({
                        src: f.src, id: f.id, name: f.name
                    }))""")
                    if iframes:
                        print(f"\n--- IFRAMES ---")
                        for iframe in iframes:
                            print(f"  {json.dumps(iframe)}")
                            if iframe['src']:
                                log.info(f"  Navigating to iframe: {iframe['src']}")
                                try:
                                    # Access frame content
                                    frame = page.frame(url=iframe['src']) if '://' in iframe['src'] else None
                                    if frame:
                                        frame_text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
                                        frame_inputs = frame.evaluate("""() => Array.from(document.querySelectorAll('input, select')).map(i => ({
                                            type: i.type, id: i.id, name: i.name, placeholder: i.placeholder || '',
                                            visible: i.offsetParent !== null
                                        })).filter(i => i.visible)""")
                                        log.info(f"  Frame content: {len(frame_text)} chars, {len(frame_inputs)} inputs")
                                        if frame_text:
                                            print(f"\n--- IFRAME CONTENT ---")
                                            for line in frame_text.split('\n'):
                                                if len(line.strip()) > 3:
                                                    print(f"  {line.strip()[:250]}")
                                        if frame_inputs:
                                            print(f"\n--- IFRAME INPUTS ---")
                                            print(json.dumps(frame_inputs, indent=2))
                                            
                                            # Try filling container number in iframe
                                            for fi in frame_inputs:
                                                if fi['type'] in ('text', 'search'):
                                                    try:
                                                        sel = f"#{fi['id']}" if fi['id'] else f"input[name='{fi['name']}']" if fi['name'] else "input[type='text']:visible"
                                                        field = frame.locator(sel).first
                                                        field.click()
                                                        field.fill(CONTAINERS[0])
                                                        log.info(f"  Filled container: {CONTAINERS[0]}")
                                                        time.sleep(1)
                                                        # Try search button
                                                        try:
                                                            frame.locator('button:has-text("Search"), input[type="submit"], button:has-text("Enquiry")').first.click()
                                                        except:
                                                            field.press("Enter")
                                                        time.sleep(10)
                                                        result_text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
                                                        page.screenshot(path=str(OUTPUT_DIR / f"dt_enq_result_{CONTAINERS[0]}.png"), full_page=True)
                                                        log.info(f"  RESULT: {len(result_text)} chars")
                                                        print(f"\n=== CONTAINER ENQUIRY RESULT ===")
                                                        for line in result_text.split('\n'):
                                                            if len(line.strip()) > 3:
                                                                print(f"  {line.strip()[:250]}")
                                                    except Exception as e2:
                                                        log.warning(f"  Fill failed: {e2}")
                                                    break
                                except Exception as e:
                                    log.warning(f"  Frame access: {e}")
                    
                    # If this is the intro page, click on "Enquiry" tab
                    if "introduction" in url:
                        try:
                            tab = page.locator('a:has-text("Enquiry")').first
                            tab_href = tab.get_attribute("href")
                            log.info(f"  Enquiry tab href: {tab_href}")
                            if tab_href and tab_href != "#":
                                page.goto(tab_href if tab_href.startswith("http") else f"https://www.dubaitrade.ae{tab_href}", 
                                    wait_until="domcontentloaded", timeout=30000)
                                time.sleep(10)
                                tab_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                                page.screenshot(path=str(OUTPUT_DIR / "dt_enq_tab.png"), full_page=True)
                                log.info(f"  Enquiry tab page: {len(tab_text)} chars -> {page.url}")
                                print(f"\n=== ENQUIRY TAB ===")
                                for line in tab_text.split('\n'):
                                    if len(line.strip()) > 3:
                                        print(f"  {line.strip()[:250]}")
                        except Exception as e:
                            log.warning(f"  Enquiry tab: {e}")
                            
                except Exception as e:
                    log.warning(f"  {url_name}: {e}")
            
            # 2. Tasreeh gate pass system 
            log.info("\n[tasreeh] Gate pass system")
            try:
                page.goto("https://tasreeh.ae/en/services/gate-passes", wait_until="domcontentloaded", timeout=30000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "dt_tasreeh.png"), full_page=True)
                log.info(f"  Tasreeh: {len(text)} chars -> {page.url}")
                print(f"\n=== TASREEH GATE PASS ===")
                for line in text.split('\n'):
                    if len(line.strip()) > 3:
                        print(f"  {line.strip()[:250]}")
            except Exception as e:
                log.warning(f"  Tasreeh: {e}")
            
            page.close()
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
