#!/usr/bin/env python3
"""
Seed entity list generator for OpenClaw OSINT research.

Connects to production ComplianceEntity (READ-ONLY) and generates plain-text
seed commands for OpenClaw. Output is entity name + country code ONLY --
no EntityIDs, no financial data, no internal identifiers.

This script runs on the dev machine, NOT on any OpenClaw VM.

Usage:
    python seed_entities.py --country CN          # single country
    python seed_entities.py --country IN,TR,AE    # multiple countries
    python seed_entities.py --all                  # all entities
    python seed_entities.py --country CN --limit 5 # test with 5

Output:
    seeds/<country_code>_entities.txt   -- one "Research: <name>, <CC>" per line
    seeds/all_entities.txt              -- combined file
"""

import argparse
import os
import sys
from datetime import datetime

try:
    import pyodbc
except ImportError:
    print("ERROR: pyodbc required. Install with: pip install pyodbc")
    sys.exit(1)


def get_connection():
    """Connect to production SQL (read-only)."""
    server = os.environ.get("SEED_SQL_SERVER", "172.20.0.6")
    username = os.environ.get("SEED_SQL_USERNAME", "copapadmin")
    password = os.environ.get("SEED_SQL_PASSWORD")

    if not password:
        print("ERROR: SEED_SQL_PASSWORD environment variable not set.")
        print("This script reads from production DB (read-only). Set the password.")
        sys.exit(1)

    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};DATABASE=globalcompliance;"
        f"UID={username};PWD={password};"
        f"TrustServerCertificate=yes;Encrypt=no"
    )
    return pyodbc.connect(conn_str, timeout=15)


def fetch_entities(conn, countries=None, limit=None):
    """Fetch entity names and countries from ComplianceEntity."""
    query = """
        SELECT CanonicalName, Country
        FROM dbo.ComplianceEntity
        WHERE MatchStatus NOT IN ('merged', 'deleted')
          AND IsBlacklisted = 0
          AND CanonicalName IS NOT NULL
          AND Country IS NOT NULL
    """
    params = []

    if countries:
        placeholders = ",".join(["?" for _ in countries])
        query += f" AND Country IN ({placeholders})"
        params.extend(countries)

    query += " ORDER BY Country, CanonicalName"

    if limit:
        query += f" OFFSET 0 ROWS FETCH NEXT ? ROWS ONLY"
        params.append(limit)

    cursor = conn.cursor()
    cursor.execute(query, params)
    return cursor.fetchall()


def generate_seed_files(entities, output_dir="seeds"):
    """Write seed files grouped by country."""
    os.makedirs(output_dir, exist_ok=True)

    by_country = {}
    for name, country in entities:
        by_country.setdefault(country, []).append(name)

    all_lines = []
    for country in sorted(by_country.keys()):
        names = by_country[country]
        filename = os.path.join(output_dir, f"{country}_entities.txt")
        lines = [f"Research: {name}, {country}" for name in names]

        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"  {filename}: {len(lines)} entities")

        all_lines.extend(lines)

    # Combined file
    all_file = os.path.join(output_dir, "all_entities.txt")
    with open(all_file, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines) + "\n")
    print(f"  {all_file}: {len(all_lines)} total entities")

    return len(all_lines)


def generate_swarmclaw_dispatch(entities, output_dir="seeds"):
    """Generate SwarmClaw dispatch commands that route to correct regional agent."""
    jurisdiction_map = {
        "US": "americas", "CA": "americas", "CO": "americas",
        "TR": "europe", "RU": "europe", "BY": "europe", "RS": "europe",
        "NG": "europe", "UA": "europe",
        "AE": "gulf", "EG": "gulf", "PK": "gulf", "IQ": "gulf",
        "CN": "china", "HK": "china", "VN": "china", "MM": "china",
        "IN": "india",
    }

    filename = os.path.join(output_dir, "swarmclaw_dispatch.sh")
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write("# SwarmClaw dispatch commands -- routes each entity to correct regional agent\n")
        f.write(f"# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n")

        for name, country in entities:
            agent = jurisdiction_map.get(country, "americas")
            safe_name = name.replace('"', '\\"')
            f.write(
                f'swarmclaw task create --agent {agent} '
                f'--message "Research: {safe_name}, {country}"\n'
            )

    os.chmod(filename, 0o755)
    print(f"  {filename}: {len(entities)} dispatch commands")


def main():
    parser = argparse.ArgumentParser(
        description="Generate OpenClaw seed entity lists from ComplianceEntity"
    )
    parser.add_argument(
        "--country", type=str, default=None,
        help="Comma-separated country codes (e.g., CN,IN,TR)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Export all entities (all countries)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit total entities (for testing)"
    )
    parser.add_argument(
        "--output", type=str, default="seeds",
        help="Output directory (default: seeds/)"
    )
    args = parser.parse_args()

    if not args.country and not args.all:
        parser.error("Specify --country <codes> or --all")

    countries = None
    if args.country:
        countries = [c.strip().upper() for c in args.country.split(",")]

    print(f"Connecting to production DB (read-only)...")
    conn = get_connection()

    print(f"Fetching entities...")
    entities = fetch_entities(conn, countries=countries, limit=args.limit)
    print(f"Found {len(entities)} entities")

    if not entities:
        print("No entities found. Check country codes.")
        conn.close()
        return

    print(f"\nGenerating seed files in {args.output}/...")
    total = generate_seed_files(entities, args.output)

    print(f"\nGenerating SwarmClaw dispatch script...")
    generate_swarmclaw_dispatch(entities, args.output)

    conn.close()
    print(f"\nDone. {total} entities ready for OpenClaw research.")
    print(f"\nTo send to OpenClaw (single VM):")
    print(f"  cat {args.output}/CN_entities.txt | while read line; do openclaw send \"$line\"; done")
    print(f"\nTo dispatch via SwarmClaw (multi-agent):")
    print(f"  bash {args.output}/swarmclaw_dispatch.sh")


if __name__ == "__main__":
    main()
