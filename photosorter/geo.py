"""Offline reverse geocoding (GeoNames cities1000 via reverse_geocoder; no network)."""
from __future__ import annotations

import re

COUNTRIES = {
    "US": "United States", "CA": "Canada", "MX": "Mexico", "GB": "United Kingdom", "IE": "Ireland",
    "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain", "PT": "Portugal", "NL": "Netherlands",
    "BE": "Belgium", "CH": "Switzerland", "AT": "Austria", "DK": "Denmark", "SE": "Sweden", "NO": "Norway",
    "FI": "Finland", "IS": "Iceland", "PL": "Poland", "CZ": "Czechia", "HU": "Hungary", "GR": "Greece",
    "HR": "Croatia", "RO": "Romania", "UA": "Ukraine", "RU": "Russia", "TR": "Turkey", "IL": "Israel",
    "EG": "Egypt", "MA": "Morocco", "ZA": "South Africa", "KE": "Kenya", "NG": "Nigeria", "IN": "India",
    "CN": "China", "JP": "Japan", "KR": "South Korea", "TW": "Taiwan", "HK": "Hong Kong", "SG": "Singapore",
    "TH": "Thailand", "VN": "Vietnam", "PH": "Philippines", "ID": "Indonesia", "MY": "Malaysia",
    "AU": "Australia", "NZ": "New Zealand", "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "PE": "Peru",
    "CO": "Colombia", "CR": "Costa Rica", "PA": "Panama", "DO": "Dominican Republic", "JM": "Jamaica",
    "BS": "Bahamas", "PR": "Puerto Rico", "CU": "Cuba", "GT": "Guatemala", "BZ": "Belize", "HN": "Honduras",
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "QA": "Qatar", "JO": "Jordan", "LB": "Lebanon",
    "VA": "Vatican City", "MT": "Malta", "LU": "Luxembourg", "MC": "Monaco", "EE": "Estonia", "LV": "Latvia",
    "LT": "Lithuania", "SK": "Slovakia", "SI": "Slovenia", "RS": "Serbia", "BG": "Bulgaria", "AL": "Albania",
}

_rg = None


def _get_rg():
    global _rg
    if _rg is None:
        import reverse_geocoder as rg  # loads ~150k cities into a KD-tree (a few seconds)

        _rg = rg
    return _rg


def reverse_geocode(coords: list[tuple[float, float]]) -> list[dict]:
    """[(lat, lon)] -> [{cc, country, state, county, city, place}]."""
    if not coords:
        return []
    rg = _get_rg()
    results = rg.search(coords, mode=1, verbose=False)  # mode=1: single process (safe in frozen Windows exe)
    out = []
    for r in results:
        cc = r.get("cc", "")
        city, state, county = r.get("name", ""), r.get("admin1", ""), r.get("admin2", "")
        out.append({
            "cc": cc,
            "country": COUNTRIES.get(cc, cc),
            "state": state,
            "county": county,
            "city": city,
            "place": place_label(city, state, cc),
        })
    return out


def place_label(city: str, state: str, cc: str) -> str:
    if not city:
        return ""
    if cc == "US":
        return f"{city}, {state}" if state else city
    country = COUNTRIES.get(cc, cc)
    return f"{city}, {country}" if country else city


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str, max_len: int = 80) -> str:
    """Make a string safe as a Windows folder/file name."""
    s = _BAD.sub("_", s or "").strip().rstrip(". ")
    if s.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        s = "_" + s
    return s[:max_len].rstrip(". ") or "_"
