"""Place-name normalization shared by the geocoder and the offline gazetteer.

Nominatim matches whole indexed words: it does not spell-correct and it does not
split compounds. Indian places are routinely written as one word ("busstand",
"railwaystation") or misspelled the same way ("bustand"), and one unmatched
token makes the entire query return nothing even when the town itself exists.
"""

import re

# Compounds people actually type into a pickup/destination field.
_COMPOUND_PATTERNS = (
    (r"bus\s*stand", "bus stand"),
    (r"bus\s*stop", "bus stop"),
    (r"bus\s*depot", "bus depot"),
    (r"bus\s*terminus", "bus terminus"),
    (r"railway\s*station", "railway station"),
    (r"railway\s*junction", "railway junction"),
    (r"airport\s*road", "airport road"),
    (r"main\s*road", "main road"),
    (r"cross\s*road", "cross road"),
    (r"ring\s*road", "ring road"),
    (r"by\s*-?\s*pass", "by pass"),
)
_COMPOUND_RES = tuple(
    (re.compile(r"\b" + pattern + r"\b", re.IGNORECASE), replacement)
    for pattern, replacement in _COMPOUND_PATTERNS
)

# Misspellings and abbreviations. "bustand" finds nothing upstream even though
# "bus stand" is there, and one unmatched word voids the whole query.
_TOKEN_FIXES = {
    "bustand": "bus stand",
    "bustandh": "bus stand",
    "bustend": "bus stand",
    "busstan": "bus stand",
    "busstand": "bus stand",
    "bustop": "bus stop",
    "busstop": "bus stop",
    "railwaystan": "railway station",
    "railwaystation": "railway station",
    "railwaystn": "railway station",
    "rlystation": "railway station",
    "rlystn": "railway station",
    "hosp": "hospital",
}
_TOKEN_FIX_RES = tuple(
    (re.compile(r"\b" + re.escape(typo) + r"\b", re.IGNORECASE), fixed)
    for typo, fixed in sorted(_TOKEN_FIXES.items(), key=lambda kv: -len(kv[0]))
)

# Words that describe a spot rather than name it. Dropped only when the full
# query returns nothing ("near X" -> "X") and ignored when matching the
# gazetteer, where they would otherwise match every "New Town".
FILLER_WORDS = {"near", "opposite", "opp", "beside", "behind", "next", "to",
                "the", "new", "old"}

# A fallback token is only worth querying when it can plausibly be a place name;
# asking for "stand" or "road" returns noise, not the town.
_DESCRIPTOR_TOKENS = {
    "stand", "stop", "station", "road", "street", "junction", "depot",
    "terminus", "airport", "temple", "hospital", "market", "bus", "railway",
    "hotel", "school", "college", "park", "bridge", "circle",
}


def normalize_place_query(q):
    """Fix Indian compound spellings ("busstand", "bustand" -> "bus stand")."""
    text = " ".join((q or "").split())
    for pattern, replacement in _COMPOUND_RES:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _TOKEN_FIX_RES:
        text = pattern.sub(replacement, text)
    # Tidy comma spacing but keep the separators: Nominatim reads them as
    # address-component boundaries ("area, city, state").
    text = re.sub(r"\s*,\s*", ", ", text)
    return " ".join(text.split())


def meaningful_tokens(text):
    """Lowercase word tokens, minus filler words."""
    return [tok for tok in re.findall(r"[0-9a-z]+", text.casefold())
            if tok and tok not in FILLER_WORDS]


def is_descriptor(token):
    return token.casefold() in _DESCRIPTOR_TOKENS
