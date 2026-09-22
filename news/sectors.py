"""Sector + symbol tagging for news headlines.

tag(title) -> {"sectors": [...], "symbols": [...]}

* Sectors: keyword table covering every entry in schema.SECTORS, matched
  case-insensitively on word boundaries (so "bank" never fires inside
  "bankrupt").
* Symbols: two complementary matchers —
    1. an alias table of ~45 top F&O names mapping company-name phrases
       ("hdfc bank", "l&t", "dr reddy") to NSE symbols;
    2. exact-token lookup of the headline's words against the symbol
       universe loaded lazily from the NSE scrip master
       (shoonya_client.load_scripmaster('NSE'), EQ rows only) inside
       try/except — on any failure the built-in fallback list is used and
       the module keeps working.  1-3 letter symbols are ambiguous and
       only match when the headline shows them in exact UPPERCASE
       ("ITC posts profit" matches, "itc hotels" does not).

Offline testing: set_symbol_universe([...]) injects the universe so the
self-test never touches shoonya_client / network.
"""

from __future__ import annotations

import os
import re
import sys

try:
    from news.schema import SECTORS
except ImportError:                                 # run as news/sectors.py
    from schema import SECTORS

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── sector keyword table (covers all schema.SECTORS) ─────────────────────
SECTOR_KEYWORDS = {
    "BANKING": ["bank", "banks", "banking", "hdfc bank", "icici", "sbi",
                "state bank", "axis bank", "kotak", "indusind", "yes bank",
                "federal bank", "rbi", "repo rate", "npa", "casa",
                "psu bank", "nbfc", "deposit growth", "credit growth"],
    "IT": ["infosys", "tcs", "tata consultancy", "wipro", "tech mahindra",
           "hcl", "hcltech", "ltimindtree", "mphasis", "coforge",
           "persistent systems", "it stocks", "it sector", "it services",
           "software services", "outsourcing", "h-1b", "saas"],
    "PHARMA": ["pharma", "drug", "drugs", "usfda", "fda", "cipla",
               "sun pharma", "dr reddy", "lupin", "aurobindo", "biocon",
               "divi", "zydus", "glenmark", "alkem", "vaccine",
               "generic drugs", "bulk drugs", "healthcare", "hospital"],
    "AUTO": ["auto", "automobile", "carmaker", "maruti", "tata motors",
             "mahindra", "m&m", "bajaj auto", "hero moto", "eicher",
             "tvs motor", "ashok leyland", "hyundai", "ev",
             "electric vehicle", "two-wheeler", "passenger vehicle", "suv",
             "auto sales", "tyre", "apollo tyres", "mrf"],
    "METAL": ["metal", "metals", "steel", "tata steel", "jsw steel",
              "jindal", "sail", "hindalco", "vedanta", "nalco", "nmdc",
              "copper", "aluminium", "aluminum", "zinc", "iron ore",
              "mining", "ferrous"],
    "ENERGY": ["ongc", "oil", "crude", "brent", "reliance", "ril", "gas",
               "lng", "opec", "petrol", "diesel", "refinery", "refining",
               "gail", "indian oil", "ioc", "bpcl", "hpcl", "petronet",
               "oil india", "petroleum"],
    "FMCG": ["fmcg", "hindustan unilever", "hul", "itc", "nestle",
             "britannia", "dabur", "marico", "godrej consumer", "colgate",
             "tata consumer", "varun beverages", "emami",
             "consumer goods", "consumer staples", "packaged foods"],
    "REALTY": ["realty", "real estate", "dlf", "godrej properties",
               "oberoi realty", "prestige estates", "brigade", "sobha",
               "lodha", "macrotech", "phoenix mills", "housing sales",
               "home sales", "property prices", "rera"],
    "INFRA": ["infra", "infrastructure", "l&t", "larsen", "gmr",
              "adani ports", "construction", "highway", "highways", "nhai",
              "road project", "metro rail", "airport", "port", "ports",
              "capex", "irb", "railways", "rvnl", "epc"],
    "FINANCE": ["nbfc", "bajaj finance", "bajaj finserv", "mutual fund",
                "insurance", "insurer", "lic", "sbi life", "hdfc life",
                "icici lombard", "amc", "asset management", "broking",
                "brokerage", "aum", "chola", "shriram finance", "muthoot",
                "paytm", "jio financial", "credit card", "microfinance"],
    "TELECOM": ["telecom", "airtel", "bharti", "vodafone", "vodafone idea",
                "jio", "5g", "spectrum", "trai", "arpu", "indus towers",
                "telecom tariff", "dot", "broadband", "subscriber"],
    "CEMENT": ["cement", "ultratech", "ambuja", "acc", "shree cement",
               "dalmia", "jk cement", "ramco", "birla corp", "grasim",
               "clinker", "cement prices", "cement demand"],
    "POWER": ["power", "electricity", "ntpc", "power grid", "tata power",
              "adani power", "adani green", "renewable", "renewables",
              "solar", "wind energy", "discom", "thermal", "hydro",
              "coal india", "jsw energy", "suzlon", "transmission"],
    "DEFENCE": ["defence", "defense", "hal", "hindustan aeronautics",
                "bel", "bharat electronics", "bharat dynamics", "mazagon",
                "cochin shipyard", "garden reach", "drdo", "missile",
                "army", "navy", "air force", "ordnance", "defence order"],
    "CHEMICALS": ["chemical", "chemicals", "srf", "pidilite",
                  "aarti industries", "deepak nitrite", "tata chemicals",
                  "upl", "pi industries", "gujarat fluorochem",
                  "navin fluorine", "agrochem", "specialty chemicals",
                  "fertiliser", "fertilizer", "pesticide"],
}
assert set(SECTOR_KEYWORDS) == set(SECTORS), "sector table out of sync"


def _phrase_rx(words, flags=re.I):
    alt = "|".join(re.escape(w) for w in
                   sorted(words, key=len, reverse=True))
    return re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % alt, flags)


_SECTOR_RX = {sec: _phrase_rx(kws) for sec, kws in SECTOR_KEYWORDS.items()}

# ── symbol alias table (~45 top F&O names) ───────────────────────────────
# symbol -> company-name phrases; phrases of <=3 chars require an exact
# UPPERCASE token in the headline.
FALLBACK_FNO = {
    "RELIANCE":   ["reliance", "RIL"],
    "HDFCBANK":   ["hdfc bank"],
    "ICICIBANK":  ["icici bank", "icici"],
    "SBIN":       ["SBI", "state bank of india", "state bank"],
    "INFY":       ["infosys", "infy"],
    "TCS":        ["TCS", "tata consultancy"],
    "WIPRO":      ["wipro"],
    "HCLTECH":    ["hcltech", "hcl tech", "hcl technologies", "HCL"],
    "TECHM":      ["tech mahindra", "techm"],
    "AXISBANK":   ["axis bank"],
    "KOTAKBANK":  ["kotak", "kotak mahindra bank"],
    "INDUSINDBK": ["indusind"],
    "ITC":        ["ITC"],
    "LT":         ["l&t", "larsen & toubro", "larsen and toubro", "larsen"],
    "BHARTIARTL": ["airtel", "bharti airtel"],
    "MARUTI":     ["maruti", "maruti suzuki"],
    "TATAMOTORS": ["tata motors"],
    "M&M":        ["M&M", "mahindra & mahindra", "mahindra and mahindra"],
    "BAJAJ-AUTO": ["bajaj auto"],
    "TATASTEEL":  ["tata steel"],
    "JSWSTEEL":   ["jsw steel"],
    "HINDALCO":   ["hindalco"],
    "VEDL":       ["vedanta"],
    "ONGC":       ["ongc"],
    "OIL":        ["OIL", "oil india"],
    "NTPC":       ["ntpc"],
    "POWERGRID":  ["power grid", "powergrid"],
    "COALINDIA":  ["coal india"],
    "ADANIENT":   ["adani enterprises"],
    "ADANIPORTS": ["adani ports"],
    "SUNPHARMA":  ["sun pharma"],
    "CIPLA":      ["cipla"],
    "DRREDDY":    ["dr reddy", "dr. reddy"],
    "LUPIN":      ["lupin"],
    "DIVISLAB":   ["divis lab", "divi's"],
    "BAJFINANCE": ["bajaj finance"],
    "BAJAJFINSV": ["bajaj finserv"],
    "HDFCLIFE":   ["hdfc life"],
    "SBILIFE":    ["sbi life"],
    "LICI":       ["LIC"],
    "ULTRACEMCO": ["ultratech"],
    "GRASIM":     ["grasim"],
    "ASIANPAINT": ["asian paints"],
    "TITAN":      ["titan"],
    "NESTLEIND":  ["nestle"],
    "HINDUNILVR": ["hindustan unilever", "HUL"],
    "BPCL":       ["BPCL", "bharat petroleum"],
    "IOC":        ["IOC", "indian oil"],
    "GAIL":       ["gail"],
    "HAL":        ["HAL", "hindustan aeronautics"],
    "BEL":        ["BEL", "bharat electronics"],
    "DLF":        ["DLF"],
    "TRENT":      ["trent"],
    "JIOFIN":     ["jio financial"],
    "PAYTM":      ["paytm", "one97"],
}

_ALIAS_RX = []                     # [(symbol, compiled_rx)] built once
for _sym, _aliases in FALLBACK_FNO.items():
    for _a in _aliases:
        if len(_a) <= 3:           # ambiguous: exact UPPERCASE token only
            _ALIAS_RX.append((_sym, _phrase_rx([_a.upper()], flags=0)))
        else:
            _ALIAS_RX.append((_sym, _phrase_rx([_a])))

# scrip-master symbols that are common English words → uppercase-only
_AMBIGUOUS_SYMBOLS = {"OIL", "IDEA", "GOLD", "CAMS", "CUB", "IEX", "MCX",
                      "ABB", "PAGE", "CREST", "PRIME", "SWAN", "MAX",
                      "STAR", "FOCUS", "SIGNAL", "TRENT"}
_TOKEN_RX = re.compile(r"[A-Za-z][A-Za-z0-9&\.\-']*")

# ── symbol universe (lazy; injectable for tests) ─────────────────────────
_UNIVERSE: set | None = None
_UNIVERSE_SOURCE: str = "unloaded"


def set_symbol_universe(symbols):
    """Inject the tradable-symbol universe (used by tests / callers that
    already hold a scrip master). Pass an iterable of symbol strings."""
    global _UNIVERSE, _UNIVERSE_SOURCE
    _UNIVERSE = {str(s).strip().upper() for s in symbols if str(s).strip()}
    _UNIVERSE_SOURCE = "injected"


def _load_universe():
    """Lazily load NSE EQ symbols via shoonya_client.load_scripmaster;
    any failure (offline, geo-block, missing dep) falls back to the
    built-in FALLBACK_FNO list — tagging always works."""
    global _UNIVERSE, _UNIVERSE_SOURCE
    if _UNIVERSE is not None:
        return
    try:
        if _REPO_ROOT not in sys.path:
            sys.path.insert(0, _REPO_ROOT)
        from shoonya_client import load_scripmaster
        table = load_scripmaster(
            "NSE", cache_dir=os.path.join(_REPO_ROOT, "data"))
        syms = set()
        for row in table.values():
            if (row.get("Instrument") or "").strip() == "EQ":
                s = (row.get("Symbol") or "").strip().upper()
                if s:
                    syms.add(s)
        if not syms:
            raise RuntimeError("scrip master had no EQ rows")
        _UNIVERSE = syms | set(FALLBACK_FNO)
        _UNIVERSE_SOURCE = "scripmaster"
    except Exception as exc:
        _UNIVERSE = set(FALLBACK_FNO)
        _UNIVERSE_SOURCE = f"fallback ({type(exc).__name__}: {exc})"


def universe_source() -> str:
    """Where the current symbol universe came from (health/debug info)."""
    return _UNIVERSE_SOURCE


# ── public API ───────────────────────────────────────────────────────────
def tag(title: str) -> dict:
    """Classify a headline. Returns
    {'sectors': [schema.SECTORS order], 'symbols': [sorted symbols]}."""
    if not title:
        return {"sectors": [], "symbols": []}
    _load_universe()

    sectors = [sec for sec in SECTORS if _SECTOR_RX[sec].search(title)]

    symbols = set()
    for sym, rx in _ALIAS_RX:                       # company-name aliases
        if rx.search(title):
            symbols.add(sym)
    for token in _TOKEN_RX.findall(title):          # exact symbol tokens
        token = token.strip("&.-'")
        if len(token) < 2:
            continue
        up = token.upper()
        if up not in _UNIVERSE:
            continue
        if (len(token) <= 3 or up in _AMBIGUOUS_SYMBOLS) and token != up:
            continue                                # ambiguous, needs CAPS
        symbols.add(up)
    return {"sectors": sectors, "symbols": sorted(symbols)}


# ── offline self-test (injected universe, no network) ────────────────────
if __name__ == "__main__":
    set_symbol_universe(list(FALLBACK_FNO) +
                        ["IRCTC", "ZOMATO", "OIL", "IDEA", "DIXON"])

    t = tag("HDFC Bank Q1 results beat estimates; RBI seen holding repo rate")
    assert t["sectors"] == ["BANKING"] and t["symbols"] == ["HDFCBANK"], t

    t = tag("Infosys, TCS lead IT stocks higher on strong deal wins")
    assert t["sectors"] == ["IT"], t
    assert set(t["symbols"]) == {"INFY", "TCS"}, t

    t = tag("Crude oil surges as OPEC trims output; ONGC, Oil India gain")
    assert t["sectors"] == ["ENERGY"], t
    assert "ONGC" in t["symbols"] and "OIL" in t["symbols"], t

    # ambiguous short symbols: exact UPPERCASE only
    assert "ITC" in tag("ITC posts record FMCG profit")["symbols"]
    assert "ITC" not in tag("itc hotels expansion on track")["symbols"]
    assert "OIL" not in tag("Sensex slips as oil prices climb")["symbols"]

    # alias phrases incl. '&'
    t = tag("L&T bags Rs 5,000-crore infrastructure order from NHAI")
    assert "INFRA" in t["sectors"] and t["symbols"] == ["LT"], t
    t = tag("M&M launches new SUV, auto sales jump 12%")
    assert "AUTO" in t["sectors"] and "M&M" in t["symbols"], t

    # multi-sector + symbol via injected (scrip-master-style) token
    t = tag("Tata Steel, Hindalco rally as metal prices rebound; "
            "Dixon shines")
    assert t["sectors"] == ["METAL"], t
    assert set(t["symbols"]) >= {"TATASTEEL", "HINDALCO", "DIXON"}, t

    # word-boundary sanity: no false fire inside larger words
    t = tag("Company faces bankruptcy filing after audit")
    assert t == {"sectors": [], "symbols": []}, t

    # every sector reachable
    probes = {"BANKING": "banking", "IT": "infosys", "PHARMA": "pharma",
              "AUTO": "automobile", "METAL": "steel", "ENERGY": "crude",
              "FMCG": "fmcg", "REALTY": "real estate", "INFRA": "infra",
              "FINANCE": "mutual fund", "TELECOM": "telecom",
              "CEMENT": "cement", "POWER": "electricity",
              "DEFENCE": "defence", "CHEMICALS": "chemicals"}
    for sec, word in probes.items():
        assert sec in tag(f"news about {word} today")["sectors"], sec

    assert tag("") == {"sectors": [], "symbols": []}
    print("sectors.py self-test OK (universe:", universe_source() + ")")
