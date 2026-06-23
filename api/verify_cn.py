"""
China company verification via Multilogin + CN residential proxy.

Uses Multilogin anti-detect browser with China proxy to access
Qichacha (qcc.com) or GSXT (gsxt.gov.cn) for company lookups.

Input: company name or USCC (Unified Social Credit Code, 18 chars).
Returns: company name, USCC, legal representative, status, registered capital.
"""

import base64
import hashlib
import json
import logging
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("verify-gateway")

_MLX_EMAIL = None
_MLX_PASSWORD = None
_MLX_FOLDER_ID = None
_MLX_PROXY_USER_CN = None
_MLX_PROXY_PASS_CN = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

_pool: queue.Queue = queue.Queue()
_pool_initialized = False


def init(get_secret):
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID
    global _MLX_PROXY_USER_CN, _MLX_PROXY_PASS_CN, _POOL_PROFILE_IDS

    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")

    # Get CN proxy creds from Multilogin CLI
    try:
        result = subprocess.run(
            [str(_CLI_PATH), "proxy-get", "--country-code", "cn",
             "--protocol", "http", "--type", "sticky"],
            capture_output=True, text=True, timeout=15,
        )
        if result.stdout.strip():
            parts = result.stdout.strip().split(":")
            _MLX_PROXY_USER_CN = parts[2]
            _MLX_PROXY_PASS_CN = parts[3]
    except Exception:
        pass

    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []

    if _MLX_PASSWORD and _MLX_PROXY_USER_CN and _POOL_PROFILE_IDS:
        log.info("CN verification ready: %d pool profiles, CN proxy configured", len(_POOL_PROFILE_IDS))
    else:
        log.warning("CN verification not fully configured")


def _init_pool():
    global _pool_initialized
    if _pool_initialized:
        return
    for pid in _POOL_PROFILE_IDS:
        _pool.put(pid)
    _pool_initialized = True


def _get_token() -> str:
    global _cached_token, _token_expiry
    with _token_lock:
        if time.monotonic() < _token_expiry and _cached_token:
            return _cached_token
        resp = requests.post(
            "https://api.multilogin.com/user/signin",
            json={"email": _MLX_EMAIL, "password": hashlib.md5(_MLX_PASSWORD.encode()).hexdigest()},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"]["http_code"] != 200:
            raise RuntimeError(f"MLX sign-in failed: {data['status']['message']}")
        _cached_token = data["data"]["token"]
        _token_expiry = time.monotonic() + 300
        return _cached_token


def _launch_profile(token: str, profile_id: str) -> int:
    url = (
        f"https://launcher.mlx.yt:45001/api/v2/profile"
        f"/f/{_MLX_FOLDER_ID}/p/{profile_id}"
        f"/start?automation_type=playwright&headless_mode=true"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, verify=False, timeout=60)
    data = resp.json()
    if data["status"]["http_code"] != 200:
        raise RuntimeError(f"MLX launch failed: {data['status']['message']}")
    return int(data["data"]["port"])


def _stop_profile(profile_id: str):
    try:
        subprocess.run([str(_CLI_PATH), "profile-stop", "--profile-id", profile_id],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def _do_cn_lookup(port: int, entity_name: str, uscc: str, profile_id: str) -> dict:
    result = {}
    error = None

    def _run():
        nonlocal result, error
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.new_context(
                    proxy={
                        "server": "http://gate.multilogin.com:8080",
                        "username": _MLX_PROXY_USER_CN,
                        "password": _MLX_PROXY_PASS_CN,
                    },
                    ignore_https_errors=True,
                )
                page = context.new_page()
                try:
                    result.update(_navigate_and_extract(page, entity_name, uscc))
                finally:
                    page.close()
                    context.close()
                    browser.close()
        except Exception as e:
            error = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=120)

    if t.is_alive():
        log.error("CN lookup HUNG for '%s' — force-stopping profile %s", entity_name, profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError("CN lookup timed out (120s)")
    if error:
        raise error
    return result


def _navigate_and_extract(page, entity_name: str, uscc: str) -> dict:
    """Search Tianyancha for company. Falls back to Baidu if Tianyancha fails."""
    search_term = uscc if uscc else entity_name

    # Tianyancha works with CN proxy, no login needed for search results
    page.goto(f"https://www.tianyancha.com/search?key={search_term}", timeout=60000, wait_until="domcontentloaded")
    time.sleep(10)

    body = page.inner_text("body")

    # Check if redirected to login
    if ("扫码登录" in body or "登录/注册" in body) and "法定代表人" not in body:
        # Tianyancha blocked — try Baidu as fallback
        log.info("Tianyancha needs login, falling back to Baidu")
        page.goto(f"https://www.baidu.com/s?wd={search_term}", timeout=60000, wait_until="domcontentloaded")
        time.sleep(8)
        body = page.inner_text("body")
        return _parse_baidu_result(entity_name, uscc, body)

    return _parse_tianyancha_result(entity_name, uscc, body)


_CN_CORP_SUFFIXES = [
    "集团股份有限公司", "集团控股有限公司", "股份有限公司",
    "有限责任公司", "集团有限公司", "控股有限公司", "有限公司",
    "(集团)", "（集团）",
]

_CN_PROVINCES = [
    # National prefix used by SOEs ("中国" + brand + industry). Strip so the
    # brand is the discriminator, not the shared 中国 prefix.
    "中国",
    "北京", "上海", "天津", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽",
    "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "内蒙古", "广西", "西藏", "宁夏", "新疆",
]


def _normalize_cn_name(name: str) -> str:
    """Strip corporate suffixes, parens, and leading geography to expose brand."""
    if not name:
        return ""
    n = name
    # Strip parens and contents (handles half-width + full-width)
    n = re.sub(r"[(（][^()（）]*[)）]", "", n)
    # Strip corporate suffixes (longest first — already ordered)
    for s in _CN_CORP_SUFFIXES:
        n = n.replace(s, "")
    # Strip leading province
    for p in _CN_PROVINCES:
        if n.startswith(p):
            n = n[len(p):]
            n = re.sub(r"^(?:省|市|自治区|自治州|地区)", "", n)
            break
    # Strip leading "XX市" (2-3 char prefecture city)
    n = re.sub(r"^[一-鿿]{2,3}市", "", n)
    return n.strip()


def _cn_brand(normalized: str) -> str:
    """First 2-4 CJK chars of a normalized name. This is typically the brand;
    industry descriptors (科技/集团/贸易/...) follow."""
    chars = [c for c in normalized if "一" <= c <= "鿿"]
    return "".join(chars[:4])


def _name_match_cn(query: str, returned: str) -> tuple[float, str]:
    """Match a queried entity name against what Tianyancha returned.
    Returns (score 0.0-1.0, reason). Score >= _CN_NAME_MATCH_THRESHOLD = accept.

    Conservative on purpose: better to return verified=false on a related-but-
    distinct match (e.g. Alibaba/Ant Group, CNPC/Sinopec, or a sibling sub
    inside the same group) than to confidently surface the wrong entity."""
    if not query or not returned:
        return 0.0, "empty"
    q = _normalize_cn_name(query)
    r = _normalize_cn_name(returned)
    if not q or not r:
        return 0.0, "empty after normalize"
    # Strongest: one normalized name is a substring of the other.
    # Handles "腾讯" → "腾讯计算机系统", "阿里巴巴(中国)" → "阿里巴巴集团控股", etc.
    if q in r or r in q:
        return 1.0, "substring"
    # Otherwise brand-prefix check. The first 2 CJK chars after normalization
    # are the brand discriminator (阿里 / 蚂蚁 / 腾讯 / 华为). If those don't
    # match exactly, it's a different company even if the rest looks similar.
    qb, rb = _cn_brand(q), _cn_brand(r)
    if not qb or not rb:
        return 0.0, "no brand"
    if qb[:2] != rb[:2]:
        return 0.0, f"brand prefix differs ({qb[:2]} vs {rb[:2]})"
    # Brand prefix matches — also require strong overlap on the full brand
    # window to catch SOE-prefix collisions (CNPC 中国石油天然气 vs
    # Sinopec 中国石油化工 — both start 石油 after stripping 中国, but
    # the rest diverges).
    qs, rs = set(qb), set(rb)
    overlap = len(qs & rs)
    smaller = min(len(qs), len(rs))
    ratio = overlap / smaller
    return ratio, f"brand overlap {overlap}/{smaller}"


# Threshold below which Tianyancha's first-result match is treated as a wrong
# entity. Tianyancha sometimes returns related-but-distinct companies
# (e.g. Alibaba → Ant Group). Better to return verified=false than to
# confidently surface the wrong entity to a banker. 0.75 means brand-prefix
# matches AND ≥3 of 4 brand-window chars overlap — strict enough to reject
# SOE-prefix collisions (CNPC vs Sinopec) and intra-group sibling subs.
_CN_NAME_MATCH_THRESHOLD = 0.75


def _parse_tianyancha_result(entity_name: str, uscc: str, body: str) -> dict:
    """Parse Tianyancha search results page."""
    result = {
        "entity_name": entity_name,
        "source": "Tianyancha (tianyancha.com) via CN residential proxy",
    }

    # Tianyancha search results format:
    # 公司名称\n存续\n小微企业\n法定代表人：XXX\n注册资本：XXX万人民币\n成立日期：XXXX-XX-XX\n统一社会信用代码：XXXXXXXXXXXXXXXXXX

    # Find the FIRST search result that matches (skip filter menus)
    # Look for pattern: 法定代表人：followed by name
    rep_match = re.search(r"法定代表人[：:]\s*(\S+)", body)
    legal_rep = rep_match.group(1).strip() if rep_match else None

    # USCC
    uscc_match = re.search(r"统一社会信用代码[：:]\s*([0-9A-Z]{18})", body)
    found_uscc = uscc_match.group(1) if uscc_match else uscc

    # Registered capital
    capital_match = re.search(r"注册资本[：:]\s*([^\n]+?)(?:\n|$)", body)
    capital = capital_match.group(1).strip() if capital_match else None

    # Established date
    date_match = re.search(r"成立日期[：:]\s*(\d{4}-\d{2}-\d{2})", body)
    est_date = date_match.group(1) if date_match else None

    # Status — look for 存续 or 注销 near the company name
    status = None
    for s in ["存续", "在业", "注销", "吊销", "迁出"]:
        if s in body:
            status = s
            break

    # Company name — find the name before 法定代表人
    # Search for Chinese company name pattern near the results section
    name = None
    if rep_match:
        # Look backwards from 法定代表人 for a company name
        before_rep = body[:rep_match.start()]
        # Find last company name pattern
        names = re.findall(r"([\u4e00-\u9fff][\u4e00-\u9fff\w()（）]{4,}(?:有限公司|股份有限公司|集团有限公司))", before_rep)
        if names:
            # Take the last one (closest to the result data)
            name = names[-1]

    # Phone
    phone_match = re.search(r"电话[：:]\s*([\d*]+)", body)
    phone = phone_match.group(1) if phone_match else None

    # Email
    email_match = re.search(r"邮箱[：:]\s*(\S+@\S+)", body)
    email = email_match.group(1) if email_match else None

    # Address
    addr_match = re.search(r"地址[：:]\s*([^\n]+?)(?:\n|$)", body)
    address = addr_match.group(1).strip() if addr_match else None

    # Flags
    flags = []
    for flag in ["小微企业", "司法案件", "失信被执行人", "被执行人", "经营异常", "行政处罚", "高新技术企业"]:
        if flag in body:
            flags.append(flag)

    # Name-match gate. Tianyancha can return a related-but-distinct entity as
    # the first search hit (e.g. searching Alibaba returns Ant Group). If the
    # query was a Chinese name and the returned legal_name is too different,
    # reject the match rather than confidently surfacing the wrong entity.
    # A queried USCC that matches what Tianyancha returned bypasses the gate
    # (USCC is the unique identifier — trust it over name similarity).
    name_match_score = None
    name_match_reason = None
    name_mismatch = False
    has_cjk_query = bool(re.search(r"[一-鿿]", entity_name or ""))
    query_uscc_matches = bool(uscc) and (found_uscc == uscc)

    if name and has_cjk_query and not query_uscc_matches:
        name_match_score, name_match_reason = _name_match_cn(entity_name, name)
        if name_match_score < _CN_NAME_MATCH_THRESHOLD:
            name_mismatch = True

    if (name or found_uscc or legal_rep) and not name_mismatch:
        result["found"] = True
        result["legal_name"] = name
        result["uscc"] = found_uscc
        result["legal_representative"] = legal_rep
        result["status"] = status
        result["registered_capital"] = capital
        result["established_date"] = est_date
        result["phone"] = phone
        result["email"] = email
        result["address"] = address
        result["flags"] = flags if flags else None
        if name_match_score is not None:
            result["name_match_score"] = round(name_match_score, 2)
        result["validation_source"] = {
            "registry": "State Administration for Market Regulation (SAMR), People's Republic of China",
            "url": "https://www.tianyancha.com",
            "record_id": found_uscc or entity_name,
            "how_to_reproduce": f"Visit tianyancha.com → Search for '{entity_name}'",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    elif name_mismatch:
        # Tianyancha had a candidate but it doesn't match the searched entity.
        # Return the candidate so the operator can inspect, but verified=false.
        result["found"] = False
        result["verified"] = False
        result["note"] = (
            f"Tianyancha returned a candidate that does not match '{entity_name}': "
            f"'{name}' (similarity={name_match_score:.2f}, threshold={_CN_NAME_MATCH_THRESHOLD}). "
            "Likely a different entity — Tianyancha's first search hit can be a "
            "related but distinct company. Re-query with the exact registered "
            "name or a USCC to confirm."
        )
        result["candidate"] = {
            "legal_name": name,
            "uscc": found_uscc,
            "legal_representative": legal_rep,
            "status": status,
            "name_match_score": round(name_match_score, 2),
        }
    else:
        result["found"] = False
        result["note"] = f"'{entity_name}' not found on Tianyancha"
        result["raw_snippet"] = body[:500]

    return result


def _parse_baidu_result(entity_name: str, uscc: str, body: str) -> dict:
    """Parse Baidu search results for company info (fallback)."""
    result = {
        "entity_name": entity_name,
        "source": "Baidu search (baidu.com) via CN residential proxy",
    }

    rep_match = re.search(r"法定代表人[：:]\s*(\S+)", body)
    legal_rep = rep_match.group(1).strip() if rep_match else None

    uscc_match = re.search(r"统一社会信用代码[：:为]\s*([0-9A-Z]{18})", body)
    found_uscc = uscc_match.group(1) if uscc_match else uscc

    capital_match = re.search(r"注册资本[：:为]\s*([^\s,，]+)", body)
    capital = capital_match.group(1).strip() if capital_match else None

    name = None
    names = re.findall(r"([\u4e00-\u9fff][\u4e00-\u9fff\w()（）]{4,}(?:有限公司|股份有限公司))", body)
    if names:
        name = names[0]

    scope_match = re.search(r"经营范围[：:为]\s*([^\n]+?)(?:\n|\.\.\.)", body)
    scope = scope_match.group(1).strip() if scope_match else None

    if name or found_uscc or legal_rep:
        result["found"] = True
        result["legal_name"] = name
        result["uscc"] = found_uscc
        result["legal_representative"] = legal_rep
        result["registered_capital"] = capital
        result["business_scope"] = scope
        result["validation_source"] = {
            "registry": "SAMR (via Baidu aggregated data)",
            "url": "https://www.baidu.com",
            "record_id": found_uscc or entity_name,
            "how_to_reproduce": f"Search baidu.com for '{entity_name}'",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    else:
        result["found"] = False
        result["note"] = f"'{entity_name}' not found via Baidu search"
        result["raw_snippet"] = body[:500]

    return result


def cn_verify(entity_name: str, uscc: str = "", max_retries: int = 2) -> dict:
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS or not _MLX_PROXY_USER_CN:
        return {"entity_name": entity_name, "found": False, "note": "Multilogin/CN proxy not configured"}

    _init_pool()

    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        return {"entity_name": entity_name, "found": False, "note": "All profiles busy — try later"}

    try:
        for attempt in range(max_retries):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                return _do_cn_lookup(port, entity_name, uscc, profile_id)
            except Exception as e:
                log.warning("CN attempt %d/%d failed ('%s'): %s", attempt + 1, max_retries, entity_name, e)
                if attempt == max_retries - 1:
                    return {"entity_name": entity_name, "found": False, "error": str(e)[:200], "note": "CN lookup failed after retries"}
            finally:
                _stop_profile(profile_id)
    finally:
        _pool.put(profile_id)
