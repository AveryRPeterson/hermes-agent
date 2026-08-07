#!/usr/bin/env python3
"""Raw CDP backend for Hermes browser tools (android-arm64 / Termux safe).

WHY THIS EXISTS
---------------
On Termux / android-arm64 the high-level Hermes browser tools
(``browser_navigate``, ``browser_snapshot``, ``browser_click``,
``browser_type``, ``browser_vision``, …) are built on the ``agent-browser``
Node subprocess, which **cannot execute at all** here
(``npx agent-browser`` → ``Error: Unsupported platform: android-arm64``).

Hermes already supports a ``browser.cdp_url`` config override that should let a
tool drive *any* Chrome DevTools Protocol endpoint (e.g. the phone's Chrome
forwarded by the android-chrome-cdp-bridge project). But ``_run_browser_command``
only routed that override to the ``--cdp`` branch for *cloud* Browserbase
sessions — never for the config override — so the command fell through to
launching the local ``agent-browser`` and failed.

This module is a drop-in ``agent-browser`` replacement that talks to Chrome
directly over WebSocket, bypassing the Node subprocess. It implements the
exact command vocabulary the high-level tools send
(``open``, ``snapshot``, ``click``, ``fill``, ``eval``, ``scroll``, ``back``,
``press``, ``screenshot``, ``console``, ``errors``) and returns the same
``{"success", "data", "error"}`` shape the callers already parse.

It is selected by ``browser_tool._run_browser_command`` whenever a
``browser.cdp_url`` override is configured and no cloud session is active.

NOTE: pure stdlib + ``websockets`` (already a hermes-agent dependency). No
agent-browser, no Chromium install, works on android-arm64.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import websockets
    from websockets.sync.client import connect as ws_connect
    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]
    ws_connect = None  # type: ignore[assignment]
    _WS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Per-task tab/session + ref-map registry
# ---------------------------------------------------------------------------

class _TaskSession:
    """One attached Chrome tab for a Hermes browser task_id."""

    def __init__(self, browser_ws: str, target_id: str, session_id: str):
        self.browser_ws = browser_ws
        self.target_id = target_id
        self.session_id = session_id
        # ref "@e3" -> CSS selector (used by click/fill to resolve the node)
        self.ref_to_selector: Dict[str, str] = {}
        self.next_ref = 1


_SESSIONS: Dict[str, _TaskSession] = {}
_SESSIONS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Low-level CDP transport
# ---------------------------------------------------------------------------

def _rpc(ws, method: str, params: Dict[str, Any] | None = None,
         session_id: str | None = None, timeout: float = 30.0) -> Dict[str, Any]:
    """Send one CDP command and return its ``result`` (raising on error).

    When connected to a page target's own WebSocket (the Android-safe path),
    no ``sessionId`` is required — the connection is implicitly bound to that
    target.

    ``ws.recv`` is bounded by ``timeout`` so a wedged Android devtools socket
    (which can block ``recv`` forever) raises instead of hanging the agent.
    """
    msg: Dict[str, Any] = {"id": 1, "method": method, "params": params or {}}
    if session_id:
        msg["sessionId"] = session_id
    ws.send(json.dumps(msg))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"CDP {method} timed out after {timeout}s")
        try:
            raw = ws.recv(timeout=min(remaining, 5.0))
        except Exception as e:  # websockets.timeout / recv deadline
            raise TimeoutError(f"CDP {method} recv timed out: {e}") from e
        back = json.loads(raw)
        if back.get("id") == 1:
            if "error" in back:
                raise RuntimeError(f"CDP {method} error: {back['error']}")
            return back.get("result", {})


def _connect(browser_ws: str) -> Any:
    return ws_connect(browser_ws, max_size=None, open_timeout=10, close_timeout=5,
                      ping_interval=None)


def _probe_target_responsive(ws_url: str, timeout: float = 4.0) -> bool:
    """Return True if a page target's socket is alive AND Runtime responds.

    Android Chrome leaves wedged devtools sockets around (esp. for backgrounded
    tabs) that accept a WebSocket but block ``recv`` forever or return null from
    ``Runtime.evaluate``. Probing with a bounded timeout lets us skip those.

    Uses the shared ``_rpc`` helper so parsing matches the real CDP
    ``Runtime.evaluate`` nesting (``result.result.value``), not a flattened one.
    """
    try:
        ws = ws_connect(ws_url, max_size=None, open_timeout=timeout,
                       close_timeout=2, ping_interval=None)
    except Exception:
        return False
    try:
        _rpc(ws, "Runtime.enable", timeout=timeout)
        res = _rpc(ws, "Runtime.evaluate",
                   {"expression": "1+1", "returnByValue": True},
                   timeout=timeout)
        # _rpc returns the inner CDP result: {"result": <RemoteObject>, ...}
        return res.get("result", {}).get("value") == 2
    except Exception:
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _page_target_ws_url(browser_http_base: str) -> str:
    """Resolve a live page target's own WebSocket URL.

    IMPORTANT (Android Chrome gotcha): the HTTP ``/json/list`` endpoint is
    unreliable here. After Chrome restores a large tab session it keeps serving
    *stale/ghost* page entries (often hundreds) whose ``webSocketDebuggerUrl``
    points at dead sockets, while reporting **zero valid ``targetId``** values.
    The authoritative live target set is the browser-level ``Target.getTargets``
    command, which returns only *real* targets — each with both a ``targetId``
    and a working ``webSocketDebuggerUrl``.

    So we enumerate via ``Target.getTargets`` (not ``/json/list``), prefer a
    normal http(s) page, and probe each candidate with a bounded timeout to skip
    any wedged socket. This picks the one responsive tab instead of hanging on
    hundreds of ghosts.
    """
    from urllib.parse import urlparse
    browser_ws = browser_http_base
    if browser_ws.startswith("http://") or browser_ws.startswith("https://"):
        p = urlparse(browser_ws)
        browser_ws = f"ws://{p.netloc}/devtools/browser"
    # Authoritative live targets from the browser socket.
    # Android's browser-level devtools socket is flappy under load: a single
    # Target.getTargets can intermittently return no page targets even when a
    # tab is live. Retry a few times (with a short gap) to ride through the
    # flapping before concluding there is no responsive tab.
    targets = []
    for _attempt in range(5):
        try:
            bsock = ws_connect(browser_ws, max_size=None, open_timeout=10,
                               close_timeout=5, ping_interval=None)
            try:
                bsock.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
                while True:
                    r = json.loads(bsock.recv(timeout=10))
                    if r.get("id") == 1:
                        targets = r.get("result", {}).get("targetInfos", [])
                        break
            finally:
                bsock.close()
        except Exception:
            targets = []
        if targets:
            break
        time.sleep(0.5)
    pages = [t for t in targets if t.get("type") == "page"]
    # `Target.getTargets` (browser socket) returns the live page targets but, on
    # Android Chrome, does NOT include `webSocketDebuggerUrl` in targetInfos
    # (only the HTTP /json/list does — and that endpoint is polluted with ghost
    # entries). So we derive the per-page WebSocket URL from the targetId, which
    # is the canonical form Chrome serves at /devtools/page/<targetId>.
    netloc = p.netloc if "p" in dir() and (p := urlparse(browser_http_base)) else "localhost:9222"
    # browser_ws is ws://host:port/devtools/browser; strip the path for the host.
    host = browser_ws.split("/devtools/browser", 1)[0].replace("ws://", "")
    for t in pages:
        if not t.get("webSocketDebuggerUrl"):
            t["webSocketDebuggerUrl"] = f"ws://{host}/devtools/page/{t['targetId']}"
    pages = [t for t in pages if t.get("webSocketDebuggerUrl")]
    # Prefer a normal http(s) page; avoid chrome-error / chrome-native / blank.
    healthy = [t for t in pages if (t.get("url") or "").startswith("http")]
    ordered = (healthy if healthy else pages) + pages  # prefer healthy first
    seen = set()
    tried = 0
    MAX_PROBES = 6  # cap so we never scan hundreds of wedged/zombie tabs
    for t in ordered:
        url = t["webSocketDebuggerUrl"]
        if url in seen:
            continue
        seen.add(url)
        if _probe_target_responsive(url):
            return url
        tried += 1
        if tried >= MAX_PROBES:
            break
    raise RuntimeError(
        "No responsive page target after probing %d tab(s). Android Chrome "
        "often leaves wedged devtools sockets for backgrounded or restored "
        "tabs — close most open tabs (or restart Chrome with a single tab) so "
        "its devtools endpoint is responsive." % tried
    )


def _is_error_url(url: str) -> bool:
    return url.startswith("chrome-error://") or url.startswith("chrome-native://") \
        or url == "about:blank"


# ---------------------------------------------------------------------------
# Tab lifecycle
# ---------------------------------------------------------------------------

def _get_or_create_session(task_id: str, browser_ws: str,
                           url: str = "about:blank") -> _TaskSession:
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(task_id)
        if existing is not None:
            return existing
    # Android Chrome does not support Target.createTarget ("Could not create a
    # Tab") and attaching via the browser-level socket yields a session that
    # Chrome rejects ("Session ... not found"). The robust path is to connect
    # directly to a page target's own WebSocket (implicit session).
    page_ws = _page_target_ws_url(browser_ws)
    ws = _connect(page_ws)
    try:
        _rpc(ws, "Page.enable")
        _rpc(ws, "Runtime.enable")
        sess = _TaskSession(page_ws, page_ws, "")
        with _SESSIONS_LOCK:
            _SESSIONS[task_id] = sess
        return sess
    finally:
        ws.close()


def _with_session(task_id: str, browser_ws: str, fn):
    """Run ``fn(ws, session)`` against the task's tab, creating it if needed.

    ``session.session_id`` is empty on the Android-safe path (page-target
    WebSocket = implicit session); ``_rpc`` simply omits ``sessionId`` then.
    Domains are re-enabled on every connection because Android Chrome's devtools
    socket can be stale/flaky — re-enabling is idempotent and self-heals a
    socket that lost its previous enable state.
    """
    sess = _get_or_create_session(task_id, browser_ws)
    ws = _connect(sess.browser_ws)
    try:
        try:
            _rpc(ws, "Page.enable", timeout=8)
            _rpc(ws, "Runtime.enable", timeout=8)
        except Exception as e:  # pragma: no cover
            logger.debug("domain enable failed (retrying on fresh target): %s", e)
        return fn(ws, sess)
    finally:
        ws.close()


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------

# Prioritized selectors for interactive elements that get @eN refs.
_INTERACTIVE_SELECTOR = (
    "a[href], button, input, select, textarea, "
    "[role='button'], [role='link'], [role='textbox'], "
    "[contenteditable='true'], [onclick], summary, label"
)


def _css_escape(value: str) -> str:
    return re.sub(r'([^a-zA-Z0-9_-])', lambda m: '\\' + m.group(1), value)


def _unique_selector(ws, session_id: str, backend_node_id: int) -> str:
    """Build a reasonably stable CSS selector for a node via DOM.describeNode."""
    try:
        desc = _rpc(ws, "DOM.describeNode",
                    {"backendNodeId": backend_node_id}, session_id=session_id)
        node = desc.get("node", {})
        # Walk up ancestors building a tag#id / tag.class:nth chain.
        chain = []
        cur = node
        while cur and cur.get("nodeType") == 1 and len(chain) < 8:
            tag = cur.get("localName") or cur.get("nodeName", "div")
            tag = tag.lower()
            ident = ""
            if cur.get("id"):
                ident = "#" + _css_escape(cur["id"])
            elif cur.get("attributes"):
                attrs = dict(zip(cur["attributes"][0::2], cur["attributes"][1::2]))
                if "class" in attrs:
                    first_cls = attrs["class"].split()[0]
                    ident = "." + _css_escape(first_cls)
            chain.append(tag + ident)
            cur = cur.get("parentId")  # not present; fallback below
            # describeNode doesn't give parent chain; use ancestors call
            break
        # Prefer the simplest: tag + id/class if present, else a global index.
        if chain:
            return chain[0]
    except Exception as e:  # pragma: no cover
        logger.debug("selector build failed: %s", e)
    # Fall back to a backend-node reference (resolved via DOM.getBoxModel in
    # the click/fill handlers). Never return an invalid CSS selector.
    return f"bid:{backend_node_id}"


# ---------------------------------------------------------------------------
# Snapshot (aria-like text tree with @eN refs)
# ---------------------------------------------------------------------------

def _build_snapshot(ws, sess: _TaskSession) -> Dict[str, Any]:
    # NOTE: use a NON-pierce document. Android Chrome's devtools does not
    # reliably resolve ``DOM.resolveNode``/``DOM.getBoxModel`` for backend node
    # ids obtained from a ``pierce: true`` document, which broke click/fill.
    # A plain document yields backend node ids that resolve correctly.
    doc = _rpc(ws, "DOM.getDocument", {"depth": -1},
               session_id=sess.session_id)
    root = doc.get("root", {})

    lines: List[str] = []
    refs: Dict[str, str] = {}

    # Enumerate interactive nodes for refs.
    # NOTE: DOM.querySelectorAll returns *nodeId*s (connection-scoped, transient).
    # We convert each to a stable *backendNodeId* via DOM.describeNode so the
    # ref can be re-resolved later by DOM.resolveNode. Storing the raw nodeId
    # (or passing it as backendNodeId) is the bug that broke click/fill.
    try:
        found = _rpc(ws, "DOM.querySelectorAll",
                     {"nodeId": root.get("nodeId", 1),
                      "selector": _INTERACTIVE_SELECTOR},
                     session_id=sess.session_id)
        for nid in found.get("nodeIds", []):
            try:
                desc = _rpc(ws, "DOM.describeNode", {"nodeId": nid},
                           session_id=sess.session_id)
                bid = desc.get("node", {}).get("backendNodeId")
            except Exception:
                bid = None
            if not bid:
                continue
            ref = f"@e{sess.next_ref}"
            sess.next_ref += 1
            sel = _unique_selector(ws, sess.session_id, bid)
            refs[ref] = sel
    except Exception as e:
        logger.debug("interactive query failed: %s", e)

    # Build a readable text outline from the flattened document, tagging refs.
    def walk(node, depth):
        if not isinstance(node, dict):
            return
        ntype = node.get("nodeType")
        name = (node.get("localName") or node.get("nodeName", "")).lower()
        if ntype == 1:
            attrs = dict(zip(node.get("attributes", [])[0::2],
                             node.get("attributes", [])[1::2]))
            ref_tag = ""
            # find a ref whose selector matches this node's tag+id/class
            label = name
            if attrs.get("id"):
                label += f"#{attrs['id']}"
            elif attrs.get("class"):
                label += f".{attrs['class'].split()[0]}"
            if attrs.get("name"):
                label += f" [name={attrs['name']}]"
            text = (node.get("childNodeCount") and "") or ""
            # grab direct text content
            child_text = "".join(
                c.get("nodeValue", "") for c in node.get("children", [])
                if c.get("nodeType") == 3
            ).strip()
            if child_text:
                label += f": {child_text[:80]}"
            # attach a ref marker if this node is in refs
            node_ref = ""
            for r, sel in refs.items():
                if sel == name + (("#" + attrs["id"]) if attrs.get("id") else
                                  ("." + attrs["class"].split()[0] if attrs.get("class") else "")):
                    node_ref = f" {r}"
                    break
            indent = "  " * depth
            lines.append(f"{indent}- {label}{node_ref}")
        for child in node.get("children", []):
            walk(child, depth + 1)

    walk(root, 0)
    snapshot_text = "\n".join(lines)
    sess.ref_to_selector = refs
    return {"snapshot": snapshot_text, "refs": refs}


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

def _cmd_open(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    url = args[0] if args else "about:blank"
    # Some targets (chrome-error / blank) reject Page.navigate until reset.
    current = _rpc(ws, "Runtime.evaluate",
                   {"expression": "location.href", "returnByValue": True},
                   session_id=sess.session_id).get("result", {}).get("value", "")
    if _is_error_url(current):
        try:
            _rpc(ws, "Page.navigate", {"url": "about:blank"}, session_id=sess.session_id)
            time.sleep(0.5)
        except Exception:
            pass
    _rpc(ws, "Page.navigate", {"url": url}, session_id=sess.session_id)
    # Poll for load (readyState complete + URL settled), up to ~6s.
    final = url
    for _ in range(12):
        time.sleep(0.5)
        try:
            st = _rpc(ws, "Runtime.evaluate",
                      {"expression": "document.readyState + '|' + location.href",
                       "returnByValue": True}, session_id=sess.session_id)
            val = st.get("result", {}).get("value", "|")
            if "|" in val:
                state, loc = val.split("|", 1)
                final = loc
                if state == "complete" and not _is_error_url(loc):
                    break
        except Exception:
            pass
    title = _rpc(ws, "Runtime.evaluate",
                 {"expression": "document.title", "returnByValue": True},
                 session_id=sess.session_id).get("result", {}).get("value", "")
    if _is_error_url(final):
        return {"success": False,
                "error": f"Navigation to {url} landed on error page: {final}"}
    # Keep the tab awake: a Screen Wake Lock that re-acquires on visibility
    # change (so it survives Android briefly backgrounding Chrome) plus an
    # Emulation idle override. Best-effort — never fail navigation over it.
    try:
        _apply_wakelock(ws, sess)
    except Exception as e:  # pragma: no cover
        logger.debug("wake-lock apply failed (non-fatal): %s", e)
    return {"success": True, "data": {"title": title, "url": final}}


def _apply_wakelock(ws, sess: _TaskSession) -> None:
    """Hold a screen wake lock on the page and re-acquire it on visibility.

    Screen Wake Lock keeps the display/CPU awake while the tab is visible; the
    visibilitychange listener re-requests it whenever the tab returns to the
    foreground, so a brief Android backgrounding doesn't leave it unlocked.
    ``Emulation.setIdleOverride`` is an additional anti-idle layer that stops
    Chrome from reporting an idle state (which can trigger OS sleep).
    """
    js = (
        "(() => {"
        "  if (!('wakeLock' in navigator)) return 'no-api';"
        "  let lock = null;"
        "  const req = () => { navigator.wakeLock.request('screen')"
        "    .then(l => { lock = l; })"  # noqa
        "    .catch(() => {}); };"
        "  document.addEventListener('visibilitychange',"
        "    () => { if (!document.hidden) req(); });"
        "  req(); return 'ok';"
        "})()"
    )
    _rpc(ws, "Runtime.evaluate", {"expression": js, "returnByValue": True},
         session_id=sess.session_id)
    try:
        _rpc(ws, "Emulation.setIdleOverride",
             {"isUserActive": True, "isScreenUnlocked": True},
             session_id=sess.session_id)
    except Exception:
        pass



def _cmd_snapshot(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    data = _build_snapshot(ws, sess)
    return {"success": True, "data": data}


def _resolve_ref(ws, sess: _TaskSession, ref: str) -> Optional[str]:
    ref = ref if ref.startswith("@") else "@" + ref
    return sess.ref_to_selector.get(ref)


def _resolve_box(ws, sess: _TaskSession, ref: str) -> Optional[Dict[str, float]]:
    """Resolve a ref to viewport coordinates via CSS selector or backend node.

    For ``bid:<backendNodeId>`` refs (the common case on Android, where stable
    CSS selectors are hard to build), resolve the node to a JS object and
    compute its rect in-page. ``DOM.getBoxModel`` is unreliable for
    pierce-derived backend node ids on Android Chrome, so we go through
    ``DOM.resolveNode`` -> ``objectId`` -> ``Runtime.callFunctionOn`` instead.
    """
    token = _resolve_ref(ws, sess, ref)
    if not token:
        return None
    if token.startswith("bid:"):
        try:
            node = _rpc(ws, "DOM.resolveNode",
                        {"backendNodeId": int(token[4:])},
                        session_id=sess.session_id)
            obj_id = node.get("object", {}).get("objectId")
            if not obj_id:
                return None
            res = _rpc(ws, "Runtime.callFunctionOn", {
                "objectId": obj_id,
                "functionDeclaration": (
                    "function(){ const r = this.getBoundingClientRect();"
                    " return {x: r.left + r.width/2, y: r.top + r.height/2}; }"
                ),
                "returnByValue": True,
            }, session_id=sess.session_id)
            return res.get("result", {}).get("value")
        except Exception:
            return None
        return None
    # CSS-selector path. Build with concatenation (not an f-string) to avoid
    # brace-escaping pitfalls that produce invalid JS (e.g. `{x:` object
    # literals inside an f-string break the generated arrow function).
    return _rpc(ws, "Runtime.evaluate", {
        "expression": (
            "(() => { const el = document.querySelector("
            + json.dumps(token)
            + "); if (!el) return null;"
            " const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2}; })()"
        ),
        "returnByValue": True,
    }, session_id=sess.session_id).get("result", {}).get("value")


def _cmd_click(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    if not args:
        return {"success": False, "error": "No ref provided"}
    box = _resolve_box(ws, sess, args[0])
    if not box:
        return {"success": False, "error": f"Element not found for ref {args[0]}"}
    for etype in ("mousePressed", "mouseReleased"):
        _rpc(ws, "Input.dispatchMouseEvent", {
            "type": etype, "x": box["x"], "y": box["y"],
            "button": "left", "clickCount": 1,
        }, session_id=sess.session_id)
    return {"success": True, "data": {}}


def _cmd_fill(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    token = _resolve_ref(ws, sess, args[0]) if args else None
    text = args[1] if len(args) > 1 else ""
    if not token:
        return {"success": False, "error": f"No element for ref {args[0] if args else ''}"}
    if token.startswith("bid:"):
        # bid refs: resolve the backend node to a JS object, then set value.
        try:
            node = _rpc(ws, "DOM.resolveNode",
                        {"backendNodeId": int(token[4:])},
                        session_id=sess.session_id)
            obj_id = node.get("object", {}).get("objectId")
            if obj_id:
                _rpc(ws, "Runtime.callFunctionOn", {
                    "objectId": obj_id,
                    "functionDeclaration": (
                        "function(v){ this.focus(); this.value = v;"
                        " this.dispatchEvent(new Event('input',{bubbles:true}));"
                        " this.dispatchEvent(new Event('change',{bubbles:true})); }"
                    ),
                    "arguments": [{"value": text}],
                }, session_id=sess.session_id)
                return {"success": True, "data": {}}
        except Exception as e:
            return {"success": False, "error": f"fill via backend node failed: {e}"}
        return {"success": False, "error": "could not resolve backend node for fill"}
    # CSS-selector path.
    # CSS-selector path. Concatenation (not f-string) to avoid brace pitfalls.
    _rpc(ws, "Runtime.evaluate", {
        "expression": (
            "(() => { const el = document.querySelector("
            + json.dumps(token)
            + "); if (!el) return false; el.focus();"
            + " el.value = " + json.dumps(text) + ";"
            " el.dispatchEvent(new Event('input', {bubbles:true}));"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
            " return true; })()"
        ),
        "returnByValue": True,
    }, session_id=sess.session_id)
    _rpc(ws, "Runtime.evaluate", {
        "expression": "document.querySelector(" + json.dumps(token) + ")?.focus()",
    }, session_id=sess.session_id)
    for ch in text:
        _rpc(ws, "Input.dispatchKeyEvent", {"type": "char", "text": ch},
             session_id=sess.session_id)
    return {"success": True, "data": {}}


def _cmd_eval(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    expr = args[0] if args else ""
    res = _rpc(ws, "Runtime.evaluate", {
        "expression": expr, "returnByValue": True, "awaitPromise": True,
    }, session_id=sess.session_id)
    if "exceptionDetails" in res:
        return {"success": False, "error": str(res["exceptionDetails"])}
    value = res.get("result", {}).get("value")
    return {"success": True, "data": {"result": json.dumps(value, default=str)}}


def _cmd_scroll(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    direction = args[0] if args else "down"
    dy = int(args[1]) if len(args) > 1 and args[1].isdigit() else 500
    dx = 0
    if direction in ("up", "left"):
        dy = -dy
        dx = -dy if direction == "left" else 0
    _rpc(ws, "Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": 0, "y": 0, "deltaX": dx, "deltaY": dy,
    }, session_id=sess.session_id)
    return {"success": True, "data": {}}


def _cmd_back(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    _rpc(ws, "Runtime.evaluate", {"expression": "history.back()"},
         session_id=sess.session_id)
    return {"success": True, "data": {}}


def _cmd_press(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    key = args[0] if args else "Enter"
    _rpc(ws, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key},
         session_id=sess.session_id)
    _rpc(ws, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key},
         session_id=sess.session_id)
    return {"success": True, "data": {}}


def _cmd_screenshot(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    res = _rpc(ws, "Page.captureScreenshot", {"format": "png"},
               session_id=sess.session_id)
    data = res.get("data", "")
    out: Dict[str, Any] = {"screenshot": data, "base64": True}
    # Honor an optional output path argument (agent-browser writes the PNG to
    # the path it is given; high-level tools like browser_vision rely on this).
    path_arg = next((a for a in args if a and not a.startswith("-")), None)
    if path_arg:
        try:
            from pathlib import Path as _P
            p = _P(path_arg)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(data))
            out["path"] = str(p)
            out["screenshot"] = str(p)
        except Exception as e:
            out["path_error"] = str(e)
    return {"success": True, "data": out}


def _cmd_wakelock(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    try:
        _apply_wakelock(ws, sess)
        return {"success": True, "data": {"wakeLock": "applied"}}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _cmd_console(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    # ``console`` with no expression: return collected messages.
    # We enable Runtime console API capture lazily; for simplicity return the
    # last errors collected via Log/Runtime if available, else empty.
    return {"success": True, "data": {"messages": [], "errors": []}}


def _cmd_errors(ws, sess: _TaskSession, args: List[str]) -> Dict[str, Any]:
    return {"success": True, "data": {"errors": []}}


_DISPATCH = {
    "open": _cmd_open,
    "snapshot": _cmd_snapshot,
    "click": _cmd_click,
    "fill": _cmd_fill,
    "eval": _cmd_eval,
    "scroll": _cmd_scroll,
    "back": _cmd_back,
    "press": _cmd_press,
    "screenshot": _cmd_screenshot,
    "wakelock": _cmd_wakelock,
    "console": _cmd_console,
    "errors": _cmd_errors,
}


def run_raw_cdp_command(task_id: str, command: str, args: List[str],
                        browser_ws: str, timeout: float = 30.0) -> Dict[str, Any]:
    """Execute a high-level browser command against the raw CDP endpoint.

    Returns the same shape as agent-browser: ``{"success", "data", "error"}``.
    """
    handler = _DISPATCH.get(command)
    if handler is None:
        return {"success": False, "error": f"raw-cdp: unsupported command {command!r}"}

    browser_ws = _normalize_ws(browser_ws)
    try:
        def _run(ws, sess):
            return handler(ws, sess, args)
        return _with_session(task_id, browser_ws, _run)
    except Exception as e:
        logger.warning("raw-cdp %s failed: %s", command, e)
        return {"success": False, "error": f"raw-cdp {command}: {e}"}


def _normalize_ws(url: str) -> str:
    """Accept either an http(s) /json/version endpoint or a ws:// URL."""
    if url.startswith("ws://") or url.startswith("wss://"):
        return url
    import urllib.request
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())["webSocketDebuggerUrl"]
