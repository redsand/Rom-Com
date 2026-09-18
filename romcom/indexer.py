import xml.etree.ElementTree as ET
import requests
from .config import settings

NS={"newznab":"http://www.newznab.com/DTD/2010/feeds/attributes/"}

def search(query, limit=50):
    s=settings()
    if not s["nzb_url"] or not s["nzb_key"]:
        raise RuntimeError("NZB_API_URL/NZB_API_KEY are not configured")
    r=requests.get(s["nzb_url"],params={"t":"search","apikey":s["nzb_key"],"q":query,"limit":limit,"extended":1,"o":"xml"},timeout=30)
    r.raise_for_status(); root=ET.fromstring(r.content); out=[]
    for item in root.findall(".//item"):
        title=(item.findtext("title") or "Unknown").strip(); enc=item.find("enclosure"); url=None; size=0
        if enc is not None:
            url=enc.attrib.get("url"); size=int(enc.attrib.get("length",0) or 0)
        for a in item.findall("newznab:attr",NS):
            if a.attrib.get("name")=="size":
                try: size=int(a.attrib.get("value",0))
                except ValueError: pass
        out.append({"title":title,"url":url,"size":size})
    return out
