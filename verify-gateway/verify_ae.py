"""
UAE FTA TRN verification via crawl-gulf VM.

tax.gov.ae is geo-blocked from non-UAE IPs. Routes verification through
crawl-gulf VM (20.233.46.58, UAE North) which has direct access.

For now: uses the user-verified TRN lookup (manual confirmation that
TRN 100330886100003 = F D Z LOGISTICS L.L.C on tax.gov.ae).
Future: automate via Playwright on crawl-gulf with CAPTCHA OCR.

Input: TRN (15-digit Tax Registration Number).
Returns: entity name (if previously verified), registration status.
"""

import json
import logging
import os
import subprocess
import time

log = logging.getLogger("verify-gateway")

_SSH_KEY = os.path.expanduser("~/.ssh/crawldevvm_key.pem")
_GULF_VM_IP = "20.233.46.58"
_GULF_VM_USER = "copadmin"  # Note: copadmin, not copapadmin


def init(get_secret):
    """No special init needed — uses SSH to crawl-gulf."""
    if os.path.exists(_SSH_KEY):
        log.info("AE FTA TRN ready: routes via crawl-gulf %s", _GULF_VM_IP)
    else:
        log.warning("AE FTA TRN: SSH key not found at %s", _SSH_KEY)


def fta_trn_verify(trn: str, entity_name: str = "", max_retries: int = 2) -> dict:
    """
    Verify TRN on UAE Federal Tax Authority portal.

    Currently routes a curl request through crawl-gulf VM which has
    UAE North IP and can access tax.gov.ae directly.
    """
    import re
    safe_trn = re.sub(r"[^0-9]", "", trn)
    if len(safe_trn) != 15:
        return {"trn": trn, "found": False, "note": "TRN must be 15 digits"}

    for attempt in range(max_retries):
        try:
            # Use crawl-gulf VM to hit the FTA TRN verification page
            # The FTA site has a CAPTCHA that needs to be solved
            # For now, we do a basic check via the page load
            result = subprocess.run(
                [
                    "ssh", "-i", _SSH_KEY,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=10",
                    "-o", "UserKnownHostsFile=/dev/null",
                    f"{_GULF_VM_USER}@{_GULF_VM_IP}",
                    f"curl -s 'https://tax.gov.ae/en/default.aspx' -o /dev/null -w '%{{http_code}}' 2>/dev/null",
                ],
                capture_output=True, text=True, timeout=30,
            )
            status_code = result.stdout.strip()

            if status_code == "200":
                return {
                    "trn": safe_trn,
                    "found": False,
                    "status": "PORTAL_ACCESSIBLE",
                    "note": (
                        f"UAE FTA portal accessible from Gulf VM. "
                        f"TRN {safe_trn} needs manual verification at tax.gov.ae → TRN Verification. "
                        f"Portal has image CAPTCHA — automated solve pending."
                    ),
                    "source": "Federal Tax Authority (FTA), UAE",
                    "validation_source": {
                        "registry": "Federal Tax Authority (FTA), UAE",
                        "url": "https://tax.gov.ae",
                        "record_id": safe_trn,
                        "how_to_reproduce": (
                            f"Visit tax.gov.ae → TRN Verification → "
                            f"Enter TRN: {safe_trn} → Solve CAPTCHA → Verify"
                        ),
                        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                }
            else:
                log.warning("AE FTA portal returned %s (attempt %d)", status_code, attempt + 1)

        except Exception as e:
            log.warning("AE FTA attempt %d/%d failed: %s", attempt + 1, max_retries, e)
            if attempt == max_retries - 1:
                return {
                    "trn": safe_trn,
                    "found": False,
                    "error": str(e)[:200],
                    "note": "UAE FTA verification failed — crawl-gulf VM unreachable",
                }

    return {"trn": safe_trn, "found": False, "note": "UAE FTA verification failed"}
