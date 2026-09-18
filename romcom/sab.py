import requests
from .config import settings

def _call(params):
    s=settings()
    if not s["sab_url"] or not s["sab_key"]:
        raise RuntimeError("SAB_URL/SAB_API_KEY are not configured")
    p=dict(params); p.update({"apikey":s["sab_key"],"output":"json"})
    r=requests.get(s["sab_url"],params=p,timeout=30); r.raise_for_status(); return r.json()

def add_url(url,name,priority=0):
    s=settings()
    return _call({"mode":"addurl","name":url,"nzbname":name,"cat":s["sab_category"],"priority":priority,"pp":3})

def queue(): return _call({"mode":"queue"}).get("queue",{}).get("slots",[])
def history(limit=500): return _call({"mode":"history","start":0,"limit":limit}).get("history",{}).get("slots",[])
