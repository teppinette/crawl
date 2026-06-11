"""
France verify — runs on the generic engine.

Source: recherche-entreprises.api.gouv.fr (DINUM / data.gouv.fr).
Official French government registry. Free, no auth, JSON response.
SIREN, legal form, directors, address, activity.
"""

import logging

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_STATUS_MAP = {"A": "ACTIVE", "C": "CEASED", "F": "CLOSED"}

_LEGAL_FORM_MAP = {
    "1000": "Entrepreneur individuel",
    "5498": "SA à conseil d'administration",
    "5499": "SA à directoire",
    "5505": "SA à directoire (entreprise 1/3)",
    "5510": "SA à directoire (entreprise 2/3)",
    "5599": "SA NCA",
    "5710": "SAS (Société par Actions Simplifiée)",
    "5720": "SASU (SAS Unipersonnelle)",
    "5800": "SE (Société Européenne)",
    "6540": "SARL (entre associés)",
}

_CATEGORY_MAP = {
    "GE": "Grande Entreprise",
    "ETI": "Entreprise de Taille Intermédiaire",
    "PME": "Petite/Moyenne Entreprise",
    "TPE": "Très Petite Entreprise",
}

_EFFECTIF_MAP = {
    "00": "0 salarié", "01": "1-2 salariés", "02": "3-5 salariés",
    "03": "6-9 salariés", "11": "10-19 salariés", "12": "20-49 salariés",
    "21": "50-99 salariés", "22": "100-199 salariés",
    "31": "200-249 salariés", "32": "250-499 salariés",
    "41": "500-999 salariés", "42": "1000-1999 salariés",
    "51": "2000-4999 salariés", "52": "5000-9999 salariés",
    "53": "10000+ salariés",
}


def init(get_secret):
    log.info("FR verify ready (engine) — Registre National des Entreprises (api.gouv.fr)")


def _parse_fr(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    results = data.get("results") or []
    if not results:
        return {"found": False}

    best = results[0]

    siren = best.get("siren", "")
    name = best.get("nom_complet", "") or best.get("nom_raison_sociale", "")
    etat = best.get("etat_administratif", "")
    nature_juridique = best.get("nature_juridique", "")
    date_creation = best.get("date_creation", "")
    categorie = best.get("categorie_entreprise", "")
    tranche_effectif = best.get("tranche_effectif_salarie", "")

    siege = best.get("siege") or {}
    address = siege.get("adresse", "")
    activite = siege.get("activite_principale", "")
    libelle_activite = siege.get("libelle_activite_principale", "")

    status = _STATUS_MAP.get(etat, etat.upper() if etat else "UNKNOWN")
    legal_form = _LEGAL_FORM_MAP.get(nature_juridique, nature_juridique or None)
    category_display = _CATEGORY_MAP.get(categorie, categorie or None)
    employee_range = _EFFECTIF_MAP.get(str(tranche_effectif), str(tranche_effectif) if tranche_effectif else None)

    founded_year = date_creation[:4] if date_creation and len(date_creation) >= 4 else None
    activity_display = (f"{activite} — {libelle_activite}".strip(" —")) or None

    dirigeants = best.get("dirigeants") or []
    directors = []
    for d in dirigeants[:10]:
        if d.get("type_dirigeant") == "personne physique":
            full_name = f"{d.get('prenoms', '')} {d.get('nom', '')}".strip()
            directors.append({
                "name": full_name,
                "role": d.get("qualite", ""),
                "birth_year": d.get("annee_de_naissance", ""),
                "nationality": d.get("nationalite"),
            })
        else:
            directors.append({
                "name": d.get("denomination", ""),
                "siren": d.get("siren", ""),
                "role": d.get("qualite", ""),
                "type": "legal_entity",
            })

    other_matches = [
        {
            "name": m.get("nom_complet", ""),
            "siren": m.get("siren", ""),
            "status": m.get("etat_administratif", ""),
            "commune": (m.get("siege") or {}).get("libelle_commune", ""),
        }
        for m in results[1:5]
    ]

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": siren or None,
        "headquarters": address or None,
        "founded_year": founded_year,
        "industry": activity_display,
        "directors": directors or None,
        "is_listed": False,  # API does not expose listing status
        # FR-specific extras passed through by engine
        "siren": siren or None,
        "raison_sociale": best.get("nom_raison_sociale") or None,
        "sigle": best.get("sigle") or None,
        "legal_form": legal_form,
        "legal_form_code": nature_juridique or None,
        "creation_date": date_creation or None,
        "category": category_display,
        "employee_range": employee_range,
        "economic_activity": activity_display,
        "establishments_total": best.get("nombre_etablissements") or None,
        "establishments_open": best.get("nombre_etablissements_ouverts") or None,
        "other_matches": other_matches or None,
        "total_matches": len(results),
        "status": status,
        "summary": (
            f"{name or entity_name} — SIREN {siren or 'N/A'} — {status}"
            + (f" — {category_display}" if category_display else "")
            + (f" — {employee_range}" if employee_range else "")
        ),
    }


FR_CONFIG = eng.CountryConfig(
    country_code="FR",
    source_name="Registre National des Entreprises (INSEE/INPI), France",
    transport=eng.T_MLX_HTTP,
    primary_url="https://recherche-entreprises.api.gouv.fr/search?q={q}&page=1&per_page=10",
    parser=_parse_fr,
    timeout=20,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://annuaire-entreprises.data.gouv.fr → search '{entity}' "
        "→ view fiche d'identité"
    ),
)


def entreprises_verify(entity_name: str, siren: str = "") -> dict:
    """FR verify entry point — backward compat with main.py routing."""
    query = (siren or "").strip() or entity_name
    return eng.run(FR_CONFIG, query, {"siren": siren})
