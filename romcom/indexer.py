import re, xml.etree.ElementTree as ET
import requests
from .config import settings

NS={"newznab":"http://www.newznab.com/DTD/2010/feeds/attributes/"}

def ping():
    s=settings()
    if not s["nzb_url"] or not s["nzb_key"]:
        raise RuntimeError("NZB_API_URL/NZB_API_KEY are not configured")
    r=requests.get(
        s["nzb_url"],
        params={"t":"search","apikey":s["nzb_key"],"q":"romcom-healthcheck","limit":1,"o":"xml"},
        timeout=30,
    )
    r.raise_for_status()
    # Newznab-style auth failures may be XML <error> documents returned with HTTP 200.
    root=ET.fromstring(r.content)
    if root.tag.lower().endswith("error"):
        raise RuntimeError(root.attrib.get("description") or "indexer API returned an error")
    err=root.find(".//error")
    if err is not None:
        raise RuntimeError(err.attrib.get("description") or "indexer API returned an error")
    return True

def search(query,limit=50):
    s=settings()
    if not s["nzb_url"] or not s["nzb_key"]:
        raise RuntimeError("NZB_API_URL/NZB_API_KEY are not configured")
    from . import searchcache
    return searchcache.cached("nzb", f"{query}|{limit}", lambda: _search_live(query, limit))

def _search_live(query,limit=50):
    s=settings()
    r=requests.get(s["nzb_url"],params={"t":"search","apikey":s["nzb_key"],"q":query,"limit":limit,"extended":1,"o":"xml"},timeout=30)
    r.raise_for_status(); root=ET.fromstring(r.content); out=[]
    for item in root.findall(".//item"):
        title=(item.findtext("title") or "Unknown").strip(); enc=item.find("enclosure"); url=None; size=0
        if enc is not None:
            url=enc.attrib.get("url"); size=int(enc.attrib.get("length",0) or 0)
        attrs={}
        for a in item.findall("newznab:attr",NS):
            attrs[a.attrib.get("name","")]=a.attrib.get("value","")
        try: size=int(attrs.get("size",size) or size)
        except ValueError: pass
        out.append({"title":title,"url":url,"size":size,"attrs":attrs})
    return out

def _tokens(s):
    return set(re.findall(r"[a-z0-9]+",s.lower()))

# Parentheticals that describe *which release of the same game* rather than *which game*:
# a site search chokes on the parentheses themselves, and the words don't help it find
# anything the bare title wouldn't. Status tags — (Demo), (Proto), (Beta), (Unl),
# (Pirate), (Aftermarket) — are deliberately NOT in this list: they name a different
# artifact, so dropping them would search for the retail ROM and then hand a Demo item
# that file, marked DOWNLOADED.
_REGIONS = ("usa|europe|japan|asia|world|korea|china|taiwan|hong kong|brazil|australia|"
            "canada|france|germany|italy|spain|netherlands|belgium|sweden|norway|denmark|"
            "finland|russia|poland|portugal|mexico|argentina|india|singapore|ireland|"
            "switzerland|austria|uk")
_LANGS = ("en|ja|jp|fr|de|es|it|nl|pt|sv|no|da|fi|ru|ko|zh|pl|cs|el|tr|hu|ar|he|ca|eu|gl|ro|nb")
_NOISE_PARENS = re.compile(
    r"\s*[(\[]\s*(?:"
    rf"(?:{_REGIONS})(?:\s*,\s*(?:{_REGIONS}))*"
    rf"|(?:{_LANGS})(?:\s*,\s*(?:{_LANGS}))*"
    r"|rev\s*[a-z0-9]+|v\d+(?:\.\d+)*|\d{4}(?:-\d{2}-\d{2})?"
    r")\s*[)\]]",
    re.I)
_BRACKETS = re.compile(r"[()\[\]]")


def clean_query(query):
    """The title reduced to what the *site search* can actually match on.

    Region/language/revision groups are dropped outright. Groups that survive (a
    multicart's board code, a "(Demo)" status) are unwrapped to bare words rather than
    deleted, because deleting them collapses an item to the generic prefix that every
    neighbouring page also matches: searching item "4-in-1 (SN 406) (Asia) (En)
    (Pirate)" as plain "4-in-1" is how three different items each downloaded the same
    Dragon Ball Z multicart (see rank() for the other half of that fix).
    """
    q = _NOISE_PARENS.sub(" ", query)
    q = _BRACKETS.sub(" ", q)
    return re.sub(r"\s+", " ", q).strip() or query


_YEAR = re.compile(r"(?:19|20)\d{2}")

# Unambiguous markers of a TV/movie/video release — never a ROM. NZB indexers are full of
# these (a game's name appears in an episode/film title: "Playdate.S04E21.Nancy.Drew…",
# "The.Protos.Experiment.2025.1080p.WEBRip"), and they carry the full title so recall is
# high enough to slip past the score floor. A title with any of these is refused outright.
_VIDEO = re.compile(
    r"\b(?:s\d{1,2}e\d{1,3}|\d{3,4}p|web-?rip|web-?dl|blu-?ray|bd-?rip|dvd-?rip|hd-?rip"
    r"|hdtv|x26[45]|h\.?26[45]|hevc|xvid|divx|aac|ac3|dts|ddp?\d(?:\.\d)?)\b", re.I)


def is_video_release(title):
    """True if the title looks like a TV/movie/video rip rather than a ROM."""
    return bool(_VIDEO.search(title or ""))


def _required(qtokens):
    """Query tokens a title must contain to be that title at all.

    A number is the whole identity of a multicart — "16-in-1" and "4 in 1" are
    different products sharing every other word — so digit-bearing tokens are
    mandatory rather than merely scoreable. Years are exempt: "1997 Super HIK
    16-in-1" is routinely titled "Super HIK 16-in-1" on a site, and requiring the
    leading year would refuse the very page we want.
    """
    return {t for t in qtokens if any(c.isdigit() for c in t) and not _YEAR.fullmatch(t)}


def rank(results,queries,kind="item",min_bytes=None,max_bytes=None):
    """Score results against the query. Both halves of the match are required.

    `recall` is how much of the query a title covers; `precision` is how much of the
    title the query accounts for. Multiplying them (rather than scoring recall alone)
    is what rejects the near-misses a recall-only score waves through: against query
    "4 in 1 (SN 406) (Asia) (En) (Pirate)" the unrelated "Dragon Ball Z 4 in 1" hits
    3 of 8 query tokens for a recall of 37.5 — above MIN_SCORE — but those 3 are only
    half of its own 6 tokens, so it scores 18.8 and is refused. Measured against the
    real ledger of bad direct fetches, recall-only accepted 23 of 23 wrong files and
    recall*precision accepts 12; adding _required() leaves 7, all of them plausible
    matches of the number they were asked for.
    """
    qtokens=set().union(*(_tokens(q) for q in queries)) if queries else set()
    required=_required(qtokens)
    cleans=[set(_tokens(clean_query(q))) for q in (queries or [])]
    ranked=[]
    for r in results:
        if not r.get("url"): continue
        if is_video_release(r.get("title")): continue  # TV/movie rip, never a ROM
        size=r.get("size") or 0
        if min_bytes and size<min_bytes: continue
        if max_bytes and size>max_bytes: continue
        rt=_tokens(r["title"])
        if required and not required <= rt: continue
        hit=len(qtokens & rt)
        recall=hit/max(1,len(qtokens))
        precision=hit/max(1,len(rt))
        score=recall*precision*100
        # A candidate whose tokens exactly equal the cleaned query's IS the item
        # asked for. Region/language words in the query can never appear in a bare
        # site title ("Draglade (Europe) (En,Fr,De,Es,It)" vs the page "draglade"),
        # so raw scoring buries the exact page below MIN_SCORE and it is refused
        # while the item re-cycles forever. The boost is exact: a near-miss with
        # extra or missing tokens keeps its raw score, floor and all.
        if any(c and rt == c for c in cleans):
            score=max(score,100.0)
        if kind=="volume":
            score+=sum(12 for w in ("collection","complete","archive","volume","pack","set") if w in rt)
        ranked.append(dict(r,score=score))
    return sorted(ranked,key=lambda x:(x["score"],x.get("size",0)),reverse=True)

# The biggest a cartridge dump of each system gets, with room to spare (zipped or not). A
# website result past it is not that game: a 28 MB "Black Dragon" for the NES was a PC
# adventure. Disc systems have no useful ceiling and are left out.
CART_MAX_BYTES = {
    "nes": 4 << 20, "fds": 4 << 20, "snes": 12 << 20, "gb": 4 << 20, "gbc": 8 << 20,
    "gba": 64 << 20, "n64": 96 << 20, "genesis": 12 << 20, "megadrive": 12 << 20,
    "mastersystem": 2 << 20, "gamegear": 2 << 20, "sg1000": 1 << 20, "32x": 8 << 20,
    "atari2600": 1 << 20, "atari5200": 1 << 20, "atari7800": 1 << 20, "lynx": 2 << 20,
    "pcengine": 4 << 20, "ngp": 8 << 20, "neogeopocket": 8 << 20,
    "neogeopocketcolor": 8 << 20, "wonderswan": 16 << 20, "wonderswancolor": 16 << 20,
    "virtualboy": 4 << 20, "colecovision": 1 << 20, "intellivision": 1 << 20,
    "nds": 512 << 20,
}
_FILLER = frozenset("the a an of and in on to for de la le les no wa version edition game".split())


def _plain(s):
    """Lowercase words with apostrophes and dots closed up: "Gilligan's" -> gilligans,
    "G.I. Joe" -> gi joe — sites spell titles without them."""
    return re.findall(r"[a-z0-9]+", re.sub(r"['’.]", "", (s or "").lower()))


def plausible(title, system, cand_title, size=0):
    """Can this website result be the item at all? The ranker counts shared words, and
    'Age of Darkness (World) (Demo 3)' shares three with 'World Of Warcraft 3.85' — so
    the name's own words (before its brackets) must mostly be in the result (a third may
    differ: romanization, a site's cut-off title), or the two must read as one spelling;
    and the file must fit the system's cartridge size."""
    cap = CART_MAX_BYTES.get((system or "").lower())
    if cap and size and size > cap:
        return False
    main = re.split(r"[(\[]", title or "", maxsplit=1)[0]
    words = [w for w in _plain(main) if w not in _FILLER]
    if not words:
        return True
    cand = _plain(re.sub(r"\.[a-z0-9]{2,4}$", "", (cand_title or "").lower()))
    while len(cand) > 1 and cand[0].isdigit() and len(cand[0]) >= 3 and cand[0] not in words:
        cand = cand[1:]                       # a site's catalogue number ("0547 ...")
    have = set(cand)
    if len([w for w in words if w not in have]) <= len(words) // 3:
        return True
    a, b = "".join(words), "".join(w for w in cand if w not in _FILLER)
    if a in b:
        return True                           # 'Mega Man' found as 'megaman'
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def search_entity(db,entity_type,entity_id,limit=50):
    if entity_type=="item":
        e=db.execute("SELECT * FROM items WHERE id=?",(entity_id,)).fetchone()
        if not e: raise KeyError(entity_id)
        if not e["authorized"]: raise PermissionError("item is not marked authorized")
        queries=[e["title"]]+[r["alias"] for r in db.execute("SELECT alias FROM aliases WHERE item_id=?",(entity_id,))]
        return rank(_merge(queries,limit),queries,"item"),e
    e=db.execute("SELECT * FROM volumes WHERE id=?",(entity_id,)).fetchone()
    if not e: raise KeyError(entity_id)
    if not e["authorized"]: raise PermissionError("volume is not marked authorized")
    queries=[r["query"] for r in db.execute("SELECT query FROM volume_search WHERE volume_id=?",(entity_id,))]
    if not queries: queries=[e["title"]]
    return rank(_merge(queries,limit),queries,"volume",e["min_bytes"],e["max_bytes"]),e

def _merge(queries,limit):
    seen={}; 
    for q in queries:
        for r in search(q,limit):
            key=r.get("url") or r["title"]
            seen[key]=r
    return list(seen.values())
