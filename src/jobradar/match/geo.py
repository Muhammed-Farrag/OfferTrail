"""Tiny location → country resolver for the locations seen in job alerts.

Unknown strings resolve to None (unknown); add entries as new locations appear.
"""

from __future__ import annotations

import re

_COUNTRIES: dict[str, tuple[str, ...]] = {
    "EG": ("egypt", "cairo", "new cairo", "giza", "alexandria", "6th of october", "sheikh zayed",
           "new administrative capital", "mansoura", "smart village"),
    "AE": ("united arab emirates", "uae", "dubai", "abu dhabi", "sharjah", "ajman"),
    "SA": ("saudi arabia", "ksa", "riyadh", "jeddah", "dammam", "khobar", "neom"),
    "QA": ("qatar", "doha"),
    "KW": ("kuwait",),
    "BH": ("bahrain", "manama"),
    "OM": ("oman", "muscat"),
    "JO": ("jordan", "amman"),
    "MA": ("morocco", "casablanca", "rabat"),
    "TN": ("tunisia", "tunis"),
    "GB": ("united kingdom", "uk", "england", "london", "manchester", "scotland"),
    "DE": ("germany", "berlin", "munich", "hamburg", "frankfurt"),
    "NL": ("netherlands", "amsterdam", "rotterdam", "eindhoven"),
    "FR": ("france", "paris"),
    "IE": ("ireland", "dublin"),
    "ES": ("spain", "madrid", "barcelona"),
    "PL": ("poland", "warsaw", "krakow"),
    "US": ("united states", "usa", "new york", "san francisco", "seattle", "austin", "boston"),
    "CA": ("canada", "toronto", "vancouver", "montreal"),
    "IN": ("india", "bengaluru", "bangalore", "hyderabad", "pune", "mumbai"),
    "PK": ("pakistan", "lahore", "karachi", "islamabad"),
}

# Regions a remote role may be scoped to, and the countries they contain (partial lists are
# fine: membership is only used to *accept* a match, never to reject one).
REGIONS: dict[str, frozenset[str]] = {
    "worldwide": frozenset({"*"}),
    "emea": frozenset({"EG", "AE", "SA", "QA", "KW", "BH", "OM", "JO", "MA", "TN", "GB", "DE",
                       "NL", "FR", "IE", "ES", "PL"}),
    "mena": frozenset({"EG", "AE", "SA", "QA", "KW", "BH", "OM", "JO", "MA", "TN"}),
    "middle east": frozenset({"EG", "AE", "SA", "QA", "KW", "BH", "OM", "JO"}),
    "africa": frozenset({"EG", "MA", "TN"}),
    "gcc": frozenset({"AE", "SA", "QA", "KW", "BH", "OM"}),
    "europe": frozenset({"GB", "DE", "NL", "FR", "IE", "ES", "PL"}),
    "eu": frozenset({"DE", "NL", "FR", "IE", "ES", "PL"}),
}

_PATTERNS = [
    (code, re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(n) for n in names) + r")(?![a-z])",
                      re.I))
    for code, names in _COUNTRIES.items()
]


def countries_in(text: str | None) -> list[str]:
    if not text:
        return []
    found = []
    for code, rx in _PATTERNS:
        if rx.search(text) and code not in found:
            found.append(code)
    return found


def resolve_country(location: str | None) -> str | None:
    """Country of an alert's location line; None if unknown or ambiguous."""
    found = countries_in(location)
    return found[0] if len(found) == 1 else None


def region_contains(region: str, country: str) -> bool:
    members = REGIONS.get(region.lower(), frozenset())
    return "*" in members or country in members
