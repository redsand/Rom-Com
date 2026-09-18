import gzip, hashlib, io, re, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
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

def _parse_xml(data):
    if isinstance(data,str): data=data.encode()
    try:
        root=ET.fromstring(data)
    except ET.ParseError:
        # Some dats in the wild are truncated after the last entry; recover by closing the root element.
        m=re.match(rb"\s*(?:<\?[^>]*\?>)?\s*(?:<!DOCTYPE[^>]*>)?\s*<(\w+)",data)
        if not m: raise
        root=ET.fromstring(data+b"</"+m.group(1)+b">")
    h=root.find("header")
    header={c.tag.lower():(c.text or "").strip() for c in h} if h is not None else {}
    games=[]
    for g in list(root.findall(".//game"))+list(root.findall(".//machine")):
        name=(g.get("name") or "").strip()
        if not name: continue
        hashes=[]
        for node in list(g.findall("rom"))+list(g.findall("disk")):
            for alg in ("crc","md5","sha1"):
                digest=(node.get(alg) or "").lower().strip()
                if digest: hashes.append((alg,digest))
        year=(g.findtext("year") or "").strip()
        games.append({"external":name,"title":(g.findtext("description") or name).strip(),
                      "year":int(year) if year.isdigit() else None,"sourcefile":None,"hashes":hashes})
    return header,games

# clrmamepro / DOSCenter: paren-delimited blocks of key-value pairs.
def _cm_tokens(text):
    i=0; n=len(text)
    while i<n:
        c=text[i]
        if c in " \t\r\n": i+=1; continue
        if c in "()": yield c; i+=1; continue
        if c=='"':
            j=text.find('"',i+1)
            if j<0: j=n
            yield text[i+1:j]; i=j+1; continue
        j=i
        while j<n and text[j] not in ' \t\r\n()"': j+=1
        yield text[i:j]; i=j

def _cm_nest(tokens):
    stack=[[]]
    for t in tokens:
        if t=="(": stack.append([])
        elif t==")":
            if len(stack)>1:
                b=stack.pop(); stack[-1].append(b)
        else: stack[-1].append(t)
    while len(stack)>1:
        b=stack.pop(); stack[-1].append(b)
    return stack[0]

_HEX=re.compile(r"^[0-9a-fA-F]{8,40}$")

def _cm_block(content):
    pairs={}; blocks=[]
    i=0
    while i<len(content):
        t=content[i]
        if isinstance(t,list): blocks.append(("",t)); i+=1; continue
        nxt=content[i+1] if i+1<len(content) else None
        if isinstance(nxt,list): blocks.append((t.lower(),nxt)); i+=2
        elif nxt is not None: pairs.setdefault(t.lower().rstrip(":"),nxt); i+=2
        else: i+=1
    return pairs,blocks

def _cm_hashes(content):
    # Unquoted values may contain spaces (DOSCenter dates), so scan for hash keys rather than pairing positionally.
    toks=[t for t in content if isinstance(t,str)]
    out=[]
    for j,t in enumerate(toks[:-1]):
        k=t.lower()
        if k in ("crc","md5","sha1") and _HEX.match(toks[j+1]):
            out.append((k,toks[j+1].lower()))
    return out

def _parse_cm(text):
    nested=_cm_nest(_cm_tokens(text))
    header={}; games=[]
    i=0
    while i<len(nested):
        t=nested[i]; nxt=nested[i+1] if i+1<len(nested) else None
        if isinstance(t,str) and isinstance(nxt,list):
            kind=t.lower(); pairs,blocks=_cm_block(nxt)
            if kind in ("clrmamepro","doscenter") and not header:
                header=pairs
            elif kind in ("game","machine"):
                name=(pairs.get("name") or "").strip()
                if name:
                    hashes=[]
                    for bname,bcontent in blocks:
                        if bname in ("rom","disk","file"): hashes+=_cm_hashes(bcontent)
                    year=(pairs.get("year") or "").strip()
                    games.append({"external":name,"title":(pairs.get("description") or name).strip(),
                                  "year":int(year) if year.isdigit() else None,
                                  "sourcefile":(pairs.get("sourcefile") or "").strip() or None,"hashes":hashes})
            i+=2
        else: i+=1
    return header,games

def parse_dat(data):
    if isinstance(data,str): data=data.encode()
    data=data.lstrip(b"\xef\xbb\xbf").lstrip()
    if data.startswith(b"<"): return _parse_xml(data)
    return _parse_cm(data.decode("utf-8",errors="replace"))

def _import_games(db,games,system,source,wanted):
    count=0; hash_count=0
    with db:
        for g in games:
            external=g["external"]; key=f"{system}/{external}"
            row=db.execute("SELECT id FROM items WHERE catalog_source=? AND external_id=?",(source,key)).fetchone()
            if row:
                item_id=row["id"]
                db.execute("UPDATE items SET title=?,system=?,year=COALESCE(?,year),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (g["title"],system,g["year"],item_id))
            else:
                item_id=_id(source,system,external)
                db.execute("""INSERT INTO items(id,title,system,year,wanted,status,catalog_source,external_id)
                  VALUES(?,?,?,?,?,'CATALOGED',?,?)""",
                  (item_id,g["title"],system,g["year"],int(bool(wanted)),source,key))
            db.execute("INSERT OR IGNORE INTO aliases(item_id,alias) VALUES(?,?)",(item_id,external))
            for alg,digest in g["hashes"]:
                if db.execute("INSERT OR IGNORE INTO file_hashes(item_id,algorithm,digest) VALUES(?,?,?)",(item_id,alg,digest)).rowcount:
                    hash_count+=1
            count+=1
    return {"items":count,"hashes":hash_count}

def import_dat(path,system,source="dat",wanted=True):
    db=connect()
    with _open_xml(path) as fh:
        data=fh.read()
    _,games=parse_dat(data)
    return _import_games(db,games,system,source,wanted)

# First matching rule wins; a None slug means "recognized but unsupported — skip".
# Skip rules must precede the generic cousin they'd otherwise fall into (wii u before wii, etc).
_SYSTEM_RULES=[
 (r"\bbios\b",None),(r"gameshark",None),
 (r"game boy advance","gba"),(r"game boy color","gbc"),(r"game boy","gb"),
 (r"super nintendo|super famicom|\bsnes\b","snes"),
 (r"famicom disk|family computer disk",None),
 (r"nintendo entertainment system|\bfamicom\b|family computer","nes"),
 (r"virtual boy","virtualboy"),
 (r"nintendo 64dd",None),(r"nintendo 64","n64"),
 (r"gamecube","gamecube"),
 (r"wii u",None),(r"\bwii\b","wii"),
 (r"\b3ds\b","3ds"),(r"nintendo dsi",None),(r"nintendo ds|\bnds\b","nds"),
 (r"playstation vita|ps vita","vita"),
 (r"playstation portable|\bpsp\b","psp"),
 (r"playstation [345]",None),
 (r"playstation 2|\bps2\b","ps2"),
 (r"playstation|\bps1\b|\bpsx\b","ps1"),
 (r"sega cd|mega cd","segacd"),(r"\b32x\b","32x"),
 (r"mega drive|genesis","genesis"),
 (r"master system|mark iii","mastersystem"),(r"game gear","gamegear"),
 (r"saturn","saturn"),(r"dreamcast","dreamcast"),
 (r"pc engine cd|turbografx cd","pcenginecd"),(r"pc engine|turbografx","pcengine"),
 (r"pc fx",None),(r"pc 98","pc98"),(r"pc 88","pc88"),
 (r"atari 2600","atari2600"),(r"atari 5200","atari5200"),(r"atari 7800","atari7800"),
 (r"jaguar",None),(r"lynx","lynx"),(r"atari st\b","atarist"),
 (r"neo ?geo pocket color","neogeopocketcolor"),(r"neo ?geo pocket","neogeopocket"),
 (r"neo ?geo cd","neogeocd"),
 (r"wonderswan color","wonderswancolor"),(r"wonderswan","wonderswan"),
 (r"\b3do\b","3do"),
 (r"amiga cd32|\bcd32\b","amigacd32"),(r"amiga cdtv|\bcdtv\b",None),(r"amiga","amiga"),
 (r"\bcd ?i\b","cdi"),
 (r"commodore 64|\bc64\b","c64"),
 (r"apple iigs|apple 2gs","apple2gs"),(r"apple ii|apple 2\b","apple2"),
 (r"amstrad cpc","amstradcpc"),
 (r"msx ?2","msx2"),(r"\bmsx\b","msx"),
 (r"x68000","x68000"),(r"fm towns","fmtowns"),
 (r"scummvm","scummvm"),
 (r"\bdos\b","dos"),(r"\bwindows\b","windows"),
 (r"translation","translations"),(r"rom ?hack|\bhacks?\b","romhacks"),
]
_RULES=[(re.compile(p),slug) for p,slug in _SYSTEM_RULES]

def _norm_sys(s):
    return re.sub(r"\s+"," ",re.sub(r"[-_]+"," ",s.lower())).strip()

def detect_system(candidates):
    """Try each candidate name (most specific first); return the system slug or None."""
    for cand in candidates:
        if not cand: continue
        n=_norm_sys(cand)
        for rx,slug in _RULES:
            if rx.search(n): return slug
    return None

def detect_source(header,path):
    text=" ".join([header.get(k,"") for k in ("author","homepage","url","name","comment")]
                  +[path.name,path.parent.name]).lower()
    if "redump" in text: return "redump"
    if "no-intro" in text or "no intro" in text: return "nointro"
    if "scummvm" in text: return "scummvm"
    author=(header.get("author") or "").strip()
    return _slug(author) if author else "dat"

_DAT_EXTS={".dat",".xml",".zip",".gz"}

def _iter_dat_payloads(f):
    """Yield (dat_name, bytes) for each catalog file inside f — zip members, gz contents, or the file itself."""
    suf=f.suffix.lower()
    if suf==".zip":
        with zipfile.ZipFile(f) as z:
            for n in z.namelist():
                if n.lower().endswith((".dat",".xml")):
                    yield Path(n).name,z.read(n)
    elif suf==".gz":
        with gzip.open(f,"rb") as fh:
            yield f.stem,fh.read()
    else:
        yield f.name,f.read_bytes()

def _merge_scummvm_hashes(db,games):
    """Attach verification hashes from a ScummVM engine dat to existing scummvm catalog items."""
    matched=unmatched=hash_count=0
    with db:
        for g in games:
            cands=[g["external"]]
            if g.get("sourcefile"): cands.insert(0,f"{g['sourcefile']}:{g['external']}")
            row=None
            for c in cands:
                row=db.execute("SELECT id FROM items WHERE catalog_source='scummvm' AND external_id=?",(c,)).fetchone()
                if row: break
            if not row: unmatched+=1; continue
            matched+=1
            db.execute("INSERT OR IGNORE INTO aliases(item_id,alias) VALUES(?,?)",(row["id"],g["title"]))
            for alg,digest in g["hashes"]:
                if db.execute("INSERT OR IGNORE INTO file_hashes(item_id,algorithm,digest) VALUES(?,?,?)",(row["id"],alg,digest)).rowcount:
                    hash_count+=1
    return {"matched":matched,"unmatched":unmatched,"hashes":hash_count}

def import_dats(path,system=None,source=None,wanted=False,progress=None):
    """Import every dat/xml/zip/gz under a directory (or a single file), auto-detecting system and source.

    ScummVM engine dats merge hashes into existing scummvm items instead of creating new ones.
    New items default to wanted=0 so bulk reference catalogs don't flood the missing list.
    """
    root=Path(path)
    if root.is_dir():
        files=sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _DAT_EXTS)
    elif root.is_file():
        files=[root]
    else:
        raise FileNotFoundError(f"no such file or directory: {path}")
    report={"files":len(files),"dats":0,"items":0,"hashes":0,"scummvm_matched":0,
            "imported":[],"skipped":[],"errors":[]}
    db=connect()
    for i,f in enumerate(files):
        if progress: progress(i,len(files),f.name)
        try:
            payloads=list(_iter_dat_payloads(f))
        except Exception as e:
            report["errors"].append({"file":f.name,"error":str(e)}); continue
        if not payloads:
            report["skipped"].append({"file":f.name,"dat":"","reason":"no .dat/.xml inside archive"}); continue
        for dat_name,data in payloads:
            report["dats"]+=1
            entry={"file":f.name,"dat":dat_name}
            try:
                header,games=parse_dat(data)
            except Exception as e:
                report["errors"].append(entry|{"error":str(e)}); continue
            if not games:
                report["skipped"].append(entry|{"reason":"no game entries"}); continue
            sys_slug=system or detect_system([header.get("name"),header.get("description"),dat_name,f.name,f.parent.name])
            if not sys_slug:
                report["skipped"].append(entry|{"reason":"unrecognized system","header":header.get("name") or dat_name}); continue
            if sys_slug=="scummvm" and not system:
                r=_merge_scummvm_hashes(db,games)
                report["scummvm_matched"]+=r["matched"]; report["hashes"]+=r["hashes"]
                report["imported"].append(entry|{"system":"scummvm","source":"scummvm","items":0}|r)
            else:
                src=source or detect_source(header,f)
                r=_import_games(db,games,sys_slug,src,wanted)
                report["items"]+=r["items"]; report["hashes"]+=r["hashes"]
                report["imported"].append(entry|{"system":sys_slug,"source":src}|r)
    if progress: progress(len(files),len(files),"done")
    return report

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
