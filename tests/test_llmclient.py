"""The Ollama client. These tests exist mostly to pin wire-format details that were verified
against a live 0.34.0 daemon and would be silently broken by a plausible-looking refactor —
NDJSON framing, dict-shaped tool arguments, the thinking channel."""
import pytest

from romcom import llmclient
from fake_llm import FakeOllama, ndjson


def setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_CHAT_MODEL", "qwen3:14b")


# ------------------------------------------------------------------- streaming/parsing

def test_streams_ndjson_not_sse(monkeypatch, tmp_path):
    """One JSON object per line — NOT `data:`-framed SSE. If someone ever 'fixes' the parser
    toward SSE, the model's output becomes empty while the request still succeeds."""
    setup_env(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["Hel", "lo"], "thinking": ["thin", "king"],
                       "usage": {"eval_count": 7, "total_duration": 12345}}]).install(monkeypatch)
    seen = []
    out = llmclient.chat([{"role": "user", "content": "hi"}], on_event=lambda n, t: seen.append((n, t)))
    assert out["content"] == "Hello"
    assert out["thinking"] == "thinking"
    assert seen == [("thinking", "thin"), ("thinking", "king"), ("token", "Hel"), ("token", "lo")]
    assert out["usage"]["eval_count"] == 7 and out["usage"]["total_duration"] == 12345


def test_blank_lines_and_unknown_fields_are_tolerated(monkeypatch, tmp_path):
    """Real streams carry keepalives and fields we don't read; neither may abort a turn."""
    setup_env(monkeypatch, tmp_path)
    FakeOllama(turns=[ndjson(
        {"message": {"content": "a"}, "done": False, "created_at": "x"},
        {"message": {"content": "b"}, "done": False, "some_future_field": {"nested": True}},
        {"done": True, "eval_count": 2},
    )]).install(monkeypatch)
    assert llmclient.chat([{"role": "user", "content": "hi"}])["content"] == "ab"


def test_tool_arguments_arrive_as_a_dict_and_are_not_json_parsed(monkeypatch, tmp_path):
    """THE porting bug. Ollama sends `arguments` already parsed, so `json.loads()` on it
    raises TypeError. A tool call whose arguments are a dict must survive untouched."""
    setup_env(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tool_calls": [{"name": "list_items",
                                       "arguments": {"system": "NES", "limit": 5}}]}]).install(monkeypatch)
    out = llmclient.chat([{"role": "user", "content": "nes?"}])
    assert out["tool_calls"] == [{"id": "call_1", "name": "list_items",
                                  "arguments": {"system": "NES", "limit": 5}}]


def test_tool_arguments_as_a_json_string_still_work(monkeypatch, tmp_path):
    """Some builds stream arguments as a string, in pieces. Both shapes are accepted, so a
    server-side change can't quietly turn every tool call into an empty argument dict."""
    setup_env(monkeypatch, tmp_path)
    FakeOllama(turns=[ndjson(
        {"message": {"tool_calls": [{"id": "c", "function": {"index": 0, "name": "get_item",
                                                             "arguments": '{"ident"'}}]}, "done": False},
        {"message": {"tool_calls": [{"function": {"index": 0, "arguments": ': "nes-1"}'}}]}, "done": False},
        {"done": True},
    )]).install(monkeypatch)
    out = llmclient.chat([{"role": "user", "content": "x"}])
    assert out["tool_calls"][0]["arguments"] == {"ident": "nes-1"}


def test_thinking_and_reasoning_variants_all_map_to_the_thinking_channel(monkeypatch, tmp_path):
    setup_env(monkeypatch, tmp_path)
    FakeOllama(turns=[ndjson(
        {"message": {"thinking": "a"}, "done": False},
        {"message": {"reasoning": "b"}, "done": False},
        {"message": {"reasoning_content": "c"}, "done": False},
        {"done": True},
    )]).install(monkeypatch)
    out = llmclient.chat([{"role": "user", "content": "x"}])
    assert out["thinking"] == "abc" and out["content"] == ""


def test_a_server_error_is_raised_not_swallowed(monkeypatch, tmp_path):
    setup_env(monkeypatch, tmp_path)
    FakeOllama(turns=[{"status": 500, "error": "model runner has crashed"}]).install(monkeypatch)
    with pytest.raises(RuntimeError, match="crashed"):
        llmclient.chat([{"role": "user", "content": "x"}])


def test_non_streaming_calls_go_through_the_same_assembly(monkeypatch, tmp_path):
    setup_env(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["non", "stream"], "usage": {"eval_count": 3}}]).install(monkeypatch)
    out = llmclient.chat([{"role": "user", "content": "x"}], stream=False)
    assert out["content"] == "nonstream" and out["usage"]["eval_count"] == 3


# --------------------------------------------------------------------------- overflow

def test_context_overflow_retries_once_with_pruned_messages(monkeypatch, tmp_path):
    """A tool that returns too much must degrade the assistant, not kill the turn — the same
    fail-soft posture `llm.py` takes. Exactly one retry, with the oversized tool result
    stubbed down instead of dropped: it is part of the turn in flight, so the model still
    needs to know it called that tool and roughly what came back."""
    setup_env(monkeypatch, tmp_path)
    fake = FakeOllama(turns=[
        {"status": 500, "error": "prompt is too long: 9000 tokens > 8192 maximum context length"},
        {"tokens": ["recovered"]},
    ]).install(monkeypatch)
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "what am I missing?"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_items"}}]},
            {"role": "tool", "content": "X" * 5000, "tool_name": "list_items"}]
    out = llmclient.chat(msgs)
    assert out["content"] == "recovered"
    assert len(fake.calls) == 2
    retried = fake.calls[1][1]["messages"]
    assert retried[0] == {"role": "system", "content": "sys"}          # instructions survive
    assert retried[1]["content"] == "what am I missing?"               # the question survives
    tool = [m for m in retried if m["role"] == "tool"][0]              # and the exchange survives
    assert "trimmed" in tool["content"] and len(tool["content"]) < 500


def test_an_old_tool_result_is_dropped_entirely_on_overflow(monkeypatch, tmp_path):
    """The counterpart: a tool result from a *previous* turn is dead weight and goes first."""
    setup_env(monkeypatch, tmp_path)
    fake = FakeOllama(turns=[
        {"status": 500, "error": "too many tokens for context length"},
        {"tokens": ["ok"]},
    ]).install(monkeypatch)
    msgs = [{"role": "system", "content": "sys"},
            {"role": "tool", "content": "O" * 5000, "tool_name": "library_summary"},
            {"role": "user", "content": "now show me something else"}]
    llmclient.chat(msgs)
    retried = fake.calls[1][1]["messages"]
    assert not [m for m in retried if m["role"] == "tool"]


def test_a_non_overflow_error_is_not_retried(monkeypatch, tmp_path):
    """The overflow path prunes and retries; nothing else takes that path.

    The example here used to be "connection reset", which is now deliberately retried as
    a transient upstream failure — so it no longer tests what this is about. A missing
    model is permanent: retrying it only makes the user wait longer for the same error."""
    setup_env(monkeypatch, tmp_path)
    fake = FakeOllama(turns=[{"status": 500, "error": "model 'nope' not found"}]).install(monkeypatch)
    with pytest.raises(RuntimeError):
        llmclient.chat([{"role": "user", "content": "x"}])
    assert len(fake.calls) == 1


# ------------------------------------------------------------------------------ prune

def test_prune_never_drops_the_system_prompt_or_the_current_turn():
    msgs = [{"role": "system", "content": "instructions"}]
    msgs += [{"role": "user", "content": f"m{i}" * 50} for i in range(40)]
    msgs.append({"role": "tool", "content": "T" * 3000, "tool_name": "x"})
    msgs.append({"role": "user", "content": "the actual question"})
    out = llmclient.prune(msgs, budget_chars=2000)
    assert out[0]["content"] == "instructions"
    assert out[-1]["content"] == "the actual question"
    assert len(llmclient.prune(msgs, budget_chars=2000)) < len(msgs)


def test_prune_drops_tool_results_before_conversation():
    """A stale tool payload is the least useful thing in the window; a prior user turn still
    carries the thread of the conversation."""
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "tool", "content": "T" * 4000, "tool_name": "big"},
            {"role": "user", "content": "u2"}]
    out = llmclient.prune(msgs, budget_chars=500)
    assert [m["role"] for m in out] == ["system", "user", "user"]
    assert out[1]["content"] == "u1"     # the conversation turn outlived the tool payload


def test_prune_handles_empty_input():
    assert llmclient.prune([]) == []


# ----------------------------------------------------------------------- resolve_model

def test_chat_model_wins_when_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_CHAT_MODEL", "mistral-small3.2:24b")
    assert llmclient.resolve_model() == "mistral-small3.2:24b"


def test_falls_back_to_a_locally_present_tool_capable_model(monkeypatch, tmp_path):
    """The point of this whole function: llm_model's default is a CLOUD model, so inheriting
    it would make the assistant need the network. Nothing configured must still resolve to
    something local that can actually call tools."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ROMCOM_CHAT_MODEL", raising=False)
    monkeypatch.setenv("ROMCOM_LLM_MODEL", "deepseek-v4.1-flash:cloud")
    FakeOllama().install(monkeypatch)
    assert llmclient.resolve_model() == "gemma4:12b"    # first installed with 'tools'


def test_llm_model_is_used_only_if_actually_installed_and_tool_capable(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ROMCOM_CHAT_MODEL", raising=False)
    monkeypatch.setenv("ROMCOM_LLM_MODEL", "qwen3:14b")
    FakeOllama().install(monkeypatch)
    assert llmclient.resolve_model() == "qwen3:14b"


def test_resolution_fails_loudly_when_nothing_can_call_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ROMCOM_CHAT_MODEL", raising=False)
    FakeOllama(tags=["nomic-embed-text:latest"],
               capabilities={"nomic-embed-text:latest": ["embedding"]}).install(monkeypatch)
    with pytest.raises(RuntimeError, match="no tool-capable"):
        llmclient.resolve_model()


# -------------------------------------------------------------------------- embeddings

def test_embed_reports_the_dimension_and_the_model(monkeypatch, tmp_path):
    """The dimension is returned rather than assumed, because the caller holds a dimension
    lock on it — a swapped embed model must be caught at write time, not at recall."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    FakeOllama(embed_dim=768).install(monkeypatch)
    vecs, dim, model = llmclient.embed(["a", "b"])
    assert dim == 768 and len(vecs) == 2 and model == "nomic-embed-text:latest"


def test_embed_wraps_a_bare_string(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    FakeOllama(embed_dim=4).install(monkeypatch)
    vecs, dim, _ = llmclient.embed("just one")
    assert dim == 4 and len(vecs) == 1


def test_models_lists_what_is_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    FakeOllama(tags=["a:1", "b:2"]).install(monkeypatch)
    assert [m["name"] for m in llmclient.models()] == ["a:1", "b:2"]
