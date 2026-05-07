"""Check Dubai Trade login portal and registration requirements."""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dt-login")

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
    log.info("=== Dubai Trade Login + Registration Check ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "dt-login-check")
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(45000)
            
            # 1. Login page
            log.info("\n[1] Login page")
            page.goto("https://eservices.dubaitrade.ae/portal2/sso/main.do", wait_until="domcontentloaded", timeout=45000)
            time.sleep(10)
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            page.screenshot(path=str(OUTPUT_DIR / "dt_login_page.png"), full_page=True)
            log.info(f"  Login page: {len(text)} chars")
            print("\n=== LOGIN PAGE ===")
            for line in text.split('\n'):
                if len(line.strip()) > 3:
                    print(f"  {line.strip()[:250]}")
            
            # Get all form elements
            forms = page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input')).map(i => ({
                    type: i.type, id: i.id, name: i.name, placeholder: i.placeholder || '',
                    visible: i.offsetParent !== null, value: i.value
                }));
                const buttons = Array.from(document.querySelectorAll('button, input[type="submit"]')).map(b => ({
                    text: b.innerText || b.value, type: b.type, visible: b.offsetParent !== null
                }));
                const links = Array.from(document.querySelectorAll('a')).map(a => ({
                    href: a.href, text: a.innerText.trim().substring(0, 100)
                })).filter(a => a.text.length > 0);
                return {inputs, buttons, links};
            }""")
            
            print("\n=== FORM ELEMENTS ===")
            print(f"Inputs: {json.dumps(forms['inputs'], indent=2)}")
            print(f"Buttons: {json.dumps(forms['buttons'], indent=2)}")
            print(f"\nLinks ({len(forms['links'])}):")
            for link in forms['links']:
                print(f"  {link['text']}: {link['href']}")
            
            # 2. Registration intro pages
            log.info("\n[2] Registration pages")
            reg_pages = [
                ("cargo_owner", "https://www.dubaitrade.ae/en/cargo-owner"),
                ("dp_reg_intro", "https://www.dubaitrade.ae/en/dp-registration-tools-introduction"),
                ("dc_reg_intro", "https://www.dubaitrade.ae/en/dc-reg-introduction"),
                ("open_services", "https://www.dubaitrade.ae/open-services-landing"),
            ]
            
            for name, url in reg_pages:
                log.info(f"  [{name}]")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(6)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / f"dt_reg_{name}.png"), full_page=True)
                    log.info(f"    {len(text)} chars")
                    print(f"\n=== {name.upper()} ===")
                    for line in text.split('\n'):
                        if len(line.strip()) > 5:
                            print(f"  {line.strip()[:250]}")
                except Exception as e:
                    log.warning(f"    {e}")
            
            # 3. Try the CargoWaves page (might have public tracking)
            log.info("\n[3] CargoWaves")
            try:
                page.goto("https://www.dubaitrade.ae/en/cargowaves", wait_until="domcontentloaded", timeout=30000)
                time.sleep(6)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "dt_cargowaves.png"), full_page=True)
                log.info(f"  CargoWaves: {len(text)} chars")
                print(f"\n=== CARGOWAVES ===")
                for line in text.split('\n'):
                    if len(line.strip()) > 5:
                        print(f"  {line.strip()[:250]}")
            except Exception as e:
                log.warning(f"  CargoWaves: {e}")
            
            page.close()
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
