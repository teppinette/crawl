"""Final targeted: Diligencia, IPC Credit, ImportYeti deep, plus Zauba/PortExaminer."""
import hashlib, json, logging, subprocess, time, os
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("final")

def get_secret(name):
    try:
        r = subprocess.run(["az","keyvault","secret","show","--vault-name","crawlkeyvault",
            "--name",name,"--query","value","-o","tsv"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip()
    except: return os.environ.get(name.upper().replace("-","_"), "")

MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
MLX_PASSWORD = get_secret("multilogin-password")
MLX_FOLDER_ID = get_secret("multilogin-folder-id")
MLX_PROXY_USER = get_secret("multilogin-proxy-user")
MLX_PROXY_PASS = get_secret("multilogin-proxy-pass")
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
              "parameters":{"fingerprint":{},
                  **({"proxy":{"type":"http","host":"gate.multilogin.com","port":8080,
                      "username":MLX_PROXY_USER,"password":MLX_PROXY_PASS}}
                     if MLX_PROXY_USER and MLX_PROXY_PASS else {})}},
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
    log.info("=== Final Corporate + Trade Data Search ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    token = mlx_auth()
    pid = mlx_create(token, "final-search")
    results = {}
    
    try:
        port = mlx_launch(token, pid)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.set_default_timeout(45000)
            
            # 1. Diligencia ClarifiedBy 
            log.info("\n[1] Diligencia ClarifiedBy")
            try:
                page.goto("https://clarifiedby.diligenciagroup.com/summary/566", wait_until="domcontentloaded", timeout=45000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "final_diligencia.png"), full_page=True)
                results["diligencia"] = text[:10000]
                log.info(f"  Diligencia: {len(text)} chars")
                print("\n=== DILIGENCIA ===")
                for line in text.split('\n'):
                    if len(line.strip()) > 5:
                        print(f"  {line.strip()[:250]}")
            except Exception as e:
                log.warning(f"  Diligencia: {e}")
            
            # Try the full URL from search results
            try:
                page.goto("https://clarifiedby.diligenciagroup.com/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                # Search for the company
                inputs = page.locator('input:visible')
                for i in range(inputs.count()):
                    inp = inputs.nth(i)
                    ph = inp.get_attribute("placeholder") or ""
                    if "search" in ph.lower() or "company" in ph.lower() or not ph:
                        inp.click()
                        inp.fill("Super Save General Trading LLC")
                        time.sleep(1)
                        inp.press("Enter")
                        time.sleep(8)
                        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                        page.screenshot(path=str(OUTPUT_DIR / "final_diligencia_search.png"), full_page=True)
                        results["diligencia_search"] = text[:8000]
                        log.info(f"  Diligencia search: {len(text)} chars")
                        print("\n=== DILIGENCIA SEARCH ===")
                        for line in text.split('\n'):
                            if len(line.strip()) > 10:
                                print(f"  {line.strip()[:200]}")
                        break
            except Exception as e:
                log.warning(f"  Diligencia search: {e}")
            
            # 2. IPC Credit report
            log.info("\n[2] IPC Credit")
            try:
                page.goto("https://www.icpcredit.com/Report/ReportRequest?companyName=super+save+general+trading", wait_until="domcontentloaded", timeout=30000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "final_icpcredit.png"), full_page=True)
                results["icpcredit"] = text[:8000]
                log.info(f"  IPC Credit: {len(text)} chars")
                for line in text.split('\n'):
                    if len(line.strip()) > 10:
                        log.info(f"    {line.strip()[:200]}")
            except Exception as e:
                log.warning(f"  IPC Credit: {e}")
            
            # 3. ImportYeti — get full page including all data attributes
            log.info("\n[3] ImportYeti — full company data")
            importyeti_urls = [
                "https://www.importyeti.com/company/super-save-general-trading-llc",
                "https://www.importyeti.com/company/super-save-general-trading",
                "https://www.importyeti.com/company/super+save+general+trading",
            ]
            for url in importyeti_urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(10)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    if len(text) > 500:
                        page.screenshot(path=str(OUTPUT_DIR / "final_importyeti.png"), full_page=True)
                        results["importyeti"] = text[:15000]
                        log.info(f"  ImportYeti: {len(text)} chars from {url}")
                        print("\n=== IMPORTYETI ===")
                        for line in text.split('\n'):
                            if len(line.strip()) > 5:
                                print(f"  {line.strip()[:250]}")
                        break
                    else:
                        log.info(f"  ImportYeti: {len(text)} chars (too short), trying next URL")
                except Exception as e:
                    log.warning(f"  ImportYeti: {e}")
            
            # 4. PortExaminer / ZaUba for UAE
            log.info("\n[4] PortExaminer / maritime trade data")
            try:
                page.goto("https://www.portexaminer.com/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                results["portexaminer_home"] = text[:3000]
                log.info(f"  PortExaminer: {len(text)} chars")
                
                # Try to search
                inp = page.locator('input:visible').first
                try:
                    inp.click()
                    inp.fill("Super Save General Trading")
                    inp.press("Enter")
                    time.sleep(8)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / "final_portexaminer.png"), full_page=True)
                    results["portexaminer_search"] = text[:8000]
                    log.info(f"  PortExaminer search: {len(text)} chars")
                except: pass
            except Exception as e:
                log.warning(f"  PortExaminer: {e}")
            
            # 5. Panjiva (S&P Global) — trade data
            log.info("\n[5] Panjiva")
            try:
                page.goto("https://panjiva.com/search?q=super+save+general+trading", wait_until="domcontentloaded", timeout=30000)
                time.sleep(8)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "final_panjiva.png"), full_page=True)
                results["panjiva"] = text[:8000]
                log.info(f"  Panjiva: {len(text)} chars")
                for line in text.split('\n'):
                    if len(line.strip()) > 10:
                        log.info(f"    {line.strip()[:200]}")
            except Exception as e:
                log.warning(f"  Panjiva: {e}")
            
            # 6. Google — direct search for MSC UAE clearing agents and freight forwarders
            log.info("\n[6] Google — MSC agent in UAE / clearing")
            goog = [
                '"super save general trading" "delivery order" OR "DO number" OR "release order"',
                '"super save general trading" site:linkedin.com',
                '"super save general trading" "clearing agent" OR "customs clearance" OR "freight forward" dubai jebel',
                'super save general trading dubai owner director shareholder',
                '"super save general trading" dubai DED license 333430',
            ]
            for i, q in enumerate(goog):
                try:
                    page.goto(f"https://www.google.com/search?q={requests.utils.quote(q)}&num=20", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)
                    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    page.screenshot(path=str(OUTPUT_DIR / f"final_goog_{i+1}.png"), full_page=True)
                    results[f"goog_{i+1}"] = {"q": q, "text": text[:5000]}
                    useful = [l.strip() for l in text.split('\n') if len(l.strip()) > 20 
                        and not l.strip().startswith(('People also', 'Related', 'Raleigh', 'Mecklenburg', 'Help'))]
                    for line in useful[:15]:
                        log.info(f"  [{i+1}] {line[:200]}")
                except Exception as e:
                    log.warning(f"  [{i+1}]: {e}")
            
            # 7. LinkedIn search for Super Save employees 
            log.info("\n[7] LinkedIn — employee profiles")
            try:
                page.goto("https://www.google.com/search?q=site:linkedin.com+%22super+save+general+trading%22+dubai&num=20", wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                page.screenshot(path=str(OUTPUT_DIR / "final_linkedin_google.png"), full_page=True)
                results["linkedin_google"] = text[:8000]
                log.info(f"  LinkedIn Google: {len(text)} chars")
                print("\n=== LINKEDIN RESULTS ===")
                for line in text.split('\n'):
                    if 'linkedin' in line.lower() or 'super save' in line.lower():
                        if len(line.strip()) > 10:
                            print(f"  {line.strip()[:250]}")
            except Exception as e:
                log.warning(f"  LinkedIn: {e}")
            
            page.close()
        
        with open(OUTPUT_DIR / "final_search.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        
        print("\n" + "="*80)
        print("FINAL SEARCH COMPLETE")
        print("="*80)
        
    finally:
        mlx_stop(pid)
        mlx_delete(token, pid)
        log.info("Done")

if __name__ == "__main__":
    main()
