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


def _wanted(db):
    return {r[0] for r in db.execute("SELECT id FROM items WHERE wanted=1")}


def test_demos_and_editions_of_a_held_game_leave_the_queue(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    _item(db, "h", "nes", "River City Ransom (U)", held=True)
    _item(db, "e1", "nes", "River City Ransom (World) (Evercade)")
    _item(db, "e2", "nes", "River City Ransom (USA) (Virtual Console)")
    _item(db, "d1", "xbox", "Tekken 4 (USA) (Demo 1)")
    _item(db, "d2", "xbox", "Official Xbox Magazine Game Disc 14 (USA)")
    _item(db, "d3", "xbox", "Killzone (Europe) (En,Fr,De,Es,It) (Bonus Disc)")
    _item(db, "s1", "wii", "IOS17-64-v512")
    _item(db, "k1", "xbox", "Demolition Racer (USA)")
    cats = {x[0]: x[3] for x in dewant.candidates()}
    assert cats == {"e1": "have-it", "e2": "have-it", "d1": "demo", "d2": "print",
                    "d3": "demo", "s1": "junk"}


def test_a_held_copy_nobody_can_read_or_a_demo_answers_for_nothing(monkeypatch, tmp_path):
    """A German-only disc, a translated dump or a demo on the shelf is no reason to stop
    looking for the game."""
    db = _db(monkeypatch, tmp_path)
    _item(db, "h1", "ps2", "Disney's Dinosaur (Europe) (Fr,De,Es,It,Nl)", held=True)
    _item(db, "w1", "ps2", "Disney's Dinosaur (Europe) (En,Sv,No,Da,Fi)")
    _item(db, "h2", "ps2", "Gradius V (Japan) (Taikenban)", held=True)
    _item(db, "w2", "ps2", "Gradius V (USA)")
    _item(db, "h3", "nes", "Mega Man 2 (U) [T-German1.01]", held=True)
    _item(db, "w3", "nes", "Mega Man 2 (USA)")
    _item(db, "h4", "ps2", "Shenmue II (USA) (Disc 1)", held=True)
    _item(db, "w4", "ps2", "Shenmue II (USA) (Disc 2)")
    assert dewant.candidates() == []


def test_one_edition_of_an_unheld_game_stays_in_the_queue(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    _item(db, "a", "ps2", "Killzone (Europe) (En,Ru,El)")
    _item(db, "b", "ps2", "Killzone (USA)")
    _item(db, "c", "ps2", "Killzone (Japan)")
    _item(db, "d", "ps3", "Dead Space (Europe) (Es,It)")
    _item(db, "e", "ps3", "Dead Space (Europe) (En,Pl,Ru,Cs,Hu)")
    _item(db, "f", "switch", "DOOM")
    _item(db, "g", "switch", "DOOM (1993)")
    dewant.dewant(apply=True)
    assert _wanted(db) == {"b", "e", "f", "g"}


def test_a_game_wanted_again_is_left_alone_and_undo_puts_it_all_back(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    _item(db, "d1", "xbox", "Tekken 4 (USA) (Demo 1)")
    _item(db, "j1", "xbox", "Zan Kabuki (Japan)")
    assert dewant.dewant(apply=True)["count"] == 2
    with db:
        db.execute("UPDATE items SET wanted=1, authorized=1 WHERE id='d1'")
    assert dewant.dewant(apply=True)["count"] == 0 and _wanted(db) == {"d1"}
    assert dewant.undo("japan-only")["restored"] == 1 and _wanted(db) == {"d1", "j1"}
    assert dewant.dewant(apply=True)["count"] == 0
