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

def rank(results,queries,kind="item",min_bytes=None,max_bytes=None):
    qtokens=set().union(*(_tokens(q) for q in queries)) if queries else set()
    ranked=[]
    for r in results:
        if not r.get("url"): continue
        size=r.get("size") or 0
        if min_bytes and size<min_bytes: continue
        if max_bytes and size>max_bytes: continue
        rt=_tokens(r["title"])
        overlap=len(qtokens & rt)/max(1,len(qtokens))
        score=overlap*100
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
