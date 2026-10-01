from romcom import exportplan
from romcom.db import connect
from romcom.web import create_app

G = 1024 ** 3


def test_best_fill_uses_the_most_space_not_the_most_platforms():
    # Greedy smallest-first would take a+b (6 GB) and stop; the fullest fit is c alone (9 GB).
    r = exportplan.best_fill({"a": 3 * G, "b": 3 * G, "c": 9 * G}, 10 * G)
    assert r["add"] == ["c"] and r["add_bytes"] == 9 * G


def test_best_fill_never_overshoots():
    r = exportplan.best_fill({"a": 5 * G + 1, "b": 5 * G}, 10 * G)
    assert r["add_bytes"] <= 10 * G and len(r["add"]) == 1


def test_top_up_fills_around_the_pick():
    """3DS is picked; 236 GB remain. gba+nds (240) is over, n64+nds (120) under-fills,
    gba alone (200) is the fullest fit."""
    sizes = {"3ds": 4 * G, "gba": 200 * G, "n64": 80 * G, "nds": 40 * G}
    r = exportplan.best_fill(sizes, 240 * G, forced=["3ds"])
    assert r["add"] == ["gba"]


def test_a_pick_bigger_than_the_card_has_no_top_up():
    r = exportplan.best_fill({"arcade": 300 * G, "nes": 1 * G}, 200 * G, forced=["arcade"])
    assert r["add"] == []


def _library(monkeypatch, tmp_path, systems):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    db = connect()
    with db:
        for system, files in systems.items():
            for n, (size, keep, rating) in enumerate(files):
                iid = f"{system}{n}"
                db.execute("INSERT INTO items(id,title,system,keep,rating) VALUES(?,?,?,?,?)",
                           (iid, iid, system, keep, rating))
                db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,?,?)",
                           (f"x:/{iid}", size, iid))
    return db


def test_plan_reports_usable_space_after_the_reserve(monkeypatch, tmp_path):
    _library(monkeypatch, tmp_path, {"3ds": [(4 * G, 0, None)]})
    cap = 256 * exportplan.GB
    p = exportplan.plan(capacity_bytes=cap, selected=["3ds"])
    assert p["usable_bytes"] == cap - cap // 100
    assert p["selected_bytes"] == 4 * G and p["selected_fits"]


def test_plan_points_an_oversized_platform_at_its_curated_subset(monkeypatch, tmp_path):
    """Arcade at 300 GB will not go on a 256 GB card; the kept / 7+ games will, and saying
    so is the useful answer."""
    _library(monkeypatch, tmp_path, {"arcade": [(290 * G, 0, None), (10 * G, 1, None), (2 * G, 0, 8)]})
    p = exportplan.plan(capacity_bytes=256 * exportplan.GB)
    [t] = p["too_big"]
    assert t["system"] == "arcade" and t["curated_bytes"] == 12 * G and t["curated_fits"]


def test_plan_honours_the_gates(monkeypatch, tmp_path):
    _library(monkeypatch, tmp_path, {"nes": [(1 * G, 1, None), (1 * G, 0, None)]})
    p = exportplan.plan(capacity_bytes=64 * exportplan.GB, gates={"keep_only": True})
    assert p["systems"] == [{"system": "nes", "files": 1, "bytes": 1 * G}]


def test_profiles_round_trip_and_are_cleaned(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    c = create_app().test_client()
    made = c.post("/api/export/profiles", json={
        "name": "Odin 2 – 3DS card", "path": "J:\\", "systems": ["3ds", "3ds", ""],
        "capacity_gb": "256", "gates": {"rating_min": 99}, "junk": 1}).get_json()
    assert made["systems"] == ["3ds"] and made["capacity_gb"] == 256.0
    assert made["gates"]["rating_min"] == 10 and "junk" not in made
    c.put(f"/api/export/profiles/{made['id']}", json={**made, "systems": ["3ds", "gba"]})
    [got] = c.get("/api/export/profiles").get_json()
    assert got["systems"] == ["3ds", "gba"]
    assert c.delete(f"/api/export/profiles/{made['id']}").status_code == 200
    assert c.delete(f"/api/export/profiles/{made['id']}").status_code == 404


def test_plan_endpoint_plans_a_card_by_size(monkeypatch, tmp_path):
    _library(monkeypatch, tmp_path, {"3ds": [(4 * G, 0, None)], "gba": [(200 * G, 0, None)]})
    c = create_app().test_client()
    d = c.post("/api/organize/plan", json={"capacity_gb": 256, "systems": ["3ds"]}).get_json()
    assert d["capacity_bytes"] == 256 * exportplan.GB
    assert d["top_up"]["add"] == ["gba"]
    assert d["best"]["add"] == ["3ds", "gba"]


def test_an_export_runs_with_the_gates_it_was_sent(monkeypatch, tmp_path):
    """The gates shown beside the Export button are the ones that run."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    seen = {}
    monkeypatch.setattr("romcom.web.organize", lambda dest, **kw: seen.update(kw) or {})
    c = create_app().test_client()
    c.post("/api/organize", json={"path": str(tmp_path / "sd"), "systems": ["3ds"],
                                  "gates": {"keep_only": True, "rating_min": 7}})
    import time
    for _ in range(40):
        if seen: break
        time.sleep(0.05)
    assert seen["keep_only"] is True and seen["rating_min"] == 7 and seen["wanted_only"] is False
