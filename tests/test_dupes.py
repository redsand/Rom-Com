"""One game fetched more than once is several files with different names and the same
bytes; the library holds them all and nothing else removed them."""
from pathlib import Path

from romcom.db import connect
from romcom.dupes import audit, cleanup, same_item_groups


def setup(monkeypatch, tmp_path, items, files):
    """items: (id, system, source, wanted, status); files: (path, bytes, sha1, item,
    match_method). Every file is also written to disk, because cleanup unlinks them."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "d.db"))
    monkeypatch.chdir(tmp_path)  # the manifest lands in backups/ relative to here
    roms = tmp_path / "roms"; roms.mkdir()
    db = connect()
    with db:
        for iid, system, src, wanted, status in items:
            db.execute("""INSERT INTO items(id,title,system,wanted,status,catalog_source)
                          VALUES(?,?,?,?,?,?)""", (iid, iid, system, wanted, status, src))
        for path, size, sha1, item, method in files:
            p = roms / path
            p.write_bytes(b"x" * size)
            db.execute("""INSERT INTO files(path,bytes,sha1,matched_item_id,match_method)
                          VALUES(?,?,?,?,?)""",
                       (str(p), size, sha1, item, method))
    return db, roms


def test_same_item_dupes_are_ranked_and_reported(monkeypatch, tmp_path):
    """The hash-verified copy is the one that stays; the report says what would go."""
    setup(monkeypatch, tmp_path,
          [("cars", "nes", "nointro", 1, "VERIFIED")],
          [("guess.zip", 9, "aa", "cars", "filename-exact"),
           ("proof.zip", 9, "aa", "cars", "hash")])
    [g] = same_item_groups()
    assert g[0]["path"].endswith("proof.zip") and len(g) == 2
    r = audit()
    assert r["duplicate_copies"] == 1 and r["reclaimable_bytes"] == 9


def test_dry_run_changes_nothing(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path,
          [("cars", "nes", "nointro", 1, "VERIFIED")],
          [("a.zip", 9, "aa", "cars", None), ("b.zip", 9, "aa", "cars", None)])
    r = cleanup()  # apply defaults off
    assert r["duplicate_copies"] == 1 and r["deleted"] == 0
    assert connect().execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 2


def test_apply_deletes_losers_rows_and_writes_a_manifest(monkeypatch, tmp_path):
    db, roms = setup(monkeypatch, tmp_path,
                     [("cars", "nes", "nointro", 1, "VERIFIED")],
                     [("keep.zip", 9, "aa", "cars", "hash"),
                      ("dupe.zip", 9, "aa", "cars", None)])
    r = cleanup(apply=True)
    assert r["deleted"] == 1 and r["bytes_freed"] == 9 and not r["errors"]
    assert not (roms / "dupe.zip").exists() and (roms / "keep.zip").exists()
    assert db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 1
    ev = db.execute("SELECT event,detail FROM events").fetchone()
    assert ev["event"] == "dupe-removed" and ev["detail"].endswith("keep.zip")
    manifests = list(Path("backups").glob("dupes-removed-*.json"))
    assert len(manifests) == 1


def test_a_local_copy_of_a_catalog_entry_is_removed(monkeypatch, tmp_path):
    """The adopted `local` row exists to represent a file; when a catalog entry holds the
    identical bytes, the copy and the adoption both go."""
    db, roms = setup(monkeypatch, tmp_path,
                     [("cat-1", "nes", "nointro", 1, "VERIFIED"),
                      ("local-nes-aa", "nes", "local", 1, "FOUND")],
                     [("cat.zip", 9, "aa", "cat-1", "hash"),
                      ("loose.zip", 9, "aa", "local-nes-aa", "adopted")])
    r = cleanup(apply=True)
    assert r["deleted"] == 1 and r["local_items_pruned"] == 1
    assert (roms / "cat.zip").exists() and not (roms / "loose.zip").exists()
    assert db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 1


def test_two_catalog_entries_sharing_content_are_report_only(monkeypatch, tmp_path):
    """Two catalog rows describe different entries (a region variant, say) and the file
    matches one of them; deleting it would silently un-match the other's claim."""
    db, roms = setup(monkeypatch, tmp_path,
                     [("usa", "nes", "nointro", 1, "VERIFIED"),
                      ("ger", "nes", "nointro", 1, "FOUND")],
                     [("usa.zip", 9, "aa", "usa", "hash"),
                      ("ger.zip", 9, "aa", "ger", None)])
    r = cleanup(apply=True)
    assert r["deleted"] == 0 and r["cross_item_kept"] == 1
    assert (roms / "usa.zip").exists() and (roms / "ger.zip").exists()


def test_a_wanted_local_copy_beats_an_unwanted_catalog_copy(monkeypatch, tmp_path):
    """The owner's verdict outranks provenance: a wanted adoption is not deleted in favor
    of a file no verdict backs."""
    db, roms = setup(monkeypatch, tmp_path,
                     [("cat-1", "nes", "nointro", 0, "CATALOGED"),
                      ("local-nes-aa", "nes", "local", 1, "FOUND")],
                     [("cat.zip", 9, "aa", "cat-1", None),
                      ("loose.zip", 9, "aa", "local-nes-aa", None)])
    r = cleanup(apply=True)
    assert r["deleted"] == 0
    assert (roms / "loose.zip").exists() and (roms / "cat.zip").exists()


def test_an_excluded_local_copy_is_left_alone(monkeypatch, tmp_path):
    """Exclusion is a deliberate decision; deleting the row would let the next scan
    re-adopt the same file."""
    db, roms = setup(monkeypatch, tmp_path,
                     [("cat-1", "nes", "nointro", 1, "VERIFIED"),
                      ("local-nes-aa", "nes", "local", 1, "EXCLUDED")],
                     [("cat.zip", 9, "aa", "cat-1", "hash"),
                      ("loose.zip", 9, "aa", "local-nes-aa", "adopted")])
    assert cleanup(apply=True)["deleted"] == 0
    assert (roms / "loose.zip").exists()


def test_distinct_content_under_one_item_is_not_a_dupe(monkeypatch, tmp_path):
    """A .cue and its .bin share an item but not bytes: both stay."""
    setup(monkeypatch, tmp_path,
          [("psx", "psx", "redump", 1, "VERIFIED")],
          [("Game.cue", 9, "bb", "psx", None), ("Game.bin", 9, "cc", "psx", None)])
    assert audit()["duplicate_copies"] == 0


def test_hashless_and_junk_files_are_never_dupes(monkeypatch, tmp_path):
    """Identity must be provable: no strong hash, no deletion. Artwork (content=0) is not
    part of the library's game bytes at all."""
    db, roms = setup(monkeypatch, tmp_path,
                    [("cars", "nes", "nointro", 1, "VERIFIED")],
                    [("a.zip", 9, "aa", "cars", None), ("b.zip", 9, "aa", "cars", None)])
    with db:
        db.execute("UPDATE files SET sha1=NULL")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) "
                   "VALUES(?,9,'cars',0)", (str(roms / "junk.png"),))
    assert audit()["duplicate_copies"] == 0


def test_the_system_filter_limits_the_cleanup(monkeypatch, tmp_path):
    db, roms = setup(monkeypatch, tmp_path,
                     [("nes-1", "nes", "nointro", 1, "VERIFIED"),
                      ("snes-1", "snes", "nointro", 1, "VERIFIED")],
                     [("nes.zip", 9, "aa", "nes-1", None),
                      ("nes2.zip", 9, "aa", "nes-1", None),
                      ("snes.zip", 9, "bb", "snes-1", None),
                      ("snes2.zip", 9, "bb", "snes-1", None)])
    r = cleanup(apply=True, systems=["snes"])
    assert r["deleted"] == 1
    assert not (roms / "snes2.zip").exists() and (roms / "nes2.zip").exists()


def test_the_web_reports_dupes_without_deleting(monkeypatch, tmp_path):
    """The Import tab shows the waste; removal stays a CLI act on the owner's archive."""
    from romcom.web import create_app
    db, roms = setup(monkeypatch, tmp_path,
                     [("cars", "nes", "nointro", 1, "VERIFIED")],
                     [("a.zip", 9, "aa", "cars", None), ("b.zip", 9, "aa", "cars", None)])
    d = create_app().test_client().get("/api/dupes").get_json()
    assert d["duplicate_copies"] == 1 and d["reclaimable_bytes"] == 9
    assert (roms / "b.zip").exists()
    assert db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 2