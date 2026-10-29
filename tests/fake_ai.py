"""Fake AI servers for tests: an Anthropic Messages API (the real SDK is pointed at it with base_url) and an
OpenAI-compatible local model server. Both record every request. Never contacts the real services."""
import http.server
import json
import threading
import urllib.parse


class _Fake:
    def __init__(self):
        self.requests = []  # dicts: method, path, query, headers, body
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                parts = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                req = {"method": self.command, "path": parts.path, "query": urllib.parse.parse_qs(parts.query),
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                with fake.lock:
                    fake.requests.append(req)
                status, data, extra = fake.answer(req)
                out = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = _any

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeAnthropic(_Fake):
    """POST /v1/messages (beta calls carry the anthropic-beta header). Set .output (dict, sent as the JSON text block), .stop_reason,
    .category (for refusals), .status (error status), .reject_beta (400 naming fallbacks on beta calls)."""

    def __init__(self):
        self.output = {"summary": "A calm day.", "replies": []}
        self.text = None  # raw text block instead of .output
        self.stop_reason = "end_turn"
        self.category = None
        self.status = 200
        self.reject_beta = False
        self.answer_model = None
        super().__init__()

    def env(self):
        return {"CALLSHEET_ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.port}"}

    def messages(self):
        return [r for r in self.requests if r["path"] == "/v1/messages"]

    def answer(self, req):
        quick = {"retry-after-ms": "1"}
        if req["method"] != "POST" or req["path"] != "/v1/messages":
            return 404, {"type": "error", "error": {"type": "not_found_error", "message": "Not found"}}, None
        beta = req["query"].get("beta") == ["true"] or "anthropic-beta" in req["headers"]
        if self.reject_beta and beta:
            return 400, {"type": "error", "error": {"type": "invalid_request_error",
                                                    "message": "fallbacks: Extra inputs are not permitted"}}, None
        if self.status != 200:
            kind = {401: "authentication_error", 429: "rate_limit_error", 500: "api_error",
                    529: "overloaded_error"}.get(self.status, "invalid_request_error")
            return self.status, {"type": "error", "error": {"type": kind, "message": "nope"}}, quick
        body = req["body"] or {}
        text = self.text if self.text is not None else json.dumps(self.output)
        resp = {"id": "msg_fake", "type": "message", "role": "assistant",
                "model": self.answer_model or body.get("model"),
                "content": [] if self.stop_reason == "refusal" else [{"type": "text", "text": text}],
                "stop_reason": self.stop_reason, "stop_sequence": None,
                "usage": {"input_tokens": 3120, "output_tokens": 410}}
        if self.stop_reason == "refusal":
            resp["stop_details"] = {"type": "refusal", "category": self.category, "explanation": None}
        return 200, resp, None


class FakeLocal(_Fake):
    """OpenAI-compatible: GET /v1/models, POST /v1/chat/completions. .contents: queue of message contents
    (the last one repeats). .status: error status for every request. Like llama.cpp with a thinking model,
    every answer also carries message.reasoning_content; .finish: finish_reason."""

    def __init__(self):
        self.contents = ['{"summary": "Local summary.", "replies": []}']
        self.models = ["qwen3-coder"]
        self.status = 200
        self.finish = "stop"
        super().__init__()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def chats(self):
        return [r for r in self.requests if r["path"] == "/v1/chat/completions"]

    def answer(self, req):
        if self.status != 200:
            return self.status, {"error": {"message": "nope"}}, None
        if req["method"] == "GET" and req["path"] == "/v1/models":
            return 200, {"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]}, None
        if req["method"] == "POST" and req["path"] == "/v1/chat/completions":
            content = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
            return 200, {"id": "c1", "object": "chat.completion", "model": (req["body"] or {}).get("model"),
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
                                                              "reasoning_content": '{"summary": "THINKING"}'},
                                      "finish_reason": self.finish}],
                         "usage": {"prompt_tokens": 900, "completion_tokens": 120}}, None
        return 404, {"error": {"message": "not found"}}, None
