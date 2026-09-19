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
        if kind=="volume":
            score+=sum(12 for w in ("collection","complete","archive","volume","pack","set") if w in rt)
        ranked.append(dict(r,score=score))
    return sorted(ranked,key=lambda x:(x["score"],x.get("size",0)),reverse=True)

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
