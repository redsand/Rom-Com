"""The card index: what is on an SD card, and what an update would do to it.

A card is the one part of a library that leaves the building, so the index is the only
memory of what it held. These tests pin the parts that could hurt: an entry the library
cannot name must never be deleted, a game the owner marked keep must survive, and a
removal must be written down before it happens.
"""
import hashlib
import json

from romcom.db import connect


def _lib(tmp_path, monkeypatch):
    """A database with a couple of games in it."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    db = connect()
    return db


def _item(db, iid, title, system="nes", score=None, keep=0, path=None, payload=b"rom-bytes"):
    """One catalog item with one file in the library."""
    with db:
        db.execute("INSERT INTO items(id,title,system,community_score,keep) VALUES(?,?,?,?,?)",
                   (iid, title, system, score, keep))
        if path:
            db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,?,?)",
                       (path, len(payload), iid))
    return iid


def _card(tmp_path, entries):
    """A card with games on it: {folder: {entry: bytes}}."""
    card = tmp_path / "card"
    for folder, games in entries.items():
        for entry, size in games.items():
            p = card / "ROMs" / folder / entry
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)
    return card


def test_a_card_is_indexed_by_what_is_on_it(tmp_path, monkeypatch):
    """The index describes the card: one entry per game ES-DE would list."""
    db = _lib(tmp_path, monkeypatch)
    card = _card(tmp_path, {"nes": {"Game (USA).nes": 100}})
    from romcom import cardindex
    r = cardindex.index_card(card, db=db)
    assert r["entries"] == 1
    rows = db.execute("SELECT folder,entry,bytes FROM card_entries").fetchall()
    assert [(r["folder"], r["entry"], r["bytes"]) for r in rows] == [("nes", "Game (USA).nes", 100)]


def test_a_disc_folder_is_one_entry(tmp_path, monkeypatch):
    """A disc dumped as cue+bin is a FOLDER that ES-DE lists as one game. It is one entry,
    and it is measured whole."""
    db = _lib(tmp_path, monkeypatch)
    card = tmp_path / "card"
    d = card / "ROMs" / "psx" / "Game (USA).cue"
    d.mkdir(parents=True)
    (d / "Game (USA).cue").write_bytes(b"x" * 40)
    (d / "Game (USA) (Track 1).bin").write_bytes(b"x" * 60)
    from romcom import cardindex
    r = cardindex.index_card(card, db=db)
    assert r["entries"] == 1
    row = db.execute("SELECT entry,bytes FROM card_entries").fetchone()
    assert row["entry"] == "Game (USA).cue" and row["bytes"] == 100


def test_a_card_file_is_matched_back_to_its_item(tmp_path, monkeypatch):
    """A card file is known by its name, the way artwork's own walk knows it."""
    db = _lib(tmp_path, monkeypatch)
    lib = tmp_path / "lib"; lib.mkdir()
    src = lib / "Game (USA).nes"; src.write_bytes(b"rom-bytes")
    _item(db, "nes-game", "Game", "nes", path=str(src))
    card = _card(tmp_path, {"nes": {"Game (USA).nes": 100}})
    from romcom import cardindex
    cardindex.index_card(card, db=db)
    row = db.execute("SELECT item_id,tracked FROM card_entries").fetchone()
    assert row["item_id"] == "nes-game"


def test_content_the_library_cannot_name_is_never_touched(tmp_path, monkeypatch):
    """The owner's own file on the card is not the library's to delete.

    This is the safety property that matters most: an entry with no item is left alone
    whatever the plan says.
    """
    db = _lib(tmp_path, monkeypatch)
    lib = tmp_path / "lib"; lib.mkdir()
    src = lib / "Game (USA).nes"; src.write_bytes(b"rom-bytes")
    _item(db, "nes-game", "Game", "nes", score=90, path=str(src))
    card = _card(tmp_path, {"nes": {"Game (USA).nes": 100, "Some Other Game.nes": 50}})
    from romcom import cardindex
    cardindex.index_card(card, db=db)
    rows = db.execute("SELECT entry,item_id FROM card_entries ORDER BY entry").fetchall()
    by = {r["entry"]: r["item_id"] for r in rows}
    assert by["Some Other Game.nes"] is None       # untracked
    assert by["Game (USA).nes"] == "nes-game"


def test_a_manifest_makes_an_entry_tracked(tmp_path, monkeypatch):
    """An entry the export wrote is known exactly; one found by name is a guess."""
    db = _lib(tmp_path, monkeypatch)
    lib = tmp_path / "lib"; lib.mkdir()
    src = lib / "Game (USA).nes"; src.write_bytes(b"rom-bytes")
    _item(db, "nes-game", "Game", "nes", path=str(src))
    card = _card(tmp_path, {"nes": {"Game (USA).nes": 100}})
    from romcom import cardindex
    cardindex.write_manifest(card, {"card_id": "abc123", "entries": [
        {"folder": "nes", "entry": "Game (USA).nes", "item_id": "nes-game"}]})
    cardindex.index_card(card, db=db)
    row = db.execute("SELECT item_id,tracked FROM card_entries").fetchone()
    assert row["item_id"] == "nes-game" and row["tracked"] == 1


def test_the_card_is_recognised_at_another_drive_letter(tmp_path, monkeypatch):
    """The card carries its own identity, so a card moved to another letter is the same
    card — not a second one, whose entries would then be two cards' worth."""
    db = _lib(tmp_path, monkeypatch)
    a = _card(tmp_path / "a", {"nes": {"Game (USA).nes": 100}})
    b = tmp_path / "b" / "card"
    (b / "ROMs" / "nes").mkdir(parents=True)
    (b / "ROMs" / "nes" / "Game (USA).nes").write_bytes(b"x" * 100)
    from romcom import cardindex
    cardindex.write_manifest(a, {"card_id": "move-me", "entries": []})
    cardindex.write_manifest(b, {"card_id": "move-me", "entries": []})
    r1 = cardindex.index_card(a, db=db)
    r2 = cardindex.index_card(b, db=db)
    assert r1["card_id"] == r2["card_id"] == "move-me"
    assert db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


def test_a_card_carries_its_own_id_from_the_first_time_it_is_seen(tmp_path, monkeypatch):
    """The id has to travel with the card, or the card is only known by the drive letter
    it happened to be plugged into — and the same card in another reader is a new card
    whose games are all guesses."""
    db = _lib(tmp_path, monkeypatch)
    card = _card(tmp_path, {"nes": {"Game (USA).nes": 100}})
    from romcom import cardindex
    r = cardindex.index_card(card, db=db)
    manifest = json.loads((card / ".romcom-card.json").read_text(encoding="utf-8"))
    assert manifest["card_id"] == r["card_id"]
    # The same card, moved to a different reader.
    moved = tmp_path / "reader2" / "card"
    (moved / "ROMs" / "nes").mkdir(parents=True)
    (moved / "ROMs" / "nes" / "Game (USA).nes").write_bytes(b"x" * 100)
    (moved / ".romcom-card.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    r2 = cardindex.index_card(moved, db=db)
    assert r2["card_id"] == r["card_id"]
    assert db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


def test_a_card_that_states_its_identity_is_not_the_card_that_used_to_be_in_the_slot(
        tmp_path, monkeypatch):
    """A card that says what it is beats the reader's memory of what was in the slot.

    Otherwise a card whose database was rebuilt inherits the old card's index, and the
    two are told apart by nothing at all.
    """
    db = _lib(tmp_path, monkeypatch)
    slot = tmp_path / "slot"
    old = slot / "card"
    (old / "ROMs" / "nes").mkdir(parents=True)
    (old / "ROMs" / "nes" / "Old Game.nes").write_bytes(b"x" * 100)
    from romcom import cardindex
    cardindex.index_card(old, db=db)
    old_id = db.execute("SELECT id FROM cards").fetchone()["id"]

    # A different card turns up in the same slot, and carries its own id.
    new = slot / "card"
    (new / "ROMs" / "nes" / "Old Game.nes").unlink()
    (new / "ROMs" / "nes" / "New Game.nes").write_bytes(b"x" * 100)
    cardindex.write_manifest(new, {"card_id": "brand-new-card", "entries": []})
    r = cardindex.index_card(new, db=db)
    assert r["card_id"] == "brand-new-card" != old_id
    # Each card keeps its own index: the new card's game is filed under the new card,
    # and the old card's memory of itself is not rewritten by the card that replaced it.
    rows = db.execute("SELECT card_id,entry FROM card_entries").fetchall()
    assert {(x["card_id"], x["entry"]) for x in rows} == {
        ("brand-new-card", "New Game.nes"), (old_id, "Old Game.nes")}


def test_play_counts_are_read_back_off_the_card(tmp_path, monkeypatch):
    """ES-DE's own bookkeeping on the card is the only place the owner's play counts
    exist — the library's own play data is barely there at all."""
    db = _lib(tmp_path, monkeypatch)
    card = _card(tmp_path, {"nes": {"Game (USA).nes": 100}})
    gl = card / "ES-DE" / "gamelists" / "nes"
    gl.mkdir(parents=True)
    (gl / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game><path>./Game (USA).nes</path>'
        '<playcount>7</playcount><lastplayed>20261015T120000</lastplayed>'
        '<favorite>true</favorite></game></gameList>', encoding="utf-8")
    from romcom import cardindex
    cardindex.index_card(card, db=db)
    row = db.execute("SELECT playcount,last_played FROM card_entries").fetchone()
    assert row["playcount"] == 7 and row["last_played"] == "20261015T120000"


def test_the_index_describes_the_card_and_not_a_log(tmp_path, monkeypatch):
    """Something removed from the card by hand must not stay in the index: the index is a
    description of what is there."""
    db = _lib(tmp_path, monkeypatch)
    card = _card(tmp_path, {"nes": {"Game (USA).nes": 100}})
    from romcom import cardindex
    cardindex.index_card(card, db=db)
    (card / "ROMs" / "nes" / "Game (USA).nes").unlink()
    cardindex.index_card(card, db=db)
    assert db.execute("SELECT COUNT(*) FROM card_entries").fetchone()[0] == 0


# ------------------------------------------------------------------------------ the plan
def _exported(tmp_path, monkeypatch, db, items, keeps=(), on_card=None):
    """Put games on a card the way the export does, and index the result.

    `items`: [(id, title, score, filename, size)] — the library's side and the card's
    side at once, so the card holds exactly what the library would export.
    `keeps`: item ids the owner marked keep.
    `on_card`: the item ids to actually put on the card; the rest are in the library
    only, waiting to be exported (which is the newcomer an eviction makes room for).
    """
    lib = tmp_path / "lib"; lib.mkdir(exist_ok=True)
    for iid, title, score, name, size in items:
        src = lib / name; src.write_bytes(b"x" * size)
        _item(db, iid, title, "nes", score=score, path=str(src),
              keep=1 if iid in keeps else 0)
    card = tmp_path / "card"
    here = {i[0] for i in items} if on_card is None else set(on_card)
    for iid, title, score, name, size in items:
        if iid not in here:
            continue
        p = card / "ROMs" / "nes" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    from romcom import cardindex
    cardindex.write_manifest(card, {"card_id": "card-1", "entries": [
        {"folder": "nes", "entry": name, "item_id": iid, "score": score}
        for iid, title, score, name, size in items if iid in here]})
    cardindex.index_card(card, db=db)
    return card


def _games_for(db, iids):
    """The export's own plan for these items — the same list organize() would build."""
    from romcom.organizer import _plan_games, _selection
    rows = _selection()
    rows = [r for r in rows if r["item_id"] in set(iids)]
    return _plan_games(rows, "flat")


def test_a_game_that_fell_out_of_the_recipe_is_removed(tmp_path, monkeypatch):
    """The library no longer selects it, so it comes off the card."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-old", "Old Game", 90, "Old Game.nes", 100),
        ("nes-new", "New Game", 80, "New Game.nes", 100)])
    from romcom import cardindex
    # The desired state holds only the new game.
    games = _games_for(db, {"nes-new"})
    plan = cardindex.plan_update(card, games, db=db)
    gone = [r["entry"] for r in plan["remove"]]
    assert gone == ["Old Game.nes"]
    assert plan["remove"][0]["reason"] == "no longer selected"


def test_a_kept_game_is_never_evicted(tmp_path, monkeypatch):
    """'keep' means the owner gave that game a slot on the card. It is not up for
    eviction whatever the ranking says."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-keep", "Kept Game", 10, "Kept Game.nes", 100),
        ("nes-new", "New Game", 99, "New Game.nes", 100)],
        keeps=("nes-keep",), on_card=("nes-keep",))
    from romcom import cardindex
    # Both are still wanted — the recipe has not dropped the kept game — so the only
    # reason it could go is that a better game wants the room.
    games = _games_for(db, {"nes-keep", "nes-new"})
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=0, reserve_bytes=0)
    assert "Kept Game.nes" not in [r["entry"] for r in plan["remove"]]
    assert any(p.get("entry") == "Kept Game.nes" for p in plan["protected"])


def test_untracked_content_is_never_evicted(tmp_path, monkeypatch):
    """A file the library cannot name is the owner's own. It is left alone, and the plan
    says so."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-keep", "Kept Game", 10, "Kept Game.nes", 100)])
    # The owner's own file, and one the library cannot name.
    (card / "ROMs" / "nes" / "Notes.txt").write_bytes(b"mine")
    from romcom import cardindex
    cardindex.index_card(card, db=db)
    games = _games_for(db, {"nes-keep"})
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=0, reserve_bytes=0)
    assert "Notes.txt" not in [r["entry"] for r in plan["remove"]]
    assert "Notes.txt" in [u["entry"] for u in plan["untracked"]]


def test_a_played_game_is_protected(tmp_path, monkeypatch):
    """A game played on the handheld earns its place: it is not traded away for a better
    one."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-played", "Played Game", 10, "Played Game.nes", 100),
        ("nes-new", "New Game", 99, "New Game.nes", 100)],
        on_card=("nes-played",))
    gl = card / "ES-DE" / "gamelists" / "nes"
    gl.mkdir(parents=True, exist_ok=True)
    (gl / "gamelist.xml").write_text(
        '<gameList><game><path>./Played Game.nes</path><playcount>4</playcount>'
        '</game></gameList>', encoding="utf-8")
    from romcom import cardindex
    cardindex.index_card(card, db=db)
    games = _games_for(db, {"nes-played", "nes-new"})
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=0, reserve_bytes=0)
    assert "Played Game.nes" not in [r["entry"] for r in plan["remove"]]


def test_a_better_game_makes_room_by_a_worse_one(tmp_path, monkeypatch):
    """The point of the feature: a well-ranked newcomer takes the slot of a badly-ranked
    game, and the plan says which game displaced which."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-old", "Old Game", 10, "Old Game.nes", 1000),
        ("nes-new", "New Game", 99, "New Game.nes", 1000)],
        on_card=("nes-old",))
    from romcom import cardindex
    games = _games_for(db, {"nes-old", "nes-new"})
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=0, reserve_bytes=0)
    gone = [r for r in plan["remove"] if r["reason"] == "outranked"]
    assert [r["entry"] for r in gone] == ["Old Game.nes"]
    assert gone[0]["title"] == "Old Game"


def test_a_card_with_room_keeps_what_it_has(tmp_path, monkeypatch):
    """An update reshuffles nothing when nothing needs to move: a card that is not full
    keeps its games, or the card churns on every run."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-old", "Old Game", 10, "Old Game.nes", 1000),
        ("nes-new", "New Game", 99, "New Game.nes", 1000)],
        on_card=("nes-old",))
    from romcom import cardindex
    games = _games_for(db, {"nes-old", "nes-new"})
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=10 * 1024 ** 3, reserve_bytes=0)
    assert [r for r in plan["remove"] if r["reason"] == "outranked"] == []


def test_an_evicted_game_does_not_come_back_on_the_next_update(tmp_path, monkeypatch):
    """A card that traded a game away must not ask for it again.

    Eviction is a decision about what the card holds, so the game that was given up is
    gone from the card. If the next update put it back, the card would flap between two
    states forever: every run would rewrite the card for no reason, and the game the
    owner was shown as given up would keep coming back.
    """
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-old", "Old Game", 10, "Old Game.nes", 1000),
        ("nes-new", "New Game", 99, "New Game.nes", 1000)],
        on_card=("nes-old",))
    from romcom import cardindex
    games = _games_for(db, {"nes-old", "nes-new"})
    # The card is full: the newcomer must be traded for.
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=0, reserve_bytes=0)
    assert [r["entry"] for r in plan["remove"] if r["reason"] == "outranked"] == [
        "Old Game.nes"]
    # The trade happens for real, and the newcomer goes on in the room it made.
    cardindex.apply_removals(card, plan, db=db)
    (card / "ROMs" / "nes" / "New Game.nes").write_bytes(b"x" * 1000)
    cardindex.index_card(card, db=db)
    # The card is full again. The game that was given up must stay given up: it is
    # wanted, but it no longer fits, and it ranks below the game holding the slot.
    after = cardindex.plan_update(card, games, db=db, evict=True,
                                  free_bytes=0, reserve_bytes=0)
    assert after["remove"] == []
    assert after["add"] == []
    assert [a["entry"] for a in after["no_room"]] == ["Old Game.nes"]


def test_a_game_that_does_not_fit_is_left_off_rather_than_traded_for(tmp_path, monkeypatch):
    """A card full of better games is not made better by a worse one.

    When the newcomer cannot fit even after giving up every game it outranks, it is left
    off and reported — not forced onto the card, and not bought at the price of a game
    that ranked better.
    """
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-good", "Good Game", 90, "Good Game.nes", 1000),
        ("nes-new", "New Game", 50, "New Game.nes", 1000)],
        on_card=("nes-good",))
    from romcom import cardindex
    games = _games_for(db, {"nes-good", "nes-new"})
    # No room at all, and the newcomer ranks worse than what is on the card.
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=0, reserve_bytes=0)
    assert plan["remove"] == []
    assert plan["add"] == []
    assert [a["entry"] for a in plan["no_room"]] == ["New Game.nes"]


def test_the_space_a_removal_frees_counts_towards_the_newcomer(tmp_path, monkeypatch):
    """A game that fell out of the recipe frees its space for the one that replaces it.

    The plan has to work this out before anything is removed, or it would refuse a card
    that is in fact about to have room.
    """
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-old", "Old Game", 90, "Old Game.nes", 1000),
        ("nes-new", "New Game", 80, "New Game.nes", 1000)],
        on_card=("nes-old",))
    from romcom import cardindex
    # The library no longer wants the old game, and the newcomer is exactly its size.
    with db:
        db.execute("UPDATE items SET wanted=0 WHERE id='nes-old'")
    games = _games_for(db, {"nes-new"})
    plan = cardindex.plan_update(card, games, db=db, evict=True,
                                 free_bytes=0, reserve_bytes=0)
    assert [r["entry"] for r in plan["remove"]] == ["Old Game.nes"]
    assert [a["entry"] for a in plan["add"]] == ["New Game.nes"]


def test_an_arcade_set_is_not_read_as_no_longer_selected(tmp_path, monkeypatch):
    """Arcade is built, not copied, so it never appears in the export's file plan — but
    the sets are on the card all the same. An update that forgot that would read every
    arcade set as "no longer selected" and take the whole arcade library off the card."""
    db = _lib(tmp_path, monkeypatch)
    card = _card(tmp_path, {"arcade": {"pacman.zip": 500}})
    with db:
        db.execute("INSERT INTO items(id,title,system,community_score,wanted,external_id,"
                   "playable) VALUES('arc-pacman','Pac-Man','arcade',50,1,'mame/pacman',1)")
    from romcom import cardindex
    from romcom.organizer import _arcade_plan
    cardindex.index_card(card, db=db)
    plan = cardindex.plan_update(card, [], db=db, systems=["arcade"],
                                  arcade={"core_sets": {"mame": ["pacman"]}})
    assert plan["remove"] == []
    assert any(k["entry"] == "pacman.zip" for k in plan["keep"])


def test_a_set_the_recipe_no_longer_builds_comes_off_the_card(tmp_path, monkeypatch):
    """The other half of the same rule: a set the recipe has dropped does come off, so
    the arcade folder is not a shelf the update never touches."""
    db = _lib(tmp_path, monkeypatch)
    card = _card(tmp_path, {"arcade": {"pacman.zip": 500, "donkeykong.zip": 400}})
    with db:
        db.execute("INSERT INTO items(id,title,system,community_score,wanted,external_id,"
                   "playable) VALUES('arc-pacman','Pac-Man','arcade',50,1,'mame/pacman',1)")
        db.execute("INSERT INTO items(id,title,system,community_score,wanted,external_id,"
                   "playable) VALUES('arc-dk','Donkey Kong','arcade',40,1,'mame/donkeykong',1)")
    from romcom import cardindex
    cardindex.index_card(card, db=db)
    plan = cardindex.plan_update(card, [], db=db, systems=["arcade"],
                                  arcade={"core_sets": {"mame": ["pacman"]}})
    assert [r["entry"] for r in plan["remove"]] == ["donkeykong.zip"]
    assert plan["remove"][0]["reason"] == "no longer selected"


def test_the_manifest_is_written_before_anything_is_removed(tmp_path, monkeypatch):
    """The record precedes the deletion, so a removal is reversible by record."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-old", "Old Game", 90, "Old Game.nes", 100),
        ("nes-new", "New Game", 80, "New Game.nes", 100)])
    from romcom import cardindex
    games = _games_for(db, {"nes-new"})
    plan = cardindex.plan_update(card, games, db=db)
    seen = {}

    import romcom.cardindex as ci
    real = ci.write_removal_manifest

    def spy(plan, card=None, db=None):
        seen["manifest"] = real(plan, card=card, db=db)
        seen["file_there"] = (card / "ROMs" / "nes" / "Old Game.nes").exists()
        return seen["manifest"]

    ci.write_removal_manifest = spy
    try:
        r = cardindex.apply_removals(card, plan, db=db)
    finally:
        ci.write_removal_manifest = real
    assert seen["file_there"] is True           # the manifest went out first
    assert r["removed"] == 1
    assert not (card / "ROMs" / "nes" / "Old Game.nes").exists()
    m = json.loads(open(r["manifest"], encoding="utf-8").read())
    assert m["removed"][0]["entry"] == "Old Game.nes"
    assert m["removed"][0]["reason"] == "no longer selected"


def test_a_removal_leaves_an_event_row(tmp_path, monkeypatch):
    """Every removal is on the record in the events table, as every other removal in the
    app is."""
    db = _lib(tmp_path, monkeypatch)
    card = _exported(tmp_path, monkeypatch, db, [
        ("nes-old", "Old Game", 90, "Old Game.nes", 100),
        ("nes-new", "New Game", 80, "New Game.nes", 100)])
    from romcom import cardindex
    games = _games_for(db, {"nes-new"})
    plan = cardindex.plan_update(card, games, db=db)
    cardindex.apply_removals(card, plan, db=db)
    row = db.execute("SELECT item_id,event,detail FROM events WHERE event='card-removed'").fetchone()
    assert row["item_id"] == "nes-old"
    assert "Old Game" in row["detail"]
    # ...and the index no longer claims it is there.
    assert db.execute("SELECT COUNT(*) FROM card_entries WHERE entry='Old Game.nes'").fetchone()[0] == 0

