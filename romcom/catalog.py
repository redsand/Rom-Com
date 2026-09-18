import gzip, hashlib, io, re, zipfile
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from .db import connect

def _slug(s):
    x=re.sub(r"[^a-z0-9]+","-",s.lower()).strip("-")
    return x[:80] or hashlib.sha1(s.encode()).hexdigest()[:12]

def _id(source,system,external):
    return f"{_slug(source)}-{_slug(system)}-{hashlib.sha1(external.encode()).hexdigest()[:12]}"

def _open_xml(path):
    p=str(path)
    if p.lower().endswith(".gz"):
        return gzip.open(path,"rb")
    if p.lower().endswith(".zip"):
        z=zipfile.ZipFile(path)
        names=[n for n in z.namelist() if n.lower().endswith((".dat",".xml"))]
        if not names: raise ValueError("zip contains no .dat/.xml file")
        return io.BytesIO(z.read(names[0]))
    return open(path,"rb")

def import_dat(path,system,source="dat",wanted=True):
    db=connect(); count=0; hash_count=0
    with _open_xml(path) as fh:
        tree=ET.parse(fh)
    root=tree.getroot()
    nodes=list(root.findall(".//game"))+list(root.findall(".//machine"))
    with db:
        for g in nodes:
            external=(g.get("name") or "").strip()
            if not external: continue
            title=(g.findtext("description") or external).strip()
            year=(g.findtext("year") or "").strip()
            year=int(year) if year.isdigit() else None
            item_id=_id(source,system,external)
            db.execute("""INSERT INTO items(id,title,system,year,wanted,status,catalog_source,external_id)
              VALUES(?,?,?,?,?,'CATALOGED',?,?)
              ON CONFLICT(catalog_source,external_id) DO UPDATE SET title=excluded.title,
              system=excluded.system,year=COALESCE(excluded.year,items.year)""",
              (item_id,title,system,year,int(bool(wanted)),source,external))
            row=db.execute("SELECT id FROM items WHERE catalog_source=? AND external_id=?",(source,external)).fetchone()
            item_id=row["id"]
            db.execute("INSERT OR IGNORE INTO aliases(item_id,alias) VALUES(?,?)",(item_id,external))
            for node in list(g.findall("rom"))+list(g.findall("disk")):
                for alg in ("crc","md5","sha1"):
                    digest=(node.get(alg) or "").lower().strip()
                    if digest:
                        db.execute("INSERT OR IGNORE INTO file_hashes(item_id,algorithm,digest) VALUES(?,?,?)",(item_id,alg,digest))
                        hash_count+=1
            count+=1
    return {"items":count,"hashes":hash_count}

def import_scummvm(url="https://www.scummvm.org/compatibility",wanted=True):
    r=requests.get(url,timeout=30,headers={"User-Agent":"Rom-Com/0.2"})
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    db=connect(); count=0
    with db:
        for tr in soup.select("tr"):
            tds=tr.find_all("td")
            if len(tds)<3: continue
            title=tds[0].get_text(" ",strip=True)
            scumm_id=tds[1].get_text(" ",strip=True)
            support=tds[2].get_text(" ",strip=True)
            if not title or not scumm_id or support.lower()=="support level": continue
            item_id=_id("scummvm","scummvm",scumm_id)
            db.execute("""INSERT INTO items(id,title,system,wanted,status,preferred_runtime,catalog_source,external_id,support_level)
              VALUES(?,?,?,?,'CATALOGED','scummvm','scummvm',?,?)
              ON CONFLICT(catalog_source,external_id) DO UPDATE SET title=excluded.title,support_level=excluded.support_level,
              preferred_runtime='scummvm',system='scummvm'""",
              (item_id,title,"scummvm",int(bool(wanted)),scumm_id,support))
            row=db.execute("SELECT id FROM items WHERE catalog_source='scummvm' AND external_id=?",(scumm_id,)).fetchone()
            db.execute("INSERT OR IGNORE INTO aliases(item_id,alias) VALUES(?,?)",(row["id"],title))
            count+=1
    return count
