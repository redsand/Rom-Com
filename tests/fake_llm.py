"""A scripted fake Ollama, so the chat stack is testable with no model installed and no
network — which is the only way CI can stay green.

It replaces `romcom.llmclient.requests` wholesale rather than stubbing `llmclient.chat`,
so the real code path is exercised: NDJSON assembly, the tool-call accumulator, usage
extraction, model resolution, and the overflow retry all run for real against frames that
look exactly like the ones a live 0.34.0 daemon produced.

Frames are built the way the real server sends them — **`arguments` as an already-parsed
dict**, not a JSON string — because that is the detail that is easiest to get wrong.
"""
import json

from romcom import llmclient

DEFAULT_TAGS = ["gemma4:12b", "qwen3:14b", "nomic-embed-text:latest"]
DEFAULT_CAPS = {"gemma4:12b": ["completion", "tools", "thinking"],
                "qwen3:14b": ["completion", "tools", "thinking"],
                "nomic-embed-text:latest": ["embedding"]}


class FakeResponse:
    def __init__(self, frames=None, status=200, body=None, text=""):
        self.frames = frames or []
        self.status_code = status
        self._json = body
        self.text = text
        self.reason = "OK" if status < 400 else "Error"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode=False):
        for f in self.frames:
            line = json.dumps(f)
            yield line.decode() if (decode_unicode is False and isinstance(line, bytes)) else line


class FakeRequests:
    def __init__(self, fake):
        self.fake = fake

    def post(self, url, json=None, stream=False, timeout=None):
        return self.fake.handle_post(url, json or {}, stream)

    def get(self, url, timeout=None):
        return self.fake.handle_get(url)


class FakeOllama:
    """`turns` is consumed in order, one entry per /api/chat call.

    A turn spec is a dict: `tokens`, `thinking` (lists of strings to stream), `tool_calls`
    (`[{"name":…, "arguments":{…}, "id":…}]`), `usage` (merged into the final frame), or
    `status`/`error` to make the request fail instead. A spec may also be `{"raw": [...]}`
    for frames written literally, which is how the parsing tests pin odd shapes.
    """

    def __init__(self, turns=None, tags=None, capabilities=None, embed_dim=768):
        self.turns = list(turns or [])
        self.tags = list(tags if tags is not None else DEFAULT_TAGS)
        self.capabilities = dict(capabilities if capabilities is not None else DEFAULT_CAPS)
        self.embed_dim = embed_dim
        self.calls = []      # (url, body) for every /api/chat, in order
        self.gets = []

    def install(self, monkeypatch):
        monkeypatch.setattr(llmclient, "requests", FakeRequests(self))
        return self

    # ---------------------------------------------------------------- responses

    def _frames(self, spec):
        if "raw" in spec:
            return spec["raw"]
        out = []
        for t in spec.get("thinking") or []:
            out.append({"model": "fake", "message": {"role": "assistant", "thinking": t}, "done": False})
        for t in spec.get("tokens") or []:
            out.append({"model": "fake", "message": {"role": "assistant", "content": t}, "done": False})
        for tc in spec.get("tool_calls") or []:
            out.append({"model": "fake", "done": False, "message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": tc.get("id") or "call_1",
                                "function": {"index": 0, "name": tc["name"],
                                             # a dict, exactly as Ollama sends it
                                             "arguments": tc.get("arguments") or {}}}]}})
        final = {"model": "fake", "done": True, "message": {"role": "assistant", "content": ""}}
        final.update(spec.get("usage") or {})
        out.append(final)
        return out

    def handle_post(self, url, body, stream):
        if url.endswith("/api/tags"):
            return FakeResponse(body={"models": [{"name": n, "size": 1} for n in self.tags]})
        if url.endswith("/api/show"):
            return FakeResponse(body={"capabilities": self.capabilities.get(body.get("model"), [])})
        if url.endswith("/api/embed"):
            n = len(body.get("input") or [])
            return FakeResponse(body={"embeddings": [[0.1] * self.embed_dim for _ in range(n)]})
        if url.endswith("/api/chat"):
            self.calls.append((url, body))
            spec = self.turns.pop(0) if self.turns else {"tokens": ["(no scripted turn)"]}
            if spec.get("status"):
                return FakeResponse(status=spec["status"],
                                    body={"error": spec.get("error", "error")},
                                    text=spec.get("error", ""))
            if not stream:
                frames = self._frames(spec)
                merged = {"model": "fake", "message": {"role": "assistant", "content": ""}}
                for f in frames:
                    msg = f.get("message") or {}
                    merged["message"]["content"] += msg.get("content") or ""
                    merged["message"]["thinking"] = (merged["message"].get("thinking") or "") + (msg.get("thinking") or "")
                    if msg.get("tool_calls"):
                        merged["message"]["tool_calls"] = msg["tool_calls"]
                    if f.get("done"):
                        merged.update({k: v for k, v in f.items() if k not in ("message", "done")})
                return FakeResponse(body=merged)
            return FakeResponse(frames=self._frames(spec))
        raise AssertionError(f"unexpected POST {url}")

    def handle_get(self, url):
        self.gets.append(url)
        if url.endswith("/api/tags"):
            return FakeResponse(body={"models": [{"name": n, "size": 1} for n in self.tags]})
        raise AssertionError(f"unexpected GET {url}")

    # ---------------------------------------------------------------- assertions

    def tool_names_sent(self, i=0):
        """The tool names offered to the model on call `i` — lets a test assert the catalog
        was actually advertised rather than assuming it."""
        body = self.calls[i][1]
        return [t["function"]["name"] for t in (body.get("tools") or [])]


def ndjson(*frames):
    """Raw NDJSON lines, for tests that need to pin parsing rather than a full turn."""
    return {"raw": list(frames)}
