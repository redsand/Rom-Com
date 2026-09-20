"""The tool registry. Every tool is exercised against a real temp database, because the
whole value of this assistant is that its answers are grounded in these queries — a tool
that returns a plausible-but-wrong shape is worse than one that errors."""
import json

from romcom import chattools, chatstore
from romcom.db import connect


def setup_db(monkeypatch, tmp_path, items=None, files=None, volumes=None, covers=None):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    db = connect()
    with db:
        for r in items or []:
            db.execute("INSERT INTO items(id,title,system,series,series_number,authorized,wanted,status,"
                       "source) VALUES(?,?,?,?,?,?,?,?,?)",
                       (r["id"], r.get("title", r["id"]), r.get("system"), r.get("series"),
                        r.get("series_number"), r.get("authorized", 1), r.get("wanted", 1),
                        r.get("status", "CATALOGED"), r.get("source")))
        for r in files or []:
            db.execute("INSERT INTO files(path,bytes,crc32,matched_item_id,match_method,content)"
                       " VALUES(?,?,?,?,?,?)",
                       (r["path"], r.get("bytes", 1024), r.get("crc32"), r.get("matched_item_id"),
                        r.get("match_method"), r.get("content", 1)))
        for v in volumes or []:
            db.execute("INSERT INTO volumes(id,title,authorized,status,min_bytes,max_bytes)"
                       " VALUES(?,?,?,?,?,?)",
                       (v["id"], v.get("title", v["id"]), v.get("authorized", 1),
                        v.get("status", "CATALOGED"), v.get("min_bytes"), v.get("max_bytes")))
        for iid, vid in covers or []:
            db.execute("INSERT INTO volume_covers(volume_id,item_id) VALUES(?,?)", (vid, iid))
    return db


def reg(ctx=None):
    return chattools.build_registry(ctx)


def call(r, name, **args):
    return chattools.dispatch(r, name, args)


NES = "Nintendo Entertainment System"


# ------------------------------------------------------------------------------- reads

def test_library_summary_reports_totals_and_breakdowns(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[
        {"id": "a", "system": NES, "status": "CATALOGED"},
        {"id": "b", "system": NES, "status": "VERIFIED"},
        {"id": "c", "system": "Super Nintendo", "status": "MISSING"},
    ], files=[{"path": "x.zip", "matched_item_id": "b", "match_method": "hash"}])
    out = call(reg(), "library_summary")
    assert out["ok"] is True
    d = out["data"]
    assert d["cataloged"] == 3 and d["on_disk"] == 1
    assert d["by_status"]["CATALOGED"] == 1
    assert {s["system"] for s in d["by_system"]} == {NES, "Super Nintendo"}


def test_facets_gives_exact_system_spellings(monkeypatch, tmp_path):
    """The model must be able to look up the exact string rather than guess — a guessed
    system name silently returns zero rows, which reads as 'you own nothing'."""
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "system": NES}, {"id": "b", "system": "Atari 2600"}])
    d = call(reg(), "facets")["data"]
    assert NES in d["systems"] and "Atari 2600" in d["systems"]
    assert "MISSING" in d["statuses"] and "VERIFIED" in d["statuses"]


def test_list_items_filters_pages_and_always_reports_total(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[
        {"id": f"n{i}", "title": f"Game {i}", "system": NES, "status": "CATALOGED"} for i in range(120)
    ])
    out = call(reg(), "list_items", system=NES, view="all", limit=50)
    d = out["data"]
    assert d["total"] == 120 and d["returned"] == 50      # total is the honest count
    page2 = call(reg(), "list_items", system=NES, view="all", limit=50, offset=50)["data"]
    assert page2["returned"] == 50
    assert {i["id"] for i in d["items"]} & {i["id"] for i in page2["items"]} == set()


def test_list_items_view_missing_matches_the_ui_definition(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[
        {"id": "a", "status": "CATALOGED", "wanted": 1},
        {"id": "b", "status": "VERIFIED", "wanted": 1},
        {"id": "c", "status": "DOWNLOADED", "wanted": 1},   # in hand, not missing
        {"id": "d", "status": "CATALOGED", "wanted": 0},    # not wanted
    ])
    d = call(reg(), "list_items", view="missing")["data"]
    assert [i["id"] for i in d["items"]] == ["a"] and d["total"] == 1


def test_list_items_search_matches_title_series_and_id(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[
        {"id": "smb", "title": "Super Mario Bros.", "series": "Mario", "system": NES},
        {"id": "zelda", "title": "The Legend of Zelda", "system": NES},
    ])
    assert call(reg(), "list_items", q="mario")["data"]["total"] == 1
    assert call(reg(), "list_items", q="zelda")["data"]["total"] == 1
    assert call(reg(), "list_items", series="Mario")["data"]["total"] == 1


def test_list_items_explains_a_wrong_system_rather_than_returning_a_bare_zero(monkeypatch, tmp_path):
    """Pins the flail: asked for `Nintendo DS` against a library that stores `nds`, the agent
    got `{"total": 0}` — indistinguishable from "you own nothing" — and spent its entire step
    budget re-guessing. The hint turns a miss into one correction."""
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "system": "nds"}, {"id": "b", "system": "nes"}])
    d = call(reg(), "list_items", system="Nintendo DS")["data"]
    assert d["total"] == 0
    assert d["hint"]["system"]["asked_for"] == "Nintendo DS"
    assert d["hint"]["system"]["did_you_mean"] == ["nds"]      # not "nes": the substring test
    assert "Retry" in d["hint"]["note"]


def test_list_items_suggests_a_status_correction(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED"}])
    d = call(reg(), "list_items", status="verified")["data"]   # the legal value is uppercase
    assert d["total"] == 0
    assert d["hint"]["status"]["did_you_mean"] == ["VERIFIED"]


def test_list_items_says_when_an_empty_result_is_genuinely_empty(monkeypatch, tmp_path):
    """The other half of the distinction. Legal filters that happen to match nothing must not
    read as a spelling problem, or the agent "fixes" a name that was already right."""
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "system": "nds", "status": "VERIFIED"}])
    d = call(reg(), "list_items", system="nds", status="MISSING")["data"]
    assert d["total"] == 0
    assert "system" not in d["hint"] and "status" not in d["hint"]
    assert "real empty result" in d["hint"]["note"]


def test_list_items_omits_the_hint_entirely_when_there_are_rows(monkeypatch, tmp_path):
    """No hint on the happy path: it would be noise in every result the agent actually uses."""
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "system": "nds"}])
    d = call(reg(), "list_items", system="nds")["data"]
    assert d["total"] == 1 and "hint" not in d


def test_get_item_returns_files_aliases_and_events(monkeypatch, tmp_path):
    db = setup_db(monkeypatch, tmp_path,
                  items=[{"id": "smb", "title": "Super Mario Bros.", "system": NES, "status": "VERIFIED"}],
                  files=[{"path": "/roms/smb.zip", "matched_item_id": "smb", "match_method": "hash",
                          "crc32": "1234"}])
    with db:
        db.execute("INSERT INTO aliases(item_id,alias) VALUES('smb','Mario Bros')")
        db.execute("INSERT INTO events(item_id,event,detail) VALUES('smb','scan-match','hash: /roms/smb.zip')")
    d = call(reg(), "get_item", ident="smb")["data"]
    assert d["kind"] == "item" and d["entity"]["status"] == "VERIFIED"
    assert d["aliases"] == ["Mario Bros"]
    assert d["files"][0]["match_method"] == "hash"
    assert d["events"][0]["event"] == "scan-match"


def test_get_item_accepts_a_unique_title_and_refuses_an_ambiguous_one(monkeypatch, tmp_path):
    """An id is awkward to quote in chat, so a title lookup is allowed — but only when it is
    unambiguous. Guessing between two Mario games is how a wrong item gets acted on."""
    setup_db(monkeypatch, tmp_path, items=[
        {"id": "smb", "title": "Super Mario Bros."},
        {"id": "smb2", "title": "Super Mario Bros. 2"},
    ])
    assert call(reg(), "get_item", ident="Super Mario Bros.")["data"]["entity"]["id"] == "smb"
    amb = call(reg(), "get_item", ident="Super Mario")["data"]
    assert amb["ambiguous"] is True and len(amb["matches"]) == 2


def test_get_item_on_a_volume_returns_covers(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
             volumes=[{"id": "v1", "title": "Big Bundle"}], covers=[("a", "v1"), ("b", "v1")])
    d = call(reg(), "get_item", ident="v1")["data"]
    assert d["kind"] == "volume" and {c["id"] for c in d["covers"]} == {"a", "b"}


def test_get_item_errors_helpfully_on_a_miss(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    out = call(reg(), "get_item", ident="nope")
    assert out["ok"] is False and "no item or volume matches" in out["data"]["error"]


# ------------------------------------------------------------------- the audit tool

def test_library_audit_finds_the_false_downloaded_population(monkeypatch, tmp_path):
    """The recurring "why is this DOWNLOADED with no file?" question. This is exactly the
    class of problem the assistant exists to explain, and task #21 exists to fix."""
    setup_db(monkeypatch, tmp_path, items=[
        {"id": "real", "title": "Really Here", "system": NES, "status": "VERIFIED"},
        {"id": "ghost", "title": "Never Arrived", "system": NES, "status": "DOWNLOADED"},
        {"id": "ghost2", "title": "Also Gone", "system": "Atari 2600", "status": "FOUND"},
    ], files=[{"path": "/roms/real.zip", "matched_item_id": "real", "match_method": "hash"}])
    d = call(reg(), "library_audit")["data"]
    assert d["in_hand_with_no_matching_file"] == 2
    assert {r["id"] for r in d["sample"]} == {"ghost", "ghost2"}
    assert dict((r["system"], r["missing"]) for r in d["by_system"]) == {NES: 1, "Atari 2600": 1}
    assert d["claimed_in_hand_by_status"]["DOWNLOADED"] == 1
    assert "reconcile" in d["explanation"] or "reset them to MISSING" in d["explanation"]


def test_library_audit_separates_hash_attribution_from_filename_only(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[
        {"id": "h", "status": "VERIFIED"}, {"id": "f", "status": "VERIFIED"},
        {"id": "g", "status": "VERIFIED"},
    ], files=[
        {"path": "/a.zip", "matched_item_id": "h", "match_method": "hash"},
        {"path": "/b.zip", "matched_item_id": "f", "match_method": "filename-exact"},
        {"path": "/c.zip", "matched_item_id": "g", "match_method": "adopted"},
    ])
    d = call(reg(), "library_audit")["data"]
    attribution = {r["match_method"]: r["files"] for r in d["file_attribution"]}
    assert attribution["hash"] == 1 and attribution["filename-exact"] == 1
    # 'g' is claimed VERIFIED but only ever matched by name — a contradiction worth surfacing
    assert d["satisfied_without_a_hash_match"] == 2


def test_library_audit_is_clean_on_a_consistent_library(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED"}],
             files=[{"path": "/a.zip", "matched_item_id": "a", "match_method": "hash"}])
    d = call(reg(), "library_audit")["data"]
    assert d["in_hand_with_no_matching_file"] == 0 and d["satisfied_without_a_hash_match"] == 0


# -------------------------------------------------------------------- network tool

def test_search_title_uses_the_indexer_and_reports_permission_clearly(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "smb", "title": "Super Mario Bros.", "authorized": 1}])
    from romcom import indexer
    monkeypatch.setattr(indexer, "search_entity",
                        lambda db, kind, ident: ([{"title": "SMB (USA).zip", "size": 40, "url": "u"}],
                                                 db.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()))
    d = call(reg(), "search_title", ident="smb")["data"]
    assert d["kind"] == "item" and d["result_count"] == 1
    assert d["results"][0]["title"] == "SMB (USA).zip"


def test_search_title_explains_an_unauthorized_item(monkeypatch, tmp_path):
    """Authorization is Rom-Com's human-control seam. The assistant must report it as a
    condition to fix, not as a mysterious failure."""
    setup_db(monkeypatch, tmp_path, items=[{"id": "smb", "title": "SMB", "authorized": 0}])
    from romcom import indexer

    def boom(db, kind, ident):
        raise PermissionError("item is not marked authorized")
    monkeypatch.setattr(indexer, "search_entity", boom)
    out = call(reg(), "search_title", ident="smb")
    assert out["ok"] is False
    assert "authorized" in out["data"]["error"] and "authorized" in out["data"]["hint"]


def test_search_title_is_marked_medium_risk(monkeypatch, tmp_path):
    """Read-only, so it is never gated — but it hits the network and is rate-limited."""
    assert reg()["search_title"].risk == "medium"
    assert reg()["list_items"].risk == "low"


# ---------------------------------------------------------------- ctx-driven tools

def test_job_status_and_watcher_health_come_from_the_injected_context(monkeypatch, tmp_path):
    """Tools reach closure state through ctx rather than importing web.py, so this is
    testable with no app at all."""
    setup_db(monkeypatch, tmp_path)
    ctx = chattools.Ctx(
        job_status=lambda kind: {"running": True, "done": 3, "total": 9, "current": "a.zip",
                                 "last_beat": __import__("time").monotonic() - 2},
        jobs_active=lambda: {"scan": {"done": 3, "total": 9, "current": "a.zip"}})
    d = call(reg(ctx), "job_status")["data"]
    assert d["active"]["scan"]["done"] == 3 and d["jobs"]["scan"]["running"] is True
    h = call(reg(ctx), "watcher_health")["data"]
    assert h["running"] is True and h["state"] == "off"     # watch toggle is off by default
    assert 0 < h["last_beat_secs"] < 30


def test_watcher_health_reports_stale_via_the_ctx(monkeypatch, tmp_path):
    import time
    setup_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_ACQUIRE_WATCH", "true")
    from romcom.config import invalidate
    invalidate()
    ctx = chattools.Ctx(job_status=lambda kind: {"running": True, "last_beat": time.monotonic() - 5000})
    h = call(reg(ctx), "watcher_health")["data"]
    assert h["watch_on"] is True and h["state"] == "stale" and h["stale"] is True


def test_doctor_reports_checks(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    names = {c["name"] for c in call(reg(), "doctor")["data"]["checks"]}
    assert "database" in names and "nzb-config" in names


# ------------------------------------------------------------------- result shaping

def test_every_tool_schema_is_valid_for_ollama():
    """The schema is the model's only description of what it can do — a malformed one means
    the tool is invisible in practice."""
    for name, t in reg().items():
        s = t.schema()
        assert s["type"] == "function" and s["function"]["name"] == name
        assert len(s["function"]["description"]) > 40, f"{name} needs a real description"
        p = s["function"]["parameters"]
        assert p["type"] == "object" and isinstance(p["properties"], dict)
        assert name.replace("_", "").isalnum(), f"{name} must be a safe tool name"
        assert t.risk in ("low", "medium", "high")


def test_results_are_truncated_with_an_honest_note(monkeypatch, tmp_path):
    """A local model will happily summarize 50 of 7,553 rows as "that's all of them", so a
    trimmed payload must say so and keep `total` intact."""
    setup_db(monkeypatch, tmp_path, items=[
        {"id": f"n{i}", "title": "A rather long game title " + "x" * 60, "system": NES} for i in range(200)
    ])
    out = call(reg(), "list_items", system=NES, limit=200)
    assert out["truncated"] is True
    assert out["note"] and "showing" in out["note"]
    assert out["data"]["total"] == 200            # the real count survives the trim
    assert len(json.dumps(out["data"])) < 3000


def test_a_small_result_is_not_marked_truncated(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "A", "system": NES}])
    out = call(reg(), "list_items", system=NES)
    assert out["truncated"] is False and out["note"] is None


# ------------------------------------------------------------------------ dispatch

def test_an_unknown_tool_lists_what_is_available(monkeypatch, tmp_path):
    """Useful to the model when it hallucinates a tool name — the error itself is the fix."""
    setup_db(monkeypatch, tmp_path)
    out = chattools.dispatch(reg(), "library_sumary", {})
    assert out["ok"] is False and "library_summary" in out["available"]


def test_a_tool_that_raises_becomes_an_error_envelope_not_a_crash(monkeypatch, tmp_path):
    """A tool error is information the model can act on; letting it propagate would end the
    turn instead."""
    setup_db(monkeypatch, tmp_path)
    r = reg()
    r["boom"] = chattools.Tool("boom", "x" * 50, chattools._obj({}), lambda a, c: 1 / 0)
    out = chattools.dispatch(r, "boom", {})
    assert out["ok"] is False and "ZeroDivisionError" in out["error"]


def test_a_tool_returning_an_error_dict_is_reported_as_not_ok(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    out = chattools.dispatch(reg(), "get_item", {"ident": "missing"})
    assert out["ok"] is False and "error" in out["data"]


def test_every_call_writes_an_audit_row(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "A", "system": NES}])
    r = reg()
    chattools.dispatch(r, "list_items", {"system": NES}, session_id=7)
    chattools.dispatch(r, "get_item", {"ident": "nope"}, session_id=7)
    rows = chatstore.recent_calls(7)
    assert {x["tool"] for x in rows} == {"list_items", "get_item"}
    assert {x["ok"] for x in rows} == {1, 0}       # failures are recorded too
    assert rows[0]["risk"] == "low"


def test_the_audit_log_cannot_take_down_the_call_it_records(monkeypatch, tmp_path):
    """Auditing is best-effort by design: a full disk or a locked database must not turn a
    working tool call into a failed one."""
    setup_db(monkeypatch, tmp_path)
    db = connect()
    with db:
        db.execute("DROP TABLE chat_tool_log")
    assert chatstore.log_call(1, "facets", {}, True, "low", "ok") is None   # no raise
    assert chattools.dispatch(reg(), "facets", {})["ok"] is True


# -------------------------------------------------------------------------------- acts

def actx(armed=None, launched=None, watcher=None, settings=None):
    """A ctx that records what it was asked to do, so the plumbing is pinned separately
    from the SQL: did the tool reach the launcher, and with what."""
    return chattools.Ctx(
        launch_job=lambda kind, params: (launched.append((kind, params)), True)[1],
        launch_watcher=lambda on: (watcher.append(on), {"on": on})[1],
        arm_acquire=(lambda: (armed.append(1), True)[1]) if armed is not None else None,
        set_settings=settings or (lambda values: {"saved": sorted(values)}))


def test_set_item_flags_writes_one_field_and_reports_the_change(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "Metroid", "authorized": 0}])
    armed = []
    out = call(reg(actx(armed=armed)), "set_item_flags", ident="a", field="authorized", value=True)
    assert out["ok"] is True
    assert out["data"]["updated"]["from"] == 0 and out["data"]["updated"]["to"] == 1
    assert connect().execute("SELECT authorized FROM items WHERE id='a'").fetchone()["authorized"] == 1
    assert armed == [1]        # arming an item must start the pipeline, as the UI route does


def test_set_item_flags_refuses_a_field_the_ui_would_also_refuse(monkeypatch, tmp_path):
    """The whitelist is imported from web.py rather than re-listed here, so a tool call
    cannot write a column the Library tab would reject."""
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "Metroid"}])
    out = call(reg(actx()), "set_item_flags", ident="a", field="crc32", value="x")
    assert out["ok"] is False and "field must be one of" in out["data"]["error"]


def test_set_item_flags_does_not_arm_the_pipeline_for_a_non_flag_field(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "Metroid", "notes": None}])
    armed = []
    call(reg(actx(armed=armed)), "set_item_flags", ident="a", field="notes", value="bought")
    assert armed == []


def test_acquire_item_refuses_an_unauthorized_item(monkeypatch, tmp_path):
    """The same human-control seam the Acquire tab enforces: authorizing is the owner's
    switch, and the assistant cannot flip it and download in one breath."""
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "Metroid", "authorized": 0}])
    out = call(reg(actx()), "acquire_item", ident="a", url="http://x/y.nzb")
    assert out["ok"] is False and "not marked authorized" in out["data"]["error"]


def test_acquire_item_queues_through_the_shared_action(monkeypatch, tmp_path):
    from romcom import sab
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "Metroid"}])
    sent = {}
    monkeypatch.setattr(sab, "add_url",
                        lambda url, name, priority=0: (sent.update(url=url, name=name)
                                                       or {"nzo_ids": ["n1"]}))
    out = call(reg(actx()), "acquire_item", ident="a", url="http://x/y.nzb", title="Metroid (USA)")
    assert out["ok"] is True and out["data"]["nzo_id"] == "n1"
    assert sent["name"] == "ROMCOM__a"
    assert connect().execute("SELECT status FROM items WHERE id='a'").fetchone()["status"] == "QUEUED"


def test_job_tools_launch_and_report_a_run_already_in_flight(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    launched = []
    r = reg(actx(launched=launched))
    assert call(r, "run_scan", path=str(tmp_path))["data"]["started"] == "scan"
    assert launched[0][0] == "scan" and launched[0][1]["name_match"] is True
    busy = chattools.Ctx(launch_job=lambda kind, params: False)
    out = chattools.dispatch(reg(busy), "start_auto_acquire", {})
    assert out["ok"] is False and "already running" in out["data"]["error"]


def test_run_scan_refuses_a_path_that_does_not_exist(monkeypatch, tmp_path):
    """A job that cannot possibly work is refused here, where the model can correct itself,
    rather than launched to fail in a background thread."""
    setup_db(monkeypatch, tmp_path)
    launched = []
    out = call(reg(actx(launched=launched)), "run_scan", path=str(tmp_path / "nope"))
    assert out["ok"] is False and "path not found" in out["data"]["error"] and launched == []


def test_defer_item_writes_the_same_skip_event_the_ui_writes(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path, items=[{"id": "a", "title": "Metroid"}])
    out = call(reg(actx()), "defer_item", ident="a", reason="no usable release")
    assert out["data"]["deferred"]["id"] == "a"
    ev = connect().execute("SELECT event,detail FROM events WHERE item_id='a'").fetchone()
    assert ev["event"] == "acquire-skip" and "no usable release" in ev["detail"]


# --------------------------------------------------------------- the confirm gate

def test_a_gated_tool_does_not_run_until_it_is_approved(monkeypatch, tmp_path):
    """The whole point: 'mark everything owned' must not touch a row on the way to asking."""
    setup_db(monkeypatch, tmp_path,
             items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    out = chattools.dispatch(reg(actx()), "mark_all_owned", {}, session_id=3)
    assert out["ok"] is False and out["requires_approval"] is True
    assert out["approval_id"] and out["summary"]
    assert "NOT RUN" in out["error"]
    row = connect().execute("SELECT wanted,authorized FROM items WHERE id='a'").fetchone()
    assert row["wanted"] == 0 and row["authorized"] == 0      # untouched


def test_the_confirm_card_says_how_much_it_would_change(monkeypatch, tmp_path):
    """'Change some items' is not a decision anyone can make; the count is the decision."""
    setup_db(monkeypatch, tmp_path, items=[
        {"id": f"n{i}", "system": NES, "status": "CATALOGED", "wanted": 1} for i in range(7)])
    out = chattools.dispatch(reg(actx()), "bulk_update_items",
                             {"filters": {"system": NES}, "field": "status", "value": "MISSING"},
                             session_id=1)
    assert "7 item(s)" in out["summary"]


def test_an_approved_call_runs_exactly_once_and_through_the_same_path(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path,
             items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    r = reg(actx())
    aid = chattools.dispatch(r, "mark_all_owned", {}, session_id=3)["approval_id"]
    assert chatstore.decide_approval(aid, True)["status"] == "approved"
    out = chattools.apply_approval(r, aid)
    assert out["ok"] is True and out["data"]["updated"] == 1
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 1
    assert chatstore.approval(aid)["result"]        # the outcome is recorded on the row


def test_an_approval_cannot_be_spent_twice(monkeypatch, tmp_path):
    """An approval is a permission for one run. If it stayed valid, holding an id would be
    the same as holding the right to repeat the call whenever you liked."""
    setup_db(monkeypatch, tmp_path,
             items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    r = reg(actx())
    aid = chattools.dispatch(r, "mark_all_owned", {}, session_id=3)["approval_id"]
    chatstore.decide_approval(aid, True)
    first = chattools.apply_approval(r, aid)
    assert first["ok"] is True and first["data"]["updated"] == 1
    # Second attempt, every way in: re-decide, re-apply, or dispatch with the id.
    assert chatstore.decide_approval(aid, True) is None
    assert chattools.apply_approval(r, aid)["ok"] is False
    again = chattools.dispatch(r, "mark_all_owned", {}, session_id=3, approval_id=aid)
    assert again["ok"] is False and "already" in again["error"]


def test_an_approval_cannot_be_reused_for_a_different_call(monkeypatch, tmp_path):
    """An approval is for one call, not for a tool. Otherwise 'yes, update my 7 NES items'
    would authorise rewriting the whole catalog."""
    setup_db(monkeypatch, tmp_path, items=[
        {"id": f"n{i}", "system": NES, "status": "CATALOGED", "wanted": 1} for i in range(7)])
    r = reg(actx())
    aid = chattools.dispatch(r, "bulk_update_items",
                             {"filters": {"system": NES}, "field": "status", "value": "MISSING"},
                             session_id=1)["approval_id"]
    chatstore.decide_approval(aid, True)
    swapped = chattools.dispatch(r, "bulk_update_items",
                                 {"filters": {}, "field": "status", "value": "EXCLUDED"},
                                 session_id=1, approval_id=aid)
    assert swapped["ok"] is False and "does not match" in swapped["error"]
    assert connect().execute(
        "SELECT COUNT(*) c FROM items WHERE status='EXCLUDED'").fetchone()["c"] == 0


def test_a_declined_call_refuses_to_run_and_reads_as_a_tool_result(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path,
             items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    r = reg(actx())
    aid = chattools.dispatch(r, "mark_all_owned", {}, session_id=3)["approval_id"]
    assert chatstore.decide_approval(aid, False)["status"] == "declined"
    out = chattools.apply_approval(r, aid)
    assert out["ok"] is False and "declined" in out["error"] and "runs once" in out["error"]
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 0
    msg = chattools.declined("mark_all_owned", {}, 3)
    assert "DECLINED" in msg["error"] and "Do not ask for the same call again" in msg["error"]


def test_organize_is_gated_because_it_moves_real_files(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    launched = []
    out = call(reg(actx(launched=launched)), "organize_library", path=str(tmp_path / "out"))
    assert out["requires_approval"] is True and launched == []
    assert "MOVE files" in out["summary"]


def test_watcher_toggle_is_gated_in_both_directions(monkeypatch, tmp_path):
    """On starts continuous downloading; off silently stops downloads that were running.
    Both are worth a prompt."""
    setup_db(monkeypatch, tmp_path)
    watcher = []
    r = reg(actx(watcher=watcher))
    for on in (True, False):
        assert chattools.dispatch(r, "watcher_toggle", {"on": on})["requires_approval"] is True
    assert watcher == []


def test_set_setting_masks_credentials_in_the_confirmation_and_the_result(monkeypatch, tmp_path):
    """A tool result reaches the model's context *and* the stored transcript, so a key
    echoed there has leaked twice over."""
    setup_db(monkeypatch, tmp_path)
    args = {"values": {"nzb_key": "deadbeef-secret", "acquire_batch_max": "5"}}
    out = chattools.dispatch(reg(actx()), "set_setting", args, session_id=1)
    assert out["requires_approval"] is True
    assert "deadbeef-secret" not in out["summary"] and "***" in out["summary"]

    aid = out["approval_id"]
    chatstore.decide_approval(aid, True)
    done = chattools.apply_approval(reg(actx()), aid)
    assert done["data"]["saved"]["nzb_key"] == "***"
    assert "deadbeef-secret" not in json.dumps(done)


def test_set_setting_still_refuses_a_key_the_settings_tab_would_reject(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    refused = chattools.Ctx(set_settings=lambda values: {"error": "unknown setting(s): nonsense"})
    aid = chattools.dispatch(reg(refused), "set_setting", {"values": {"nonsense": "1"}})["approval_id"]
    chatstore.decide_approval(aid, True)
    out = chattools.apply_approval(reg(refused), aid)
    assert out["ok"] is False and "unknown setting" in out["data"]["error"]


def test_every_gated_tool_states_its_own_confirmation_sentence(monkeypatch, tmp_path):
    """A generic 'run this tool?' card is not a decision aid. Each gated tool must describe
    its own effect — and describing it must not execute it."""
    setup_db(monkeypatch, tmp_path)
    r = reg(actx())
    gated = {n: t for n, t in r.items() if t.risk == "high"}
    assert gated, "expected some high-risk tools"
    for name, tool in gated.items():
        assert tool.confirm, f"{name} has no confirmation sentence"
        assert len(tool.confirm({})) > 30


# ------------------------------------------------------------------------------- memory

def embed(monkeypatch, **kw):
    from fake_llm import FakeOllama
    return FakeOllama(**kw).install(monkeypatch)


def test_remember_fact_stores_a_durable_note(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    out = call(reg(actx()), "remember_fact", key="snes_folder", value="D:/roms/snes",
               source="owner said so")
    assert out["ok"] is True
    assert out["data"]["saved"] == "snes_folder" and out["data"]["updated"] is False
    assert chatstore.fact("snes_folder")["value"] == "D:/roms/snes"


def test_remember_fact_says_when_it_replaced_a_previous_value(monkeypatch, tmp_path):
    """The model needs to know it overwrote something, or it cannot tell the owner their old
    preference was superseded."""
    setup_db(monkeypatch, tmp_path)
    r = reg(actx())
    call(r, "remember_fact", key="snes_folder", value="D:/roms/snes")
    out = call(r, "remember_fact", key="snes_folder", value="E:/roms/snes")
    assert out["data"]["updated"] is True and "replaced" in out["data"]["note"]
    assert chatstore.facts() and len(chatstore.facts()) == 1


def test_remember_fact_refuses_something_that_looks_like_a_credential(monkeypatch, tmp_path):
    """A secret pasted into chat must not become a permanent injection into every future
    turn. Refused at write, not masked later — a stored secret is already in the database."""
    setup_db(monkeypatch, tmp_path)
    for value in ["api_key: abc123", "my password = hunter2", "Bearer sk-live-xyz",
                  "indexer token: 9f3a"]:
        out = call(reg(actx()), "remember_fact", key="creds", value=value)
        assert out["ok"] is False, value
        assert "credential" in out["data"]["error"]
    assert chatstore.facts() == []


def test_remember_fact_requires_both_halves(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    assert call(reg(actx()), "remember_fact", key="k")["ok"] is False
    assert call(reg(actx()), "remember_fact", value="v")["ok"] is False
    assert chatstore.facts() == []


def test_list_facts_shows_everything_remembered(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    r = reg(actx())
    call(r, "remember_fact", key="a", value="1")
    call(r, "remember_fact", key="b", value="2")
    out = call(r, "list_facts")
    assert out["data"]["total"] == 2
    assert {f["key"] for f in out["data"]["facts"]} == {"a", "b"}


def test_forget_fact_deletes_and_reports_an_unknown_key(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    r = reg(actx())
    call(r, "remember_fact", key="temp", value="x")
    assert call(r, "forget_fact", key="temp")["data"]["forgotten"] == "temp"
    assert chatstore.fact("temp") is None
    out = call(r, "forget_fact", key="temp")
    assert out["ok"] is False and "no remembered fact" in out["data"]["error"]


def test_recall_memory_finds_a_note_by_meaning(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    from fake_llm import FakeOllama
    FakeOllama(embed_fn=lambda t: [1.0, 0.0] if "snes" in t.lower() else [0.0, 1.0]).install(monkeypatch)
    chatstore.store_chunk("note", "the snes folder lives on D:")
    out = call(reg(actx()), "recall_memory", query="where are the snes files")
    assert out["ok"] is True and out["data"]["hits"] == 1
    assert "snes folder" in out["data"]["results"][0]["text"]


def test_recall_memory_says_when_memory_is_simply_empty(monkeypatch, tmp_path):
    """An empty result must read as a gap in what was recorded, not a failed search — the
    model should not retry the same query hoping for a different answer."""
    setup_db(monkeypatch, tmp_path)
    embed(monkeypatch, embed_fn=lambda t: [1.0, 0.0])
    out = call(reg(actx()), "recall_memory", query="anything at all")
    assert out["ok"] is True and out["data"]["hits"] == 0
    assert "gap in what has been recorded" in out["data"]["note"]


def test_recall_memory_requires_a_query(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    out = call(reg(actx()), "recall_memory", query="  ")
    assert out["ok"] is False and "required" in out["data"]["error"]


def test_the_memory_tools_are_all_read_only_and_ungated(monkeypatch, tmp_path):
    """Memory is the agent's own notebook. Prompting the owner to confirm every note would
    make the feature unusable; the credential guard above is what keeps it safe."""
    setup_db(monkeypatch, tmp_path)
    r = reg(actx())
    for name in ("remember_fact", "recall_memory", "list_facts", "forget_fact"):
        assert r[name].risk == "low", name
        assert r[name].confirm is None, name
