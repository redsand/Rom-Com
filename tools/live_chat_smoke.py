"""One-shot live smoke test for the assistant stack against the real Ollama server.

Uses a throwaway temp DB so nothing here can touch romcom.db. Prints the event sequence
and the final answer, then exits — no polling, no server left running.

    PYTHONPATH=".../romcom-readline-stub" python tools/live_chat_smoke.py "your question"
    PYTHONPATH=".../romcom-readline-stub" python tools/live_chat_smoke.py --approve "mark everything as owned"

With `--approve` (or `--decline`) the run continues past the pause: it prints the confirmation
card, records the owned count before and after, then resolves the approval and streams the
model's follow-up. That is the only way to see a *real* model react to `requires_approval` —
the tests can only prove the machinery, not that a model handles the pause sensibly.
"""
import os
import sys
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="romcom-smoke-")
os.environ["ROMCOM_DB"] = os.path.join(tmp, "smoke.db")
os.environ["ROMCOM_CHAT_ENABLED"] = "true"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from romcom import chatagent, chattools, llmclient  # noqa: E402
from romcom.db import connect  # noqa: E402

db = connect()
with db:
    # (title, system, status, wanted, authorized) — smoke-2 is owned but deliberately
    # un-wanted, so `mark_all_owned` has something real to change and the before/after
    # count below can actually differ.
    for i, (title, system, status, wanted, auth) in enumerate([
        ("Super Mario Bros.", "NES", "MISSING", 1, 1),
        ("Metroid", "NES", "DOWNLOADED", 1, 1),
        ("Sonic the Hedgehog", "Genesis", "MISSING", 1, 1),
        ("Zelda II", "NES", "FOUND", 0, 0),
    ]):
        db.execute(
            "INSERT INTO items(id,title,system,authorized,wanted,status) VALUES(?,?,?,?,?,?)",
            (f"smoke-{i}", title, system, auth, wanted, status))

print(f"model         : {llmclient.resolve_model()}")
print(f"tools         : {len(chattools.build_registry())}")
v, dim, m = llmclient.embed(["hello"])
print(f"embed         : {m} -> {dim}-dim (first={v[0][:3]})")

argv = sys.argv[1:]

if argv[:1] == ["--memory"]:
    # Verify the real embedding path and cosine ranking against the live embed model, since
    # that is the part a fake embedder cannot prove. Also prints the memory block that would
    # actually be injected into a turn.
    from romcom import chatstore, chatagent
    chatstore.store_chunk("note", "The SNES collection lives in D:/roms/snes.")
    chatstore.store_chunk("note", "Genesis artwork is kept separately in E:/art/genesis.")
    chatstore.remember_fact("naming", "prefers No-Intro naming")
    print(f"chunks        : {connect().execute('SELECT COUNT(*) c FROM chat_memory_chunks').fetchone()['c']}")
    for q in ["where are my super nintendo roms?", "what naming does he prefer?"]:
        hits = chatstore.recall(q, k=3)
        print(f"\nrecall {q!r}")
        for h in hits:
            print(f"   {h['score']:.3f}  {h['text'][:70]}")
        if not hits:
            print("   (no hit above threshold)")
    block = chatagent._memory_block(None, "where are my super nintendo roms?")
    print(f"\nmemory block  : {len(block)} chars\n{block}")
    sys.exit(0)

decide = None
if argv[:1] == ["--approve"]:
    decide, argv = True, argv[1:]
elif argv[:1] == ["--decline"]:
    decide, argv = False, argv[1:]

question = " ".join(argv) or "how many items are there, and by system?"
print(f"\nquestion      : {question}\n" + "-" * 60)

t0 = time.time()
events = []
cards = []


def emit(name, payload):
    events.append(name)
    if name == "token":
        print(payload["text"], end="", flush=True)
    elif name == "tool_start":
        print(f"\n[{name}] {payload['name']} {payload.get('args')}")
    elif name == "tool_result":
        print(f"\n[{name}] {payload['name']} ok={payload['ok']}")
        print(f"          {str(payload.get('data') or payload.get('error'))[:400]}")
    elif name in ("error", "approval_required"):
        print(f"\n[{name}] {payload}")
    if name == "approval_required":
        cards.append(payload)


def owned():
    """What `mark_all_owned` actually moves: the wanted+authorized flags on owned items."""
    return connect().execute(
        "SELECT COUNT(*) c FROM items WHERE status IN "
        "('FOUND','DOWNLOADED','VERIFIED','NORMALIZED','INSTALLED','TESTED') "
        "AND wanted=1 AND authorized=1").fetchone()["c"]


registry = chattools.build_registry()
out = chatagent.run_turn(None, question, registry, emit, cancel=None)

if out["paused"] and cards:
    card = cards[0]
    print("\n" + "-" * 60)
    print(f"PAUSED        : approval #{card['approval_id']} — nothing has run")
    print(f"summary       : {card['summary']}")
    before = owned()
    print(f"owned before  : {before}")
    if decide is None:
        print("(no --approve/--decline given; leaving it pending, nothing changed)")
    else:
        print(f"\ndeciding      : {'APPROVE' if decide else 'DECLINE'}")
        result = chatagent.resolve_approval(card["approval_id"], decide, registry, emit)
        after = owned()
        print(f"\nowned after   : {after}  ({'changed' if after != before else 'unchanged'})")
        # The resumed turn streams through `emit`, so printing `content` here would repeat
        # the whole answer — the token stream above is already it.
        print(f"final answer  : {len(result['content']) if result else 0} chars, streamed above")

print("\n" + "-" * 60)
print(f"events        : {events}")
print(f"wall          : {time.time() - t0:.1f}s")
