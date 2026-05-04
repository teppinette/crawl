#!/usr/bin/env python3
"""
Quick CIR submission script.

Usage:
    python3 cir_submit.py "WEBFORMA DE PANAMA, S.A." PA
    python3 cir_submit.py "Sinochem Singapore" SG --tax-id "UEN 199001234Z"
    python3 cir_submit.py "Acme GmbH" DE --poll          # submit + wait for result
    python3 cir_submit.py --status <job_id>               # check existing job
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from keyvault import get_secret

API_URL = "http://localhost:8400"
API_KEY = get_secret("cir-api-key")
HEADERS = {"Content-Type": "application/json", "X-API-Key": API_KEY}


def submit(args):
    payload = {
        "entity_legal_name": args.name,
        "entity_country": args.country.upper(),
        "workstreams": None,
    }
    if args.tax_id:
        payload["entity_tax_id"] = args.tax_id
    if args.jurisdiction:
        payload["entity_jurisdiction"] = args.jurisdiction
    if args.website:
        payload["entity_website"] = args.website
    if args.address:
        payload["entity_address"] = args.address
    if args.industry:
        payload["entity_industry"] = args.industry

    resp = requests.post(f"{API_URL}/api/v1/research", headers=HEADERS, json=payload)
    if resp.status_code != 200:
        print(f"ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)

    job = resp.json()
    job_id = job["job_id"]
    print(f"Submitted: {args.name} ({args.country.upper()})")
    print(f"Job ID:    {job_id}")
    print(f"Region:    {job['region']}")
    print(f"Status:    {job['status']}")

    if args.poll:
        poll_job(job_id)

    return job_id


def poll_job(job_id):
    print(f"\nPolling {job_id[:8]}...", flush=True)
    while True:
        time.sleep(30)
        resp = requests.get(f"{API_URL}/api/v1/research/{job_id}", headers=HEADERS)
        job = resp.json()
        status = job["status"]
        print(f"  [{time.strftime('%H:%M:%S')}] {status}", end="", flush=True)

        if status == "completed":
            print(f" -> blob: {job.get('blob_path', 'N/A')}")
            if job.get("report_summary"):
                print(f"\nSummary:\n{job['report_summary'][:500]}")
            break
        elif status == "failed":
            print(f" -> error: {job.get('error', 'unknown')}")
            break
        else:
            print("", flush=True)


def check_status(job_id):
    resp = requests.get(f"{API_URL}/api/v1/research/{job_id}", headers=HEADERS)
    if resp.status_code != 200:
        print(f"ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(resp.json(), indent=2))


def main():
    parser = argparse.ArgumentParser(description="Submit CIR research jobs")
    parser.add_argument("name", nargs="?", help="Entity legal name")
    parser.add_argument("country", nargs="?", help="ISO 2-letter country code")
    parser.add_argument("--tax-id", help="Tax ID / registration number")
    parser.add_argument("--jurisdiction", help="State/province/region")
    parser.add_argument("--website", help="Entity website URL")
    parser.add_argument("--address", help="Registered address")
    parser.add_argument("--industry", help="Sector/industry description")
    parser.add_argument("--poll", action="store_true", help="Wait for completion")
    parser.add_argument("--status", metavar="JOB_ID", help="Check status of existing job")

    args = parser.parse_args()

    if args.status:
        check_status(args.status)
    elif args.name and args.country:
        submit(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
