"""
Mexico DENUE (INEGI) company verification.

Source: https://www.inegi.org.mx/app/api/denue/v1/consulta/
Free API — token obtained by email from INEGI.
5M+ establishments, search by company name, returns legal name,
address, economic activity, employee size.

NOTE: INEGI API returns non-standard HTTP 000 for errors/empty results,
which breaks all standard HTTP clients. We use raw sockets to handle this.
The BuscarEntidad endpoint requires a state code (entity_code 0 = HTTP 000),
so we search the top 10 business states first, then remaining if needed.

Input: entity_name (company name search)
Returns: legal_name, address, economic_activity, employee_size, coordinates
"""

import json
import logging
import re
import socket
import ssl
import time
import urllib.parse

log = logging.getLogger("verify-gateway")

_API_HOST = "www.inegi.org.mx"
_API_BASE = "/app/api/denue/v1/consulta"
_TOKEN = ""

# Mexican states ordered by business density (CDMX, Edo Mex, NL, Jalisco first)
_PRIORITY_STATES = [9, 15, 19, 14, 21, 25, 8, 28, 30, 11]
_ALL_STATES = list(range(1, 33))

# SSL context (reusable)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def init(get_secret):
    global _TOKEN
    _TOKEN = get_secret("denue-api-token") or ""
    if _TOKEN:
        log.info("MX DENUE ready (INEGI API token configured)")
    else:
        log.warning("MX DENUE not configured — set denue-api-token in Key Vault")


def denue_verify(entity_name: str, rfc: str = "") -> dict:
    """
    Verify a Mexican company via INEGI DENUE business directory.

    Searches by company name across Mexican states. RFC is accepted
    but DENUE doesn't search by RFC — it's recorded in the result.
    """
    if not entity_name:
        return {"found": False, "error": "entity_name required"}

    if not _TOKEN:
        return {
            "entity_name": entity_name,
            "found": False,
            "error": "DENUE API token not configured — register at inegi.org.mx",
        }

    try:
        results = _search_all_states(entity_name)

        if not results:
            simplified = _simplify_name(entity_name)
            if simplified != entity_name:
                results = _search_all_states(simplified)

        if not results:
            return {
                "entity_name": entity_name,
                "rfc": rfc or None,
                "found": False,
                "status": "NOT_FOUND",
                "source": "DENUE (INEGI), Mexico",
                "validation_source": _validation_source(entity_name),
            }

        # Rank results: prefer where Razon_social matches the query
        results = _rank_results(results, entity_name)
        best = results[0]
        return _format_result(best, entity_name, rfc, len(results), results[:5])

    except Exception as e:
        log.error("MX DENUE error for %s: %s", entity_name, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _raw_get(path: str) -> list:
    """
    Raw socket HTTP GET to INEGI API.

    INEGI returns HTTP 000 for errors/empty results which breaks all
    standard HTTP clients. We parse the response manually.
    """
    try:
        sock = socket.create_connection((_API_HOST, 443), timeout=15)
        ssock = _SSL_CTX.wrap_socket(sock, server_hostname=_API_HOST)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {_API_HOST}\r\n"
            f"Accept: application/json\r\n"
            f"User-Agent: Mozilla/5.0\r\n"
            f"Connection: close\r\n\r\n"
        )
        ssock.sendall(req.encode())

        data = b""
        ssock.settimeout(10)
        while True:
            try:
                chunk = ssock.recv(8192)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break
        ssock.close()

        text = data.decode("utf-8", errors="replace")
        if "\r\n\r\n" not in text:
            return []

        status_line = text.split("\r\n")[0]
        body = text.split("\r\n\r\n", 1)[1]

        # HTTP 000 = no results or error (INEGI's non-standard way)
        if "200" not in status_line or not body:
            return []

        return json.loads(body)

    except Exception as e:
        log.debug("DENUE raw GET failed: %s", str(e)[:100])
        return []


def _search_state(name: str, state_code: int) -> list:
    """Search DENUE in a specific state."""
    encoded = urllib.parse.quote(name, safe="")
    path = f"{_API_BASE}/BuscarEntidad/{encoded}/{state_code:02d}/1/10/{_TOKEN}"
    return _raw_get(path)


def _search_all_states(name: str) -> list:
    """Search priority states first, stop when we find enough results."""
    all_results = []

    # Search priority states first (covers ~80% of businesses)
    for st in _PRIORITY_STATES:
        results = _search_state(name, st)
        if results:
            all_results.extend(results)
            # If we found 5+ results in priority states, good enough
            if len(all_results) >= 5:
                return all_results

    # If nothing found, search remaining states
    if not all_results:
        remaining = [s for s in _ALL_STATES if s not in _PRIORITY_STATES]
        for st in remaining:
            results = _search_state(name, st)
            if results:
                all_results.extend(results)
                if len(all_results) >= 5:
                    return all_results

    return all_results


def _rank_results(results: list, query: str) -> list:
    """Rank results by relevance — prefer Razon_social match over Nombre match."""
    q = _simplify_name(query).upper()

    def score(r):
        razon = (r.get("Razon_social", "") or "").strip().upper()
        nombre = (r.get("Nombre", "") or "").strip().upper()
        # Exact Razon_social match
        if razon == q:
            return 0
        # Razon_social starts with query
        if razon and razon.startswith(q):
            return 1
        # Query is contained in Razon_social
        if razon and q in razon:
            return 2
        # Exact Nombre match
        if nombre == q:
            return 3
        # Nombre starts with query
        if nombre.startswith(q):
            return 4
        # Fallback
        return 5

    return sorted(results, key=score)


def _simplify_name(name: str) -> str:
    """Remove common Mexican corporate suffixes for broader search."""
    suffixes = [
        r"\bS\.?A\.?\s+DE\s+C\.?V\.?\b",
        r"\bS\.?\s*DE\s+R\.?L\.?\s+DE\s+C\.?V\.?\b",
        r"\bS\.?A\.?P\.?I\.?\s+DE\s+C\.?V\.?\b",
        r"\bS\.?A\.?\b$",
        r"\bS\.?C\.?\b$",
        r"\bS\.?\s*DE\s+R\.?L\.?\b$",
    ]
    result = name.strip()
    for pat in suffixes:
        result = re.sub(pat, "", result, flags=re.IGNORECASE).strip()
    return result.strip(" ,.")


def _format_result(record: dict, query_name: str, rfc: str,
                   total_matches: int, top_matches: list) -> dict:
    """Format DENUE API record into standard verification response."""
    legal_name = record.get("Nombre", "")
    razon_social = record.get("Razon_social", "")
    activity = record.get("Clase_actividad", "")
    personnel = record.get("Estrato", "")
    establishment_id = record.get("Id", "")

    # Address
    street_type = record.get("Tipo_vialidad", "")
    street = record.get("Calle", record.get("Nom_vialidad", ""))
    ext_num = record.get("Num_Exterior", record.get("Numero_ext", ""))
    int_num = record.get("Num_Interior", record.get("Numero_int", ""))
    colonia = record.get("Colonia", "")
    cp = record.get("CP", record.get("Cod_postal", ""))
    location = record.get("Ubicacion", "")
    phone = record.get("Telefono", "")
    email = record.get("Correo_e", "")
    website = record.get("Sitio_internet", "")
    longitude = record.get("Longitud", "")
    latitude = record.get("Latitud", "")

    addr_parts = []
    if street_type and street:
        addr_parts.append(f"{street_type} {street}")
    elif street:
        addr_parts.append(street)
    if ext_num and ext_num != "0":
        addr_parts.append(f"#{ext_num}")
    if int_num:
        addr_parts.append(f"Int. {int_num}")
    if colonia:
        addr_parts.append(colonia)
    if cp:
        addr_parts.append(f"C.P. {cp}")
    if location:
        addr_parts.append(location.strip())
    address = ", ".join(p for p in addr_parts if p and p.strip())

    other_matches = []
    for m in top_matches[1:]:
        other_matches.append({
            "name": m.get("Nombre", ""),
            "legal_name": m.get("Razon_social", ""),
            "activity": m.get("Clase_actividad", ""),
            "location": m.get("Ubicacion", ""),
        })

    display_name = razon_social.strip() if razon_social.strip() else legal_name

    return {
        "entity_name": display_name,
        "query_name": query_name,
        "rfc": rfc or None,
        "found": True,
        "status": "ACTIVE",
        "establishment_name": legal_name if legal_name != razon_social else None,
        "legal_name": razon_social.strip() or None,
        "economic_activity": activity or None,
        "employee_size": personnel or None,
        "registered_address": address or None,
        "postal_code": cp or None,
        "phone": phone or None,
        "email": email or None,
        "website": website or None,
        "coordinates": {
            "latitude": latitude,
            "longitude": longitude,
        } if latitude and longitude else None,
        "denue_id": str(establishment_id) if establishment_id else None,
        "total_matches": total_matches,
        "other_matches": other_matches if other_matches else None,
        "source": "DENUE (Directorio Estadístico Nacional de Unidades Económicas), INEGI, Mexico",
        "validation_source": _validation_source(query_name),
    }


def _validation_source(query_name: str) -> dict:
    return {
        "registry": "DENUE — Directorio Estadístico Nacional de Unidades Económicas, INEGI, Mexico",
        "url": "https://www.inegi.org.mx/app/mapa/denue/default.aspx",
        "how_to_reproduce": (
            f"Visit inegi.org.mx/app/mapa/denue → "
            f"Search: {query_name} → View establishment details"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
