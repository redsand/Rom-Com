"""The service runs behind waitress, which logs nothing on its own."""
import romcom.web as web


def _run(app, environ=None):
    """Drive the middleware as a WSGI server would, capturing what it passes through."""
    seen = {}
    def start_response(status, headers, exc_info=None):
        seen["status"] = status; seen["headers"] = headers
        return lambda _b: None
    body = list(app(environ or {"REMOTE_ADDR": "127.0.0.1", "REQUEST_METHOD": "GET",
                                "PATH_INFO": "/api/jobs/active", "QUERY_STRING": ""},
                    start_response))
    return seen, body


def test_the_access_log_records_each_request(capsys):
    """Worth a test because this log is diagnostic, not decoration: it was the werkzeug
    access log that located the dead chat stream, by showing the browser's polling arriving
    while no /api/chat/stream request ever did."""
    app = web._AccessLog(lambda env, sr: (sr("200 OK", []), [b"x"])[1])
    _run(app)
    line = capsys.readouterr().out.strip()
    assert '"GET /api/jobs/active" 200' in line and line.startswith("127.0.0.1 - - [")


def test_the_query_string_is_kept(capsys):
    app = web._AccessLog(lambda env, sr: (sr("404 NOT FOUND", []), [b""])[1])
    _run(app, {"REMOTE_ADDR": "127.0.0.1", "REQUEST_METHOD": "GET",
               "PATH_INFO": "/api/chat/approvals/9", "QUERY_STRING": "status=pending"})
    assert '"GET /api/chat/approvals/9?status=pending" 404' in capsys.readouterr().out


def test_the_response_passes_through_untouched(capsys):
    """A logger that alters the response is worse than no logger. The chat stream is a
    generator whose framing already broke once; this must stay a pure observer."""
    sentinel = [b"event: start\n", b"data: {}\n\n"]
    app = web._AccessLog(lambda env, sr: (sr("200 OK", [("Content-Type", "text/event-stream")]), sentinel)[1])
    seen, body = _run(app)
    assert body == sentinel
    assert seen["status"] == "200 OK"
    assert seen["headers"] == [("Content-Type", "text/event-stream")]


def test_the_pool_is_big_enough_for_a_held_stream():
    """Waitress defaults to 4 threads and one chat turn holds one for its whole duration,
    including while paused on an approval card. An exhausted pool looks like a hung server,
    so this is pinned rather than left to the default."""
    assert web.SERVE_THREADS >= 8
    # Default channel_timeout is 120s measured from the last byte; a turn waiting on an
    # approval sends nothing meanwhile and would be dropped mid-stream.
    assert web.SERVE_CHANNEL_TIMEOUT > 600
