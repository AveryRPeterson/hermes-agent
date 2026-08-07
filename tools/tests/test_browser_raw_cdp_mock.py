#!/usr/bin/env python3
"""Deterministic verification of tools/browser_raw_cdp.py logic.

Mocks the CDP WebSocket transport so the handlers are exercised end-to-end
against scripted CDP responses -- independent of the flaky Android Chrome
devtools socket. Proves the patch's command logic is correct.
"""
import json
import sys

sys.path.insert(0, "/data/data/com.termux/files/home/.hermes/hermes-agent")
# Must import the module *after* we can monkeypatch ws_connect; import now:
import tools.browser_raw_cdp as mod


# --------------------------------------------------------------------------
# Fake WebSocket transport
# --------------------------------------------------------------------------
class FakeWS:
    def __init__(self, responder):
        self.responder = responder
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self, timeout=None):
        msg = self.sent.pop(0)
        return json.dumps(self.responder(msg))

    def close(self):
        pass


def page_responder(msg):
    method = msg.get("method")
    eid = msg["id"]
    if method == "Page.enable":
        return {"id": eid, "result": {}}
    if method == "Runtime.enable":
        return {"id": eid, "result": {}}
    if method == "Page.navigate":
        return {"id": eid, "result": {}}
    if method == "Page.captureScreenshot":
        return {"id": eid, "result": {"data": "BASE64PNGDATA"}}
    if method == "DOM.getDocument":
        return {"id": eid, "result": {"root": {
            "nodeId": 1, "nodeType": 1, "localName": "html", "nodeName": "HTML",
            "attributes": [], "childNodeCount": 1,
            "children": [{
                "nodeId": 2, "nodeType": 1, "localName": "body", "nodeName": "BODY",
                "attributes": [], "childNodeCount": 1,
                "children": [{
                    "nodeId": 3, "nodeType": 1, "localName": "a", "nodeName": "A",
                    "attributes": ["href", "https://example.com/foo"],
                    "childNodeCount": 0,
                    "children": [{"nodeId": 4, "nodeType": 3,
                                  "nodeValue": "Click me", "children": []}]
                }]
            }]
        }}}
    if method == "DOM.querySelectorAll":
        return {"id": eid, "result": {"nodeIds": [3]}}
    if method == "DOM.describeNode":
        # Must include backendNodeId so the snapshot stores a resolvable ref.
        return {"id": eid, "result": {"node": {
            "nodeType": 1, "localName": "a", "nodeName": "A",
            "backendNodeId": 3,
            "attributes": ["href", "https://example.com/foo"], "parentId": 2}}}
    if method == "DOM.resolveNode":
        return {"id": eid, "result": {"object": {
            "type": "object", "subtype": "node", "className": "HTMLAnchorElement",
            "objectId": "MOCKOBJ1"}}}
    if method == "Runtime.callFunctionOn":
        # If computing a bounding rect, return a box; otherwise return true.
        expr = (msg.get("params") or {}).get("functionDeclaration", "")
        if "getBoundingClientRect" in expr:
            return {"id": eid, "result": {"result": {"value": {"x": 10, "y": 20}}}}
        return {"id": eid, "result": {"result": {"value": True}}}
    if method == "Runtime.evaluate":
        expr = (msg.get("params") or {}).get("expression", "")
        if expr == "1+1":
            return {"id": eid, "result": {"result": {"value": 2}}}
        if expr == "location.href":
            return {"id": eid, "result": {"result": {"value": "https://example.com"}}}
        if expr.startswith("document.readyState"):
            return {"id": eid, "result": {"result": {"value": "complete|https://example.com"}}}
        if expr == "document.title":
            return {"id": eid, "result": {"result": {"value": "Example Domain"}}}
        if "getBoundingClientRect" in expr:
            return {"id": eid, "result": {"result": {"value": {"x": 10, "y": 20}}}}
        if "history.back" in expr:
            return {"id": eid, "result": {"result": {"value": None}}}
        return {"id": eid, "result": {"result": {"value": None}}}
    if method in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent"):
        return {"id": eid, "result": {}}
    return {"id": eid, "result": {}}


def browser_responder(msg):
    eid = msg["id"]
    if msg.get("method") == "Target.getTargets":
        return {"id": eid, "result": {"targetInfos": [
            {"type": "page", "url": "chrome-native://newtab/", "targetId": "nat1"},
            {"type": "page", "url": "https://example.com/", "targetId": "ex1",
             "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/EX1"},
        ]}}
    return {"id": eid, "result": {}}


def fake_ws_connect(url, **kwargs):
    if str(url).endswith("/devtools/browser"):
        return FakeWS(browser_responder)
    return FakeWS(page_responder)


mod.ws_connect = fake_ws_connect


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def run():
    BWS = "ws://localhost:9222/devtools/browser"
    passed = 0
    failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}")

    # open
    r = mod.run_raw_cdp_command("t1", "open", ["https://example.com"], BWS)
    check("open success", r.get("success") is True)
    check("open returns title", r.get("data", {}).get("title") == "Example Domain")

    # snapshot
    r = mod.run_raw_cdp_command("t1", "snapshot", [], BWS)
    snap = r.get("data", {}).get("snapshot", "")
    refs = r.get("data", {}).get("refs", {})
    check("snapshot success", r.get("success") is True)
    check("snapshot has text", len(snap) > 0)
    check("snapshot produced @eN refs", len(refs) > 0)

    # eval
    r = mod.run_raw_cdp_command("t1", "eval", ["document.title"], BWS)
    check("eval success", r.get("success") is True)
    check("eval returns Example Domain",
          "Example Domain" in (r.get("data", {}).get("result", "") or ""))

    # click (uses ref from snapshot)
    ref = next(iter(refs))
    r = mod.run_raw_cdp_command("t1", "click", [ref], BWS)
    check("click success", r.get("success") is True)

    # fill
    r = mod.run_raw_cdp_command("t1", "fill", [ref, "hello"], BWS)
    check("fill success", r.get("success") is True)

    # screenshot
    r = mod.run_raw_cdp_command("t1", "screenshot", [], BWS)
    check("screenshot success", r.get("success") is True)
    check("screenshot base64", len(r.get("data", {}).get("screenshot", "")) > 0)

    # scroll
    r = mod.run_raw_cdp_command("t1", "scroll", ["down"], BWS)
    check("scroll success", r.get("success") is True)

    # back
    r = mod.run_raw_cdp_command("t1", "back", [], BWS)
    check("back success", r.get("success") is True)

    # press
    r = mod.run_raw_cdp_command("t1", "press", ["Enter"], BWS)
    check("press success", r.get("success") is True)

    # unsupported command
    r = mod.run_raw_cdp_command("t1", "bogus", [], BWS)
    check("unsupported command reported", r.get("success") is False)

    print(f"\n=== {passed}/{passed + failed} checks passed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
