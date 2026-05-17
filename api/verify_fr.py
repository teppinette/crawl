"""
France company verification via recherche-entreprises.api.gouv.fr.

Source: https://recherche-entreprises.api.gouv.fr/search
Official French government API (DINUM / data.gouv.fr).
Free — no auth, no rate limit observed, JSON response.
Full French business registry: SIREN, legal form, directors, address, activity.

Input: entity_name (search by name) or siren (9-digit SIREN number)
Returns: legal_name, siren, status, legal_form, directors, address,
         economic_activity, employee_range, creation_date, category
"""

import logging
import time

import requests

log = logging.getLogger("verify-gateway")

_API_URL = "https://recherche-entreprises.api.gouv.fr/search"
_PROXY = None


def init(get_secret):
    log.info("FR Entreprises ready (api.gouv.fr, no auth required, direct access)")


def entreprises_verify(entity_name: str, siren: str = "") -> dict:
    """
    Verify a French company via the official business registry API.

    Searches by company name or 9-digit SIREN number.
    """
    if not entity_name and not siren:
        return {"found": False, "error": "entity_name or siren required"}

    try:
        query = siren.strip() if siren else entity_name.strip()
        records = _search(query)

        if not records:
            return {
                "entity_name": entity_name,
                "siren": siren or None,
                "found": False,
                "status": "NOT_FOUND",
                "source": "Registre National des Entreprises (INSEE/INPI), France",
                "validation_source": _validation_source(query),
            }

        best = records[0]
        return _format_result(best, entity_name, siren, len(records), records[:5])

    except Exception as e:
        log.error("FR Entreprises error for %s: %s", entity_name or siren, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _search(query: str) -> list:
    """Search French business registry."""
    resp = requests.get(
        _API_URL,
        params={
            "q": query,
            "page": 1,
            "per_page": 10,
        },
        headers={"Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


def _format_result(record: dict, query_name: str, siren_input: str,
                   total_matches: int, top_matches: list) -> dict:
    """Format API record into standard verification response."""
    siren = record.get("siren", "")
    name = record.get("nom_complet", "")
    raison_sociale = record.get("nom_raison_sociale", "")
    sigle = record.get("sigle", "")
    etat = record.get("etat_administratif", "")
    nature_juridique = record.get("nature_juridique", "")
    date_creation = record.get("date_creation", "")
    categorie = record.get("categorie_entreprise", "")
    tranche_effectif = record.get("tranche_effectif_salarie", "")
    nb_etablissements = record.get("nombre_etablissements", 0)
    nb_ouverts = record.get("nombre_etablissements_ouverts", 0)

    # Siege (headquarters)
    siege = record.get("siege", {})
    address = siege.get("adresse", "")
    activite = siege.get("activite_principale", "")
    libelle_activite = siege.get("libelle_activite_principale", "")
    code_postal = siege.get("code_postal", "")
    commune = siege.get("libelle_commune", "")
    date_debut = siege.get("date_debut_activite", "")

    # Status mapping
    status_map = {
        "A": "ACTIVE",
        "C": "CEASED",
        "F": "CLOSED",
    }
    status = status_map.get(etat, etat.upper() if etat else "UNKNOWN")

    # Legal form mapping (common ones)
    legal_form_map = {
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
        "5498": "SA à CA (avec CTE surveillance)",
        "5499": "SA à directoire",
    }
    legal_form = legal_form_map.get(nature_juridique, nature_juridique)

    # Category mapping
    cat_map = {
        "GE": "Grande Entreprise",
        "ETI": "Entreprise de Taille Intermédiaire",
        "PME": "Petite/Moyenne Entreprise",
        "TPE": "Très Petite Entreprise",
    }
    category_display = cat_map.get(categorie, categorie)

    # Employee range mapping
    effectif_map = {
        "00": "0 salarié",
        "01": "1-2 salariés",
        "02": "3-5 salariés",
        "03": "6-9 salariés",
        "11": "10-19 salariés",
        "12": "20-49 salariés",
        "21": "50-99 salariés",
        "22": "100-199 salariés",
        "31": "200-249 salariés",
        "32": "250-499 salariés",
        "41": "500-999 salariés",
        "42": "1000-1999 salariés",
        "51": "2000-4999 salariés",
        "52": "5000-9999 salariés",
        "53": "10000+ salariés",
    }
    employee_range = effectif_map.get(str(tranche_effectif), str(tranche_effectif) if tranche_effectif else None)

    # Directors
    dirigeants = record.get("dirigeants", [])
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

    # Activity code
    activity_display = f"{activite} — {libelle_activite}" if activite and libelle_activite else activite or libelle_activite or None

    # Other matches
    other_matches = []
    for m in top_matches[1:]:
        other_matches.append({
            "name": m.get("nom_complet", ""),
            "siren": m.get("siren", ""),
            "status": m.get("etat_administratif", ""),
            "commune": (m.get("siege") or {}).get("libelle_commune", ""),
        })

    return {
        "entity_name": name,
        "query_name": query_name,
        "found": True,
        "status": status,
        "siren": siren or None,
        "raison_sociale": raison_sociale or None,
        "sigle": sigle or None,
        "legal_form": legal_form or None,
        "legal_form_code": nature_juridique or None,
        "creation_date": date_creation or None,
        "category": category_display or None,
        "employee_range": employee_range,
        "economic_activity": activity_display,
        "registered_address": address or None,
        "postal_code": code_postal or None,
        "commune": commune or None,
        "directors": directors if directors else None,
        "establishments_total": nb_etablissements,
        "establishments_active": nb_ouverts,
        "total_matches": total_matches,
        "other_matches": other_matches if other_matches else None,
        "source": "Registre National des Entreprises (INSEE/INPI), France",
        "validation_source": _validation_source(query_name or siren),
    }


def _validation_source(query: str) -> dict:
    return {
        "registry": "Registre National des Entreprises — INSEE / INPI, France",
        "url": "https://annuaire-entreprises.data.gouv.fr/",
        "api": "https://recherche-entreprises.api.gouv.fr/search",
        "how_to_reproduce": (
            f"Visit annuaire-entreprises.data.gouv.fr → "
            f"Search: {query} → View company details"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
