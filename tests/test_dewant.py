"""dewant: the categories nobody wants leave the acquisition backlog."""
from romcom.db import connect
from romcom import dewant


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    return connect()


def _item(db, iid, system, title, wanted=1, status="CATALOGED", held=False):
    with db:
        db.execute("INSERT INTO items(id,title,system,status,wanted,authorized) "
                   "VALUES(?,?,?,?,?,?)", (iid, title, system, status, wanted, 1))
        if held:
            db.execute("INSERT INTO files(path,bytes,matched_item_id,content) "
                       "VALUES(?,?,?,1)", (f"H:/{iid}.rom", 9, iid))


def test_tutors_crosswords_and_japan_only_leave_the_backlog(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    _item(db, "t1", "nds", "PONS Franzoesisch - Vokabel Buddy (Germany)")
    _item(db, "t2", "nds", "Sueddeutsche Zeitung Wissen - Mehr Kreuzwortraetsel (Germany)")
    _item(db, "t3", "nds", "Ore ga Omae o Mamoru (Japan)")
    _item(db, "t4", "nds", "4471_New-9FA55393_to_Old-BD105752")
    _item(db, "t5", "nds", "[Wi-Fi] Jump Super Stars - Panel Unlocker (J)")
    _item(db, "k1", "nds", "Mario Kart DS (USA)")
    _item(db, "k2", "nds", "Virtua Fighter (Japan, USA) (En)")
    _item(db, "k3", "nds", "Math Blaster (USA)")
    r = dewant.dewant()
    gone = {x[0] for x in dewant.candidates()}
    assert gone == {"t1", "t2", "t3", "t4", "t5"}
    assert r["by_category"]["japan-only"] == 2 and r["by_category"]["print"] >= 1
    assert r["applied"] is False and db.execute(
        "SELECT wanted FROM items WHERE id='t1'").fetchone()[0] == 1

    dewant.dewant(apply=True)
    assert db.execute("SELECT wanted FROM items WHERE id='t3'").fetchone()[0] == 0
    assert db.execute("SELECT authorized FROM items WHERE id='t3'").fetchone()[0] == 0
    assert db.execute("SELECT wanted FROM items WHERE id='k1'").fetchone()[0] == 1


def test_a_held_game_never_leaves_and_arcade_is_untouched(monkeypatch, tmp_path):
    """`wanted` on a game the library holds is a fact about the owner, not the queue;
    and arcade's Japan-only games are half the point of the set."""
    db = _db(monkeypatch, tmp_path)
    _item(db, "h1", "ps2", "Zettai Zetsumei Toshi (Japan)", held=True)
    _item(db, "a1", "arcade", "Dodonpachi (Japan)", held=False)
    r = dewant.dewant(apply=True)
    assert r["count"] == 0
    assert db.execute("SELECT wanted FROM items WHERE id='h1'").fetchone()[0] == 1
    assert db.execute("SELECT wanted FROM items WHERE id='a1'").fetchone()[0] == 1

def test_a_numbered_title_is_a_game_not_bookkeeping(monkeypatch, tmp_path):
    """"007: James Bond" and "1942 (Jpn, Alt)" are games; "4471_New-9FA55393…" is a
    DAT's bookkeeping row. The number alone never makes the call — the payload does."""
    db = _db(monkeypatch, tmp_path)
    _item(db, "g1", "nes", "1942 (Jpn, Alt)")
    _item(db, "g2", "dos", "007: James Bond - The Stealth Affair")
    _item(db, "g3", "nes", "007 - The Living Daylights (Euro)")
    _item(db, "j1", "nds", "4471_Old-BD105752_to_New-9FA55393")
    gone = {x[0] for x in dewant.candidates()}
    assert gone == {"j1"}
