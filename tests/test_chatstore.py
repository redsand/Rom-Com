"""Chat persistence. The load-bearing property is that a stored thread round-trips back into
exactly the message shape Ollama needs, because a turn that reloads a subtly different
conversation than the model saw is a bug that only shows up as confused answers."""
import json

import pytest
from fake_llm import FakeOllama

from romcom import chatstore
from romcom.db import connect


def setup_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    return connect()


def test_a_new_session_is_empty_and_listed(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("Find the ghosts")
    s = chatstore.session(sid)
    assert s["title"] == "Find the ghosts"
    assert chatstore.history(sid) == []
    assert [x["id"] for x in chatstore.sessions()] == [sid]


def test_an_untitled_session_gets_a_title_from_its_first_message(monkeypatch, tmp_path):
    """A session list of seven rows all called "New chat" is useless; the first user turn is
    what the owner will recognize it by."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    assert chatstore.session(sid)["title"] is None
    chatstore.touch(sid, title="what am I missing on the NES?")
    assert chatstore.session(sid)["title"] == "what am I missing on the NES?"


def test_messages_come_back_oldest_first_and_page_from_the_end(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    ids = [chatstore.append(sid, "user", f"m{i}") for i in range(10)]
    assert [m["content"] for m in chatstore.history(sid)] == [f"m{i}" for i in range(10)]
    tail = chatstore.history(sid, limit=3)
    assert [m["content"] for m in tail] == ["m7", "m8", "m9"]
    assert [m["id"] for m in tail] == ids[-3:]


def test_a_thread_round_trips_into_ollamas_message_shape(monkeypatch, tmp_path):
    """This is the contract. A tool result is matched back to its call BY NAME, so the name
    must survive; the assistant's own tool_calls must come back as structured calls, not as
    text the model has to re-parse."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "user", "how many are missing?")
    calls = [{"id": "c1", "name": "list_items", "arguments": {"view": "missing"}}]
    chatstore.append(sid, "assistant", "", thinking="I should count them", tool_args=calls)
    chatstore.append(sid, "tool", '{"ok": true, "data": {"total": 3}}', tool_name="list_items")
    chatstore.append(sid, "assistant", "Three are missing.")
    msgs = chatstore.to_messages(chatstore.history(sid))
    assert msgs[0] == {"role": "user", "content": "how many are missing?"}
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"] == calls
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_name"] == "list_items"
    assert msgs[3] == {"role": "assistant", "content": "Three are missing."}


def test_reasoning_is_stored_but_never_replayed_into_context(monkeypatch, tmp_path):
    """`thinking` is for the UI's reasoning bubble. Re-feeding a local model its own prior
    reasoning degrades it and inflates the prompt for nothing, so it must not appear in the
    rebuilt messages."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "assistant", "The answer is 3.", thinking="secret chain of thought")
    stored = chatstore.history(sid)[0]
    assert stored["thinking"] == "secret chain of thought"
    msgs = chatstore.to_messages(chatstore.history(sid))
    assert "thinking" not in msgs[0] and "secret" not in json.dumps(msgs)


def test_tool_results_are_capped_at_insert(monkeypatch, tmp_path):
    """A 40 KB library_summary replayed on every subsequent turn is what makes a tool-heavy
    thread blow the context window. The in-flight turn still sees the full result."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "tool", "Z" * 9000, tool_name="library_summary")
    stored = chatstore.history(sid)[0]["content"]
    assert len(stored) < chatstore.TOOL_RESULT_CAP + 100
    assert "truncated" in stored and "9000" in stored


def test_a_short_tool_result_is_not_annotated(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "tool", '{"ok":true}', tool_name="doctor")
    assert chatstore.history(sid)[0]["content"] == '{"ok":true}'


def test_thinking_can_be_backfilled_after_a_stream_finishes(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    mid = chatstore.append(sid, "assistant", "answer")
    chatstore.set_thinking(mid, "reasoned about it")
    assert chatstore.history(sid)[0]["thinking"] == "reasoned about it"


def test_drop_after_removes_a_half_written_turn(monkeypatch, tmp_path):
    """If a turn is abandoned mid-stream, its partial assistant reply must not be replayed
    as context on the next turn."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    keep = chatstore.append(sid, "user", "question")
    chatstore.append(sid, "assistant", "half an ans")
    chatstore.append(sid, "tool", "{}", tool_name="facets")
    assert chatstore.drop_after(sid, keep) == 2
    assert [m["content"] for m in chatstore.history(sid)] == ["question"]


def test_deleting_a_session_takes_its_messages_but_not_the_audit_trail(monkeypatch, tmp_path):
    """An audit trail that vanishes when someone tidies their chat list is worthless."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("doomed")
    chatstore.append(sid, "user", "hi")
    chatstore.log_call(sid, "list_items", {"view": "missing"}, True, "low", "ok")
    assert chatstore.delete_session(sid) == 1
    assert chatstore.session(sid) is None
    assert chatstore.history(sid) == []
    assert [r["tool"] for r in chatstore.recent_calls(sid)] == ["list_items"]


def test_the_session_list_reports_message_counts_and_a_preview(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("thread")
    chatstore.append(sid, "user", "the first thing I asked")
    chatstore.append(sid, "assistant", "an answer")
    row = chatstore.sessions()[0]
    assert row["messages"] == 2 and row["first_user"] == "the first thing I asked"


def test_sessions_are_ordered_by_most_recent_activity(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    a = chatstore.new_session("older")
    b = chatstore.new_session("newer")
    db = connect()
    with db:
        db.execute("UPDATE chat_sessions SET updated_at='2020-01-01 00:00:00' WHERE id=?", (a,))
        db.execute("UPDATE chat_sessions SET updated_at='2030-01-01 00:00:00' WHERE id=?", (b,))
    assert [s["id"] for s in chatstore.sessions()] == [b, a]


def test_the_audit_log_records_arguments_and_outcome(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    chatstore.log_call(3, "list_items", {"system": "NES", "limit": 50}, True, "low", "12 rows")
    chatstore.log_call(3, "get_item", {"ident": "nope"}, False, "low", "not found")
    rows = chatstore.recent_calls(3)
    assert rows[0]["tool"] == "get_item" and rows[0]["ok"] == 0
    assert json.loads(rows[1]["arguments"])["system"] == "NES"
    assert rows[1]["result_summary"] == "12 rows"


# ---------------------------------------------------------------------------- approvals

def test_a_request_starts_pending_and_lists_against_its_session(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    aid = chatstore.request_approval(4, "mark_all_owned", {}, "Mark everything owned")
    row = chatstore.approval(aid)
    assert row["status"] == "pending" and row["session_id"] == 4
    assert json.loads(row["arguments"]) == {}
    assert [a["id"] for a in chatstore.pending_approvals(4)] == [aid]
    assert chatstore.pending_approvals(9) == []


def test_a_decision_is_recorded_once_and_only_from_pending(monkeypatch, tmp_path):
    """Idempotence is enforced by the UPDATE's WHERE clause, not by the caller remembering
    to check — a double-click must not be able to approve twice."""
    setup_db(monkeypatch, tmp_path)
    aid = chatstore.request_approval(1, "watcher_toggle", {"on": True}, "Turn the watcher on")
    assert chatstore.decide_approval(aid, True)["status"] == "approved"
    assert chatstore.decide_approval(aid, True) is None
    assert chatstore.decide_approval(aid, False) is None
    assert chatstore.approval(aid)["decided_at"]


def test_spending_an_approval_consumes_it(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    aid = chatstore.request_approval(1, "mark_all_owned", {}, "s")
    assert chatstore.spend_approval(aid) is None            # not approved yet
    chatstore.decide_approval(aid, True)
    assert chatstore.spend_approval(aid)["status"] == "used"
    assert chatstore.spend_approval(aid) is None            # and not twice
    assert chatstore.approval(aid)["status"] == "used"


# ------------------------------------------------------------------------------- facts

def test_remembering_the_same_key_updates_rather_than_duplicating(monkeypatch, tmp_path):
    """'The owner's preferred SNES folder' should have one current answer, not five rows the
    agent has to disambiguate between."""
    setup_db(monkeypatch, tmp_path)
    chatstore.remember_fact("snes_folder", "D:/roms/snes", source="conversation")
    chatstore.remember_fact("snes_folder", "E:/roms/super-nintendo")
    assert chatstore.facts() and len(chatstore.facts()) == 1
    f = chatstore.fact("snes_folder")
    assert f["value"] == "E:/roms/super-nintendo"      # the newer value wins
    assert f["updated_at"]


def test_a_remembered_fact_round_trips_with_its_source_and_confidence(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    chatstore.remember_fact("prefer_no_intro", "true", source="owner said so", confidence=0.9)
    f = chatstore.fact("prefer_no_intro")
    assert f["source"] == "owner said so" and f["confidence"] == pytest.approx(0.9)


def test_a_blank_key_is_refused_rather_than_stored(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    assert chatstore.remember_fact("   ", "something") is None
    assert chatstore.facts() == []


def test_forgetting_a_fact_removes_it(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    chatstore.remember_fact("temp", "x")
    assert chatstore.forget_fact("temp") == 1
    assert chatstore.fact("temp") is None
    assert chatstore.forget_fact("temp") == 0           # already gone


# -------------------------------------------------------------------------- embeddings

def fake_embed(monkeypatch, embed_fn=None, **kw):
    """Install a fake Ollama. With `embed_fn`, vectors are deterministic per text so cosine
    ordering is meaningful — the default uniform vector scores every chunk identically and
    would make a recall test pass without testing anything.

    Clears the embedding LRU first: it is module state keyed on (model, text), and two tests
    that use the same text with different fake vectors would otherwise see the first test's
    vector. Installing a fake embed model invalidates everything cached from the last one."""
    chatstore._embed_cache.clear()
    return FakeOllama(embed_fn=embed_fn, **kw).install(monkeypatch)


def test_the_embedding_cache_is_keyed_on_the_model_not_just_the_text(monkeypatch, tmp_path):
    """A same-dimension model swap must not serve the old model's vectors out of the cache.
    The dimension lock would not catch it — both models return 768 dims — so the model name
    has to be part of the cache key or recall silently compares incomparable vectors."""
    setup_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_CHAT_EMBED_MODEL", "model-a")
    from romcom.config import invalidate
    invalidate()
    FakeOllama(embed_fn=lambda t: [1.0, 0.0]).install(monkeypatch)
    assert chatstore.embed_cached(["hello"])[0] == [[1.0, 0.0]]

    monkeypatch.setenv("ROMCOM_CHAT_EMBED_MODEL", "model-b")
    invalidate()
    FakeOllama(embed_fn=lambda t: [0.0, 1.0]).install(monkeypatch)
    # Same text, different model: must re-embed, not return the cached [1.0, 0.0].
    assert chatstore.embed_cached(["hello"])[0] == [[0.0, 1.0]]


def bag_of_words(*vocab):
    """A tiny deterministic embedder: the count of each vocabulary word. Two texts sharing
    vocabulary score high, unrelated ones score 0.

    Note this is *raw counts*, not unit vectors, so cosine normalises away repetition: a
    document containing only "alpha" is more similar to the query "alpha alpha" than one
    containing "alpha alpha beta" is. That is correct cosine behaviour, and it is why the
    ordering tests below use `fixed()` instead — ranking should be asserted against
    similarities that are unambiguous rather than an artifact of word counts."""
    def fn(text):
        t = (text or "").lower()
        return [float(t.count(w)) for w in vocab]
    return fn


def fixed(**vectors):
    """An embedder that maps exact text → exact vector, so cosine ordering is unambiguous.
    Any unmapped text gets `default` (pointing away from every mapped vector)."""
    dim = max(len(v) for v in vectors.values())
    def fn(text):
        return vectors.get(text, [0.0] * dim)
    return fn


def test_a_stored_chunk_can_be_recalled_and_is_scored(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("snes", "folder", "genesis", "case"))
    chatstore.store_chunk("note", "the snes folder is D:/roms/snes")
    chatstore.store_chunk("note", "genesis case art goes in E:/art")
    hits = chatstore.recall("where is my snes folder", k=5)
    assert hits and "snes" in hits[0]["text"]
    assert hits[0]["score"] > 0.9
    assert all(h["score"] >= 0.35 for h in hits)


def test_recall_returns_nearest_first(monkeypatch, tmp_path):
    """Ordering is by cosine similarity, most similar first. Similarities here are exact, so
    this tests the ranking rather than an artifact of the test embedder."""
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, fixed(query=[1.0, 1.0], exact=[1.0, 1.0], near=[1.0, 0.8],
                                  orthogonal=[-1.0, 1.0]))
    chatstore.store_chunk("note", "near")
    chatstore.store_chunk("note", "orthogonal")
    chatstore.store_chunk("note", "exact")
    hits = chatstore.recall("query", k=3)
    assert [h["text"] for h in hits] == ["exact", "near"]
    assert hits[0]["score"] == pytest.approx(1.0)


def test_recall_can_be_narrowed_to_one_kind(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("alpha", "beta"))
    chatstore.store_chunk("note", "alpha note")
    chatstore.store_chunk("session_summary", "alpha summary", ref_id=7)
    assert [h["kind"] for h in chatstore.recall("alpha", kind="note")] == ["note"]
    assert [h["kind"] for h in chatstore.recall("alpha", kind="session_summary")] == ["session_summary"]


def test_recall_respects_the_minimum_score(monkeypatch, tmp_path):
    """An unrelated chunk must come back empty rather than as a weak-but-present hit, or the
    agent would inject irrelevant context into every turn."""
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("alpha", "beta"))
    chatstore.store_chunk("note", "beta")
    assert chatstore.recall("alpha", min_score=0.5) == []


def test_recall_bumps_last_accessed_so_used_memories_are_visible(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("alpha"))
    cid = chatstore.store_chunk("note", "alpha")
    assert connect().execute("SELECT last_accessed FROM chat_memory_chunks WHERE id=?",
                             (cid,)).fetchone()["last_accessed"] is None
    chatstore.recall("alpha")
    assert connect().execute("SELECT last_accessed FROM chat_memory_chunks WHERE id=?",
                             (cid,)).fetchone()["last_accessed"]


def test_an_embedding_model_outage_returns_no_hits_rather_than_raising(monkeypatch, tmp_path):
    """Recall is an enhancement. A turn that dies because the embedding model is down is
    strictly worse than a turn with no recalled context."""
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[{"status": 500, "error": "model runner crashed"}]).install(monkeypatch)
    assert chatstore.recall("anything") == []
    assert chatstore.recall("") == []


def test_the_dimension_lock_refuses_a_vector_from_a_different_model(monkeypatch, tmp_path):
    """Swapping to bge-m3 (1024-dim) must fail loudly at write time rather than quietly
    filling the store with a second, incomparable vector space."""
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, embed_dim=768)
    assert chatstore.store_chunk("note", "768-dim chunk")
    # Same model name, different dimension — exactly what a swapped model looks like.
    monkeypatch.setattr(chatstore, "_embed_cache", {})
    FakeOllama(embed_dim=1024).install(monkeypatch)
    with pytest.raises(RuntimeError) as e:
        chatstore.store_chunk("note", "1024-dim chunk")
    assert "dimension changed" in str(e.value) and "768" in str(e.value)
    # And nothing was written.
    assert connect().execute("SELECT COUNT(*) c FROM chat_memory_chunks").fetchone()["c"] == 1


def test_recall_never_compares_against_a_foreign_model(monkeypatch, tmp_path):
    """Even if a mismatched row somehow exists, `recall` filters it out by model."""
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("alpha"))
    chatstore.store_chunk("note", "alpha")
    db = connect()
    with db:      # a row from another embed model, written around the lock
        db.execute("INSERT INTO chat_memory_chunks(kind,text,embedding,dim,model,norm)"
                   " VALUES('note','foreign',?,2,'bge-m3',1.0)", (b"\x00\x00\x00\x00\x00\x00\x80?",))
    assert [h["text"] for h in chatstore.recall("alpha")] == ["alpha"]


def test_an_empty_chunk_is_not_stored(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("alpha"))
    assert chatstore.store_chunk("note", "   ") is None
    assert connect().execute("SELECT COUNT(*) c FROM chat_memory_chunks").fetchone()["c"] == 0


def test_deleting_a_session_takes_its_summary_chunk_with_it(monkeypatch, tmp_path):
    """Otherwise recall keeps surfacing a conversation the owner deliberately removed."""
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("alpha"))
    sid = chatstore.new_session("doomed")
    chatstore.store_chunk("session_summary", "alpha summary", ref_id=sid)
    chatstore.store_chunk("note", "alpha note")
    chatstore.delete_session(sid)
    remaining = connect().execute("SELECT text FROM chat_memory_chunks").fetchall()
    assert [r["text"] for r in remaining] == ["alpha note"]


# ---------------------------------------------------------------------- summarization

def _fill(sid, n, start=0):
    for i in range(start, start + n):
        chatstore.append(sid, "user", f"question {i}")
        chatstore.append(sid, "assistant", f"answer {i}")


def test_nothing_is_summarized_below_the_trigger(monkeypatch, tmp_path):
    """29 messages is not due. Summarizing eagerly would mean a model call on nearly every
    short conversation, which on a local model is the whole cost of the feature."""
    setup_db(monkeypatch, tmp_path)
    fake = fake_embed(monkeypatch)
    sid = chatstore.new_session("short")
    _fill(sid, 14)                       # 28 messages
    assert len(chatstore._unsummarized(sid)) == 28
    assert chatstore.maybe_summarize(sid) is None
    assert fake.calls == []              # no model call was made
    assert chatstore.session(sid)["summary"] is None


def test_the_summary_fires_at_the_trigger_and_keeps_the_recent_window_verbatim(
        monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["The owner asked about NES coverage."]}]).install(monkeypatch)
    sid = chatstore.new_session("long")
    _fill(sid, 16)                       # 32 messages — past the 30 trigger
    text = chatstore.maybe_summarize(sid)
    assert text == "The owner asked about NES coverage."
    row = chatstore.session(sid)
    assert row["summary"] == text
    # Everything but the newest SUMMARY_KEEP was compressed, and the watermark is the id of
    # the last message that went in — so the kept window is exactly 20 messages.
    assert len(chatstore._unsummarized(sid)) == chatstore.SUMMARY_KEEP


def test_the_summarizer_sees_only_the_messages_being_compressed(monkeypatch, tmp_path):
    """The prompt must carry the transcript it is compressing — and the retained window must
    not be in it, or the model would compress messages that are about to be replayed anyway."""
    setup_db(monkeypatch, tmp_path)
    fake = FakeOllama(turns=[{"tokens": ["summary"]}]).install(monkeypatch)
    sid = chatstore.new_session("long")
    _fill(sid, 16)
    chatstore.maybe_summarize(sid)
    sent = fake.calls[0][1]["messages"]
    transcript = sent[-1]["content"]
    assert "question 0" in transcript and "answer 0" in transcript
    assert "question 15" not in transcript          # inside the retained window
    assert "tools" not in fake.calls[0][1]          # summarization is not a tool call


def test_re_summarizing_folds_in_the_previous_summary(monkeypatch, tmp_path):
    """The summary is rolling: each pass must build on the last, not replace it with a
    summary of only the newest batch."""
    setup_db(monkeypatch, tmp_path)
    fake = FakeOllama(turns=[{"tokens": ["first pass"]},
                             {"tokens": ["second pass"]}]).install(monkeypatch)
    sid = chatstore.new_session("long")
    _fill(sid, 16)
    chatstore.maybe_summarize(sid)
    _fill(sid, chatstore.SUMMARY_EVERY + 1, start=100)      # +15 or more new messages
    chatstore.maybe_summarize(sid)
    assert len(fake.calls) == 2
    second = fake.calls[1][1]["messages"]
    assert any("first pass" in (m["content"] or "") for m in second)


def test_a_failed_summarization_does_not_advance_the_watermark(monkeypatch, tmp_path):
    """Otherwise one Ollama hiccup would silently discard the beginning of the thread
    forever, because the messages would look 'already summarized' next time."""
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[{"status": 500, "error": "model runner crashed"}]).install(monkeypatch)
    sid = chatstore.new_session("long")
    _fill(sid, 16)
    before = len(chatstore._unsummarized(sid))
    assert chatstore.maybe_summarize(sid) is None
    assert chatstore.session(sid)["summary"] is None
    assert len(chatstore._unsummarized(sid)) == before       # nothing lost


def test_an_empty_summary_response_does_not_advance_the_watermark(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["   "]}]).install(monkeypatch)
    sid = chatstore.new_session("long")
    _fill(sid, 16)
    assert chatstore.maybe_summarize(sid) is None
    assert chatstore.session(sid)["summary"] is None


def test_the_summary_is_also_stored_as_a_recallable_chunk(monkeypatch, tmp_path):
    """So 'what did we decide about the SNES folder' works from a brand-new session."""
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, bag_of_words("snes", "folder"))
    FakeOllama(turns=[{"tokens": ["We settled the snes folder."]}],
               embed_fn=bag_of_words("snes", "folder")).install(monkeypatch)
    sid = chatstore.new_session("long")
    _fill(sid, 16)
    chatstore.maybe_summarize(sid)
    hits = chatstore.recall("the snes folder", kind="session_summary")
    assert len(hits) == 1 and hits[0]["ref_id"] == str(sid)
    assert "snes folder" in hits[0]["text"]


def test_the_recall_floor_sits_in_the_measured_gap_not_at_the_plans_guess(monkeypatch, tmp_path):
    """`nomic-embed-text` scores unrelated English sentences at 0.30-0.38 and genuinely
    relevant pairs at ~0.69 (measured live). The plan's inherited 0.35 let off-topic queries
    pull library chunks into the turn, so the floor has to be above the noise and below real
    matches. This fails loudly if someone lowers it back without re-measuring."""
    assert chatstore.RECALL_MIN_SCORE > 0.40, "below the measured noise floor — recall becomes noise"
    assert chatstore.RECALL_MIN_SCORE < 0.69, "above real matches — recall stops finding anything"
    # And `recall`'s default is that value, not a second copy of a different number.
    assert chatstore.recall.__defaults__[1] is None


def test_recall_uses_the_module_floor_by_default(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    fake_embed(monkeypatch, fixed(query=[1.0, 0.0], noise=[0.44, 0.9]))
    chatstore.store_chunk("note", "noise")        # cosine ~0.44 — under the floor
    assert chatstore.recall("query") == []
