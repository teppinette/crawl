"""Targeted deep-dive on key leads: DcciInfo, Jebel Ali warehouse, Google Maps, JAFZA listing."""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("targeted")

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
    log.info("=== Targeted Deep Dive ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "targeted-v1")
    results = {}
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(45000)
            
            # 1. DcciInfo full page — get ALL content
            log.info("\n[1] DcciInfo — full profile")
            try:
                page.goto("https://dcciinfo.com/co/super-save-general-trading-llc-dubai/251100", wait_until="domcontentloaded", timeout=45000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                # Also get structured data
                all_links = page.evaluate("""() => Array.from(document.querySelectorAll('a')).map(a => ({
                    href: a.href, text: a.innerText.trim()
                })).filter(a => a.text.length > 0)""")
                results["dcciinfo_full"] = text
                results["dcciinfo_links"] = all_links
                page.screenshot(path=str(OUTPUT_DIR / "targeted_dcciinfo.png"), full_page=True)
                log.info(f"  DcciInfo: {len(text)} chars, {len(all_links)} links")
                print("\n=== DCCIINFO FULL TEXT ===")
                print(text[:5000])
                print(f"\n=== DCCIINFO LINKS ({len(all_links)}) ===")
                for link in all_links:
                    if any(kw in link['text'].lower() for kw in ['super save', 'clearing', 'freight', 'customs', 'transport', 'haulier', 'jebel', 'jafza']):
                        print(f"  {link['text']}: {link['href']}")
            except Exception as e:
                log.warning(f"  DcciInfo: {e}")
            
            # 2. Google: "Mina Jabal Ali" "super save" + phone
            log.info("\n[2] Google — Jebel Ali warehouse")
            goog_queries = [
                '"super save general trading" "mina jebel ali" OR "mina jabal ali" OR "gate 8"',
                '"super save general trading" "+971 56 447 4055"',
                '"super save general trading" "jebel ali free zone" warehouse',
                '"super save general trading" clearing agent OR customs broker name',
                'site:dubaitrade.ae "super save general trading"',
                'site:jafza.ae "super save general trading"',
                '"MEDUFX870746"',
                '"super save general trading" "notify party" OR "delivery order" OR "DO number"',
            ]
            for i, q in enumerate(goog_queries):
                try:
                    page.goto(f"https://www.google.com/search?q={requests.utils.quote(q)}&num=20", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / f"targeted_google_{i+1}.png"), full_page=True)
                    results[f"google_{i+1}"] = {"q": q, "text": text[:5000]}
                    log.info(f"  Google [{i+1}]: {len(text)} chars")
                    # Print non-empty lines
                    useful_lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 20 
                        and not l.strip().startswith(('People also', 'Related searches', 'Raleigh', 'Mecklenburg'))]
                    for line in useful_lines[:15]:
                        log.info(f"    {line[:200]}")
                except Exception as e:
                    log.warning(f"  Google [{i+1}]: {e}")
            
            # 3. Google Maps — get actual listing details with click
            log.info("\n[3] Google Maps — listing details")
            try:
                page.goto("https://www.google.com/maps/search/super+save+general+trading+dubai", wait_until="domcontentloaded", timeout=45000)
                time.sleep(10)
                
                # Click on the first result
                try:
                    first_result = page.locator('[role="feed"] > div').first
                    first_result.click()
                    time.sleep(5)
                except:
                    pass
                
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "targeted_gmaps_detail.png"), full_page=True)
                results["gmaps_detail"] = text[:8000]
                log.info(f"  Google Maps detail: {len(text)} chars")
                print("\n=== GOOGLE MAPS DETAIL ===")
                for line in text.split('\n'):
                    if len(line.strip()) > 5:
                        print(f"  {line.strip()[:200]}")
                        
            except Exception as e:
                log.warning(f"  Google Maps: {e}")
            
            # 4. Google: "super save general trading" + specific freight/clearing co names from UAE
            log.info("\n[4] Google — freight forwarder association")
            ff_queries = [
                '"super save general trading" "agility" OR "panalpina" OR "kuehne" OR "dhl" OR "aramex"',
                '"super save general trading" "al futtaim" OR "gac" OR "gulf agency" OR "al naboodah"',
                '"super save general trading" "barloworld" OR "tristar" OR "al shirawi" OR "khalidia"',
                '"super save general trading" freight forwarder dubai "jebel ali"',
                '"super save" "general trading" "clearing" dubai -"general trading license"',
            ]
            for i, q in enumerate(ff_queries):
                try:
                    page.goto(f"https://www.google.com/search?q={requests.utils.quote(q)}&num=20", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    results[f"ff_{i+1}"] = {"q": q, "text": text[:3000]}
                    log.info(f"  FF [{i+1}]: {len(text)} chars")
                    useful = [l.strip() for l in text.split('\n') if len(l.strip()) > 20 
                        and not l.strip().startswith(('People also', 'Related', 'Raleigh', 'Mecklenburg'))]
                    for line in useful[:10]:
                        log.info(f"    {line[:200]}")
                except Exception as e:
                    log.warning(f"  FF [{i+1}]: {e}")
            
            # 5. Try buy2send warehouse addresses page directly
            log.info("\n[5] buy2send — warehouse addresses")
            try:
                page.goto("https://www.buy2send.com", wait_until="domcontentloaded", timeout=45000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                
                # Look for warehouse address links
                links = page.evaluate("""() => Array.from(document.querySelectorAll('a')).map(a => ({
                    href: a.href, text: a.innerText.trim()
                })).filter(a => a.text.toLowerCase().includes('warehouse') || 
                    a.text.toLowerCase().includes('address') || 
                    a.text.toLowerCase().includes('contact'))""")
                log.info(f"  buy2send links: {json.dumps(links[:10])}")
                
                # Click "warehouse" links if found
                for link in links[:3]:
                    if link.get('href'):
                        try:
                            page.goto(link['href'], wait_until="domcontentloaded", timeout=30000)
                            time.sleep(5)
                            wh_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                            page.screenshot(path=str(OUTPUT_DIR / "targeted_buy2send_wh.png"), full_page=True)
                            results["buy2send_warehouse"] = wh_text[:5000]
                            log.info(f"  Warehouse page: {len(wh_text)} chars")
                            print("\n=== BUY2SEND WAREHOUSE PAGE ===")
                            for line in wh_text.split('\n'):
                                if any(kw in line.lower() for kw in ['address', 'warehouse', 'dubai', 'jebel', 'jafza', '+971', 'office', 'gate']):
                                    print(f"  {line.strip()[:200]}")
                            break
                        except: pass
                
                # Also check footer / about / contact pages
                for subpath in ['/about', '/contact', '/warehouse', '/shipping-addresses', '/faq']:
                    try:
                        page.goto(f"https://www.buy2send.com{subpath}", wait_until="domcontentloaded", timeout=20000)
                        time.sleep(5)
                        sub_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                        if len(sub_text) > 200:
                            results[f"buy2send{subpath.replace('/','_')}"] = sub_text[:5000]
                            log.info(f"  buy2send{subpath}: {len(sub_text)} chars")
                            for line in sub_text.split('\n'):
                                if any(kw in line.lower() for kw in ['address', 'warehouse', 'dubai', 'jebel', 'jafza', '+971', 'office', 'gate', 'super save']):
                                    log.info(f"    >>> {line.strip()[:200]}")
                    except: pass
                    
            except Exception as e:
                log.warning(f"  buy2send: {e}")
            
            # 6. colombomail.lk — warehouse addresses
            log.info("\n[6] colombomail — warehouse page")
            try:
                for subpath in ['', '/shipping', '/faq', '/about', '/warehouse-addresses', '/how-it-works']:
                    page.goto(f"https://colombomail.lk{subpath}", wait_until="domcontentloaded", timeout=20000)
                    time.sleep(5)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    if len(text) > 200:
                        results[f"colombomail{subpath.replace('/','_') or '_home'}"] = text[:5000]
                        for line in text.split('\n'):
                            if any(kw in line.lower() for kw in ['address', 'warehouse', 'dubai', 'jebel', 'jafza', '+971', 'office', 'super save', 'room', 'building', 'street']):
                                log.info(f"    [{subpath or '/'}] >>> {line.strip()[:200]}")
            except Exception as e:
                log.warning(f"  colombomail: {e}")
            
            page.close()
        
        with open(OUTPUT_DIR / "targeted_search.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        
        print("\n" + "="*80)
        print("TARGETED SEARCH — ALL ADDRESS/WAREHOUSE FINDINGS")
        print("="*80)
        
        addresses = set()
        phones = set()
        for key, val in results.items():
            text = val if isinstance(val, str) else val.get("text", "") if isinstance(val, dict) else ""
            for line in text.split('\n'):
                l = line.strip()
                if '+971' in l and len(l) < 100:
                    phones.add(l)
                if any(kw in l.lower() for kw in ['warehouse address', 'office', 'room ', 'building', 'floor', 'street', 'jebel ali', 'jafza', 'mina ', 'gate no']):
                    if len(l) > 15 and len(l) < 300:
                        addresses.add(l)
        
        print("\nPHONE NUMBERS:")
        for p in sorted(phones):
            print(f"  {p}")
        
        print("\nADDRESSES:")
        for a in sorted(addresses):
            print(f"  {a}")
        
        print(f"\n{'='*80}")
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
