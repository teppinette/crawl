"""Dubai Trade Container Enquiry — open service, no login."""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("dt-enquiry")

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
    "MSNU5529424", "MSNU6792965", "MSNU7760767", "MSNU9153090",
    "MSNU9166158", "SEKU6825445", "SEKU6842139", "TCNU7279232",
    "TCNU8790592", "TGBU5852746", "TIIU4035545", "TXGU8624573",
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
    log.info("=== Dubai Trade Container Enquiry ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "dt-container-enq")
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(60000)
            
            # 1. Container Enquiry Introduction page
            log.info("\n[1] Container Enquiry Introduction")
            page.goto("https://www.dubaitrade.ae/en/container-enquiry-introduction", wait_until="domcontentloaded", timeout=45000)
            time.sleep(10)
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            page.screenshot(path=str(OUTPUT_DIR / "dt_container_enq_intro.png"), full_page=True)
            log.info(f"  Intro: {len(text)} chars")
            print("\n=== CONTAINER ENQUIRY INTRODUCTION ===")
            for line in text.split('\n'):
                if len(line.strip()) > 3:
                    print(f"  {line.strip()[:250]}")
            
            # Get all links
            links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href, text: a.innerText.trim().substring(0, 150)
            })).filter(a => a.text.length > 0)""")
            print("\n--- Links ---")
            for link in links:
                if any(kw in (link['href'] + link['text']).lower() for kw in 
                    ['enquiry', 'inquiry', 'container', 'search', 'start', 'proceed', 'service']):
                    print(f"  {link['text']}: {link['href']}")
            
            # Get form elements
            inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input, select, textarea')).map(i => ({
                tag: i.tagName, type: i.type, id: i.id, name: i.name, 
                placeholder: i.placeholder || '', visible: i.offsetParent !== null
            }))""")
            print(f"\nInputs: {json.dumps([i for i in inputs if i['visible']], indent=2)}")
            
            # Get iframes (services often embedded)
            iframes = page.evaluate("""() => Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: f.src, id: f.id, name: f.name, width: f.width, height: f.height
            }))""")
            if iframes:
                print(f"\nIframes: {json.dumps(iframes, indent=2)}")
                
                # Try to access iframe content
                for iframe_info in iframes:
                    if iframe_info['src']:
                        log.info(f"  Found iframe: {iframe_info['src']}")
                        try:
                            frame = page.frame(url=iframe_info['src'])
                            if frame:
                                frame_text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
                                log.info(f"  Iframe content: {len(frame_text)} chars")
                                print(f"\n=== IFRAME CONTENT ===")
                                for line in frame_text.split('\n'):
                                    if len(line.strip()) > 3:
                                        print(f"  {line.strip()[:250]}")
                                
                                # Check for inputs in iframe
                                frame_inputs = frame.evaluate("""() => Array.from(document.querySelectorAll('input, select, textarea')).map(i => ({
                                    tag: i.tagName, type: i.type, id: i.id, name: i.name,
                                    placeholder: i.placeholder || '', visible: i.offsetParent !== null
                                })).filter(i => i.visible)""")
                                if frame_inputs:
                                    print(f"\nFrame inputs: {json.dumps(frame_inputs, indent=2)}")
                        except Exception as e:
                            log.warning(f"  Iframe access: {e}")
            
            # 2. Try "START SERVICE" button or similar
            log.info("\n[2] Looking for service start button")
            try:
                buttons = page.evaluate("""() => Array.from(document.querySelectorAll('button, a.btn, a[class*="btn"], input[type="submit"]')).map(b => ({
                    text: b.innerText || b.value, href: b.href || '', visible: b.offsetParent !== null,
                    tag: b.tagName, class: b.className
                })).filter(b => b.visible)""")
                print(f"\nButtons: {json.dumps(buttons, indent=2)}")
                
                # Click "START SERVICE" if found
                start_btn = page.locator('a:has-text("START SERVICE"), button:has-text("START SERVICE"), a:has-text("Start Service")')
                if start_btn.count() > 0:
                    start_btn.first.click()
                    time.sleep(10)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / "dt_container_enq_service.png"), full_page=True)
                    log.info(f"  After START: {len(text)} chars")
                    print(f"\n=== AFTER START SERVICE ===")
                    for line in text.split('\n'):
                        if len(line.strip()) > 3:
                            print(f"  {line.strip()[:250]}")
                    
                    # Check for container input
                    service_inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input, select')).map(i => ({
                        tag: i.tagName, type: i.type, id: i.id, name: i.name,
                        placeholder: i.placeholder || '', visible: i.offsetParent !== null
                    })).filter(i => i.visible)""")
                    if service_inputs:
                        print(f"\nService inputs: {json.dumps(service_inputs, indent=2)}")
                        
                        # Try filling container number
                        for inp_info in service_inputs:
                            if inp_info['type'] in ('text', 'search'):
                                sel = f"#{inp_info['id']}" if inp_info['id'] else f"input[name='{inp_info['name']}']" if inp_info['name'] else "input[type='text']:visible"
                                try:
                                    field = page.locator(sel).first
                                    field.click()
                                    field.fill(CONTAINERS[0])
                                    time.sleep(1)
                                    # Submit
                                    try:
                                        page.locator('button:has-text("Search"), button:has-text("Enquiry"), button:has-text("Submit"), input[type="submit"]').first.click()
                                    except:
                                        field.press("Enter")
                                    time.sleep(10)
                                    result = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                                    page.screenshot(path=str(OUTPUT_DIR / "dt_container_enq_result.png"), full_page=True)
                                    log.info(f"  Search result: {len(result)} chars")
                                    print(f"\n=== CONTAINER SEARCH RESULT ===")
                                    for line in result.split('\n'):
                                        if len(line.strip()) > 3:
                                            print(f"  {line.strip()[:250]}")
                                except Exception as e:
                                    log.warning(f"  Fill failed: {e}")
                                break
                                
            except Exception as e:
                log.warning(f"  Start service: {e}")
            
            # 3. Also check the direct DP World container inquiry open service
            log.info("\n[3] DP World Container Inquiry (open)")
            page.goto("https://www.dubaitrade.ae/en/open-services/dp-world/container-inquiry", wait_until="domcontentloaded", timeout=30000)
            time.sleep(8)
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            page.screenshot(path=str(OUTPUT_DIR / "dt_dpw_container_inq.png"), full_page=True)
            log.info(f"  DP World container inquiry: {len(text)} chars")
            print(f"\n=== DP WORLD CONTAINER INQUIRY ===")
            for line in text.split('\n'):
                if len(line.strip()) > 3:
                    print(f"  {line.strip()[:250]}")
            
            # Check for iframes
            iframes = page.evaluate("""() => Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: f.src, id: f.id
            }))""")
            if iframes:
                print(f"\nIframes: {json.dumps(iframes, indent=2)}")
                for iframe_info in iframes:
                    if iframe_info['src']:
                        log.info(f"  Found iframe: {iframe_info['src']}")
                        # Navigate directly to iframe URL
                        try:
                            page.goto(iframe_info['src'], wait_until="domcontentloaded", timeout=30000)
                            time.sleep(8)
                            frame_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                            page.screenshot(path=str(OUTPUT_DIR / "dt_dpw_container_inq_iframe.png"), full_page=True)
                            log.info(f"  Iframe direct: {len(frame_text)} chars")
                            print(f"\n=== IFRAME DIRECT ===")
                            for line in frame_text.split('\n'):
                                if len(line.strip()) > 3:
                                    print(f"  {line.strip()[:250]}")
                        except Exception as e:
                            log.warning(f"  Iframe direct: {e}")
            
            # 4. Cargo Delivery Request (open service)
            log.info("\n[4] Cargo Delivery Request")
            page.goto("https://www.dubaitrade.ae/en/cargo-delivery-request-introduction", wait_until="domcontentloaded", timeout=30000)
            time.sleep(8)
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            page.screenshot(path=str(OUTPUT_DIR / "dt_cargo_delivery.png"), full_page=True)
            log.info(f"  Cargo Delivery: {len(text)} chars")
            print(f"\n=== CARGO DELIVERY REQUEST ===")
            for line in text.split('\n'):
                if len(line.strip()) > 3:
                    print(f"  {line.strip()[:250]}")
            
            page.close()
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
