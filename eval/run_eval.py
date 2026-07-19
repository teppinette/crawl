#!/usr/bin/env python3
"""Golden-set eval harness for the CIR engine.

Two modes:
  --screening (default)  CHEAP, NO LLM. Screens every golden entity against the
     sanctions source and scores precision/recall. Loudly flags FALSE NEGATIVES
     (a missed true sanctions hit = regulatory incident). Run this before/after
     any model or pipeline change — it can't regress silently.
  --full                 Runs a real CIR per case (costs tokens; clean cases route
     to Haiku). Adds adverse-media recall, identity resolution, and groundedness.

Env: CIR_API_URL (default prod gateway), CIR_API_KEY (required).
Usage:
  CIR_API_KEY=... python3 eval/run_eval.py               # screening only
  CIR_API_KEY=... python3 eval/run_eval.py --full        # + full CIR eval
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get(
    "CIR_API_URL",
    "https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io"
).rstrip("/")
KEY = os.environ.get("CIR_API_KEY", "")
HERE = os.path.dirname(os.path.abspath(__file__))


def _api(path, body=None, method="POST", timeout=60):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-API-Key": KEY,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _load():
    with open(os.path.join(HERE, "golden_set.json")) as f:
        return json.load(f)["cases"]


def screening_eval(cases):
    print("\n=== SCREENING EVAL (deterministic, no LLM) ===")
    tp = fn = fp = tn = 0
    false_negatives = []
    for c in cases:
        exp = c["expect"]["sanctions"]
        try:
            r = _api("/api/v1/sources/opensanctions/search",
                     {"entity_name": c["name"], "country": (c["country"] or "").lower()},
                     timeout=60)
            total = int(r.get("total") or 0)
        except Exception as e:
            print(f"  ERROR {c['name']}: {str(e)[:80]}")
            continue
        got = "hit" if total > 0 else "clear"
        ok = (got == exp)
        if exp == "hit" and got == "hit":
            tp += 1
        elif exp == "hit" and got == "clear":
            fn += 1; false_negatives.append(c["name"])
        elif exp == "clear" and got == "hit":
            fp += 1
        else:
            tn += 1
        flag = "✓" if ok else ("✗✗ FALSE NEGATIVE" if exp == "hit" else "✗ false positive")
        print(f"  [{flag}] {c['name']:52} expect={exp:5} got={got:5} (candidates={total})")
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    print(f"\n  precision={prec:.2f}  recall={rec:.2f}  "
          f"(TP={tp} FN={fn} FP={fp} TN={tn})")
    if false_negatives:
        print(f"  🚨 FALSE NEGATIVES (missed sanctions hits): {false_negatives}")
    else:
        print("  ✅ no false negatives — every known sanctions hit was surfaced")
    return {"precision": prec, "recall": rec, "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "false_negatives": false_negatives}


def _run_cir(name, country, poll_s=240):
    r = _api("/api/v1/cir/run", {"entity_name": name, "country_code": country})
    rid = r.get("run_id")
    if not rid:
        return None
    t0 = time.time()
    while time.time() - t0 < poll_s:
        run = _api(f"/api/v1/evidence/runs/{rid}", method="GET", timeout=30)
        if run.get("status") in ("complete", "failed"):
            break
        time.sleep(15)
    ev = _api(f"/api/v1/evidence/runs/{rid}/evidence", method="GET", timeout=40).get("evidence", [])
    rd = _api(f"/api/v1/evidence/runs/{rid}/renders", method="GET", timeout=40).get("renders", [])
    cm = next((x for x in rd if x.get("render_type") == "cir_markdown"), None)
    return {"run_id": rid, "status": run.get("status"), "evidence": ev,
            "render": (cm or {}).get("payload") or {}}


def full_eval(cases):
    print("\n=== FULL CIR EVAL (runs CIRs — costs tokens; clean→Haiku) ===")
    adv_tp = adv_fn = adv_fp = 0
    id_ok = id_total = 0
    grounds = []
    for c in cases:
        res = _run_cir(c["name"], c["country"])
        if not res or res["status"] != "complete":
            print(f"  [skip] {c['name']}: {res and res['status']}")
            continue
        ev = res["evidence"]
        # adverse detected?
        adverse = False
        idhit = False
        for e in ev:
            ex = e.get("extracted") if isinstance(e.get("extracted"), dict) else {}
            if ex.get("adverse_findings") or (e.get("source_id") == "darkweb_screen" and ex.get("findings")):
                adverse = True
            want_id = c["expect"].get("identity")
            if want_id and want_id.lower() in json.dumps(ex, default=str).lower():
                idhit = True
        exp_adv = c["expect"].get("adverse")
        if exp_adv is True and adverse: adv_tp += 1
        elif exp_adv is True and not adverse: adv_fn += 1
        elif exp_adv is False and adverse: adv_fp += 1
        if c["expect"].get("identity"):
            id_total += 1; id_ok += 1 if idhit else 0
        g = (res["render"].get("grounding") or {}).get("grounding_score")
        if g is not None: grounds.append(g)
        model = res["render"].get("model", "?")
        print(f"  {c['name']:48} adverse={adverse}(exp {exp_adv}) "
              f"id={'✓' if idhit else '—'} ground={g} model={model}")
    print(f"\n  adverse recall: TP={adv_tp} FN={adv_fn} FP={adv_fp}")
    if id_total:
        print(f"  identity resolution: {id_ok}/{id_total}")
    if grounds:
        print(f"  mean grounding: {sum(grounds)/len(grounds):.1f}%  (min {min(grounds)})")


def main():
    if not KEY:
        print("ERROR: set CIR_API_KEY"); sys.exit(1)
    cases = _load()
    print(f"golden set: {len(cases)} cases | gateway {BASE}")
    screening_eval(cases)
    if "--full" in sys.argv:
        full_eval(cases)
    else:
        print("\n(run with --full to also eval adverse recall + identity + groundedness)")


if __name__ == "__main__":
    main()
