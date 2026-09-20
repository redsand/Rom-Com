from romcom.db import connect
from romcom.catalog_status import catalog_status

def test_catalog_status_returns_targets(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"db.sqlite"))
    connect()
    rows=catalog_status()
    assert any(r["source"]=="nointro" and r["system"]=="snes" for r in rows)


def test_coverage_accounts_for_every_item_in_the_library(tmp_path, monkeypatch):
    """The view is a coverage report, so anything it omits is a blind spot. It used to walk
    `catalogs:` alone: 205,907 items across 20 systems -- arcade, dos, nds, vita, ps3 -- were
    absent with nothing to indicate it. Pin the invariant rather than the row count: every
    item in the database is represented by exactly one row."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "cov.db"))
    # load_yaml resolves against the repo root, not the cwd, so the config is injected rather
    # than written to tmp_path -- otherwise this silently tests the real catalogs.yaml.
    import romcom.catalog_status as cs
    monkeypatch.setattr(cs, "load_yaml", lambda name: {
        "catalogs": {"nointro": {"enabled": True, "systems": ["nes", "psp"]},
                     "off": {"enabled": False, "systems": ["saturn"]}},
        "custom": {"systems": ["nds", "neverimported"]}})
    from romcom.db import connect
    catalog_status = cs.catalog_status
    db = connect()
    rows_in = [("nointro", "nes"),        # declared, expected source
               ("retroplay", "nes"),      # declared system, DIFFERENT source -> extra-source
               ("nointro", "psp"),        # declared under this source
               ("local", "nds"),          # the custom block
               ("redump", "ps3")]         # mentioned nowhere -> undeclared
    with db:
        for i, (src, sysname) in enumerate(rows_in):
            db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                       " VALUES(?,?,?,'CATALOGED',?,?)",
                       (f"i{i}", f"T{i}", sysname, src, f"{sysname}/T{i}"))

    rows = catalog_status()
    assert sum(r["count"] for r in rows) == 5, rows      # nothing lost, nothing double-counted
    got = {(r["source"], r["system"]): r["declared"] for r in rows if r["count"]}
    assert got == {("nointro", "nes"): "catalogs", ("nointro", "psp"): "catalogs",
                   ("retroplay", "nes"): "extra-source", ("local", "nds"): "custom",
                   ("redump", "ps3"): "undeclared"}
    # A custom system nobody has imported still shows, as an unmet target rather than silence.
    assert {"source": "custom", "system": "neverimported", "count": 0,
            "loaded": False, "declared": "custom"} in rows
