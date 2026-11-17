#!/usr/bin/env python3
"""A fake signal-cli for the tests: JSON-RPC 2.0 over stdin/stdout like `signal-cli ... jsonRpc`.

No network. State lives in the --config directory:
  calls.jsonl    every request received ({"method", "params"}), one per line, plus {"argv": [...]} at start
  account.json   the linked account ({"number", "aci"}); written by finishLink, read by listAccounts
  scan           create it to "scan" the QR code: finishLink waits for it (FAKE_SIGNAL_AUTOSCAN=1: no wait)
  inbox.jsonl    envelopes to deliver: after subscribeReceive each new line is sent as a receive notification
  refuse         when present, startLink answers with an error
Unknown methods answer error -32601, like signal-cli.
"""
import json
import os
import pathlib
import sys
import threading
import time

NUMBER = "+15551234567"
ACI = "11111111-2222-4333-8444-555555555555"

args = sys.argv[1:]
conf = pathlib.Path(args[args.index("--config") + 1])
conf.mkdir(parents=True, exist_ok=True)
calls = conf / "calls.jsonl"
out_lock = threading.Lock()
subscribed = threading.Event()


def log(entry):
    with open(calls, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def deliver():
    subscribed.wait()
    sent = 0
    inbox = conf / "inbox.jsonl"
    while True:
        if inbox.exists():
            lines = inbox.read_text(encoding="utf-8").splitlines()
            for line in lines[sent:]:
                if line.strip():
                    acc = json.loads((conf / "account.json").read_text())["number"] if (conf / "account.json").exists() else None
                    send({"jsonrpc": "2.0", "method": "receive",
                          "params": {"subscription": 0, "result": {"envelope": json.loads(line), "account": acc}}})
            sent = len(lines)
        time.sleep(0.05)


def handle(req):
    method, params = req.get("method"), req.get("params") or {}
    if method == "startLink":
        if (conf / "refuse").exists():
            raise RuntimeError("refused")
        return {"deviceLinkUri": "sgnl://linkdevice?uuid=FAKE-uuid_1&pub_key=BQ%2Bfake%2Fkey%3D"}
    if method == "finishLink":
        if not os.environ.get("FAKE_SIGNAL_AUTOSCAN"):
            while not (conf / "scan").exists():
                time.sleep(0.05)
        acc = {"number": NUMBER, "aci": ACI}
        (conf / "account.json").write_text(json.dumps(acc))
        return acc
    if method == "listAccounts":
        p = conf / "account.json"
        return [json.loads(p.read_text())] if p.exists() else []
    if method == "subscribeReceive":
        subscribed.set()
        return 0
    if method in ("unsubscribeReceive", "listContacts", "listGroups"):
        return []
    return None


def main():
    log({"argv": args})
    sys.stderr.write(f"INFO  Starting for {NUMBER} ({ACI})\n")
    sys.stderr.flush()
    threading.Thread(target=deliver, daemon=True).start()
    known = {"startLink", "finishLink", "listAccounts", "subscribeReceive", "unsubscribeReceive", "listContacts",
             "listGroups"}
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        log({"method": req.get("method"), "params": req.get("params")})
        if req.get("method") not in known:
            send({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": "Method not implemented"}})
            continue

        def run(req=req):
            try:
                send({"jsonrpc": "2.0", "id": req.get("id"), "result": handle(req)})
            except Exception as e:  # noqa: BLE001
                send({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -1, "message": str(e)}})

        threading.Thread(target=run, daemon=True).start()  # finishLink blocks; others still answer


if __name__ == "__main__":
    main()
