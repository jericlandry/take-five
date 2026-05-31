import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

NPI_API_URL = "https://npiregistry.cms.hhs.gov/api/"


async def search_npi(
    first_name: str,
    last_name: str,
    city: Optional[str] = None,
    state: Optional[str] = None,
    enumeration_type: Optional[str] = "NPI-1",
) -> list:
    """
    Search the CMS NPI Registry for healthcare providers.

    enumeration_type controls what kind of provider is searched:

    "NPI-1" (default) — Individual providers. Use for:
        Physicians (MD, DO), nurses, nurse practitioners, physician assistants,
        physical/occupational/speech therapists, psychiatrists, psychologists,
        social workers, dietitians, pharmacists, chiropractors, dentists,
        audiologists, optometrists, and any other licensed individual clinician.

    "NPI-2" — Organizational providers. Use for:
        Hospitals, home health agencies, skilled nursing facilities, assisted
        living facilities, hospice organizations, pharmacies, medical groups,
        outpatient clinics, labs, imaging centers, and any other provider
        entity that is not an individual person.

    None (omit) — Search both individual and organizational providers.
        Use when the caller doesn't know the provider type, or wants
        a broader result set.
    """
    params = {
        "first_name": first_name,
        "last_name": last_name,
        "limit": 5,
        "version": "2.1",
    }
    if enumeration_type:
        params["enumeration_type"] = enumeration_type
    if city:
        params["city"] = city
    if state:
        params["state"] = state

    async with httpx.AsyncClient() as client:
        resp = await client.get(NPI_API_URL, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    results = []
    for r in raw.get("results", []):
        basic      = r.get("basic", {})
        taxonomies = r.get("taxonomies", [])
        primary    = next((t for t in taxonomies if t.get("primary")), None)
        # Fall back to any taxonomy with a description if primary has none
        if not primary or not primary.get("desc"):
            primary = next((t for t in taxonomies if t.get("desc")), primary or {})
        addresses = r.get("addresses", [])
        practice  = next(
            (a for a in addresses if a.get("address_purpose") == "LOCATION"),
            addresses[0] if addresses else {}
        )
        results.append({
            "npi":           r.get("number"),
            "name":          f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip(),
            "credential":    basic.get("credential", ""),
            "specialty":     primary.get("desc", ""),
            "taxonomy_code": primary.get("code", ""),
            "phone":         practice.get("telephone_number", ""),
            "address":       f"{practice.get('address_1', '')} {practice.get('address_2', '')}".strip(),
            "city":          practice.get("city", ""),
            "state":         practice.get("state", ""),
            "postal_code":   practice.get("postal_code", ""),
        })

    return results
