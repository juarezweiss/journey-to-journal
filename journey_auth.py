"""Ephemeral authentication using Journey's existing Google authorization.

No OAuth client, cookie/profile access, environment secrets, or credential files.
"""
import getpass
import http.server
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


class AuthError(Exception):
    pass


def clean_token(value):
    if not isinstance(value, str):
        raise AuthError("Invalid credential input.")
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value or len(value) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise AuthError("Invalid credential input.")
    return value


def prompt_token():
    # getpass otherwise falls back to echoing stdin; fail closed instead.
    if not sys.stdin.isatty():
        raise AuthError("Authentication requires a local interactive terminal.")
    return clean_token(getpass.getpass("Journey Google Drive bearer token (hidden): "))


def browser_token(origin="https://journey.cloud", timeout=180):
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or parsed.hostname is None
            or not (parsed.hostname == "journey.cloud" or parsed.hostname.endswith(".journey.cloud"))
            or parsed.netloc != parsed.hostname or parsed.path or parsed.query or parsed.fragment):
        raise AuthError("Browser origin must be an exact HTTPS Journey origin, without a path.")
    nonce = secrets.token_urlsafe(32)
    result = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_one_request(self):
            self.connection.settimeout(5)
            try:
                super().handle_one_request()
            except (OSError, ValueError):
                pass

        def reply(self, status, body=b"", content_type="text/plain"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            return self.path == "/" + nonce and self.headers.get("Host") == host

        def do_GET(self):
            if not self.allowed():
                return self.reply(404)
            # Token travels through postMessage, never a URL, console, cookie or storage.
            page = """<!doctype html><meta charset=utf-8><title>Journey local authentication</title>
<p id=s>Waiting for a connection from Journey…</p><script>
(() => {
 const expected = ORIGIN;
 const status = document.getElementById('s');
 let connected = false;
 const handler = async e => {
  if(e.origin !== expected || e.source !== window.opener) return;
  if(e.data?.type === 'journey-export-status') {
   connected = true;
   const states = {
    'no-app': 'Connected to Journey. App root unavailable: select the main Journey frame in Console.',
    'no-pinia': 'Connected to Journey. Token cache unavailable. Open an existing entry or photo to observe a Drive request.',
    'empty': 'Connected to Journey. No cached Drive token. Open an existing uncached entry or photo from the Google Drive journal.',
    'multiple': 'Connected to Journey. Multiple cached Drive tokens; use the hidden terminal fallback to select the intended account.',
    'found': 'Connected to Journey. Credential found; transferring locally.'
   };
   if(states[e.data.state]) status.textContent = states[e.data.state];
   return;
  }
  if(e.data?.type !== 'journey-export-token' || typeof e.data.token !== 'string') return;
  clearInterval(hello);
  window.removeEventListener('message', handler);
  try {
   const r = await fetch(location.pathname, {method:'POST', credentials:'omit', cache:'no-store',
    headers:{'Content-Type':'text/plain'}, body:e.data.token});
   document.getElementById('s').textContent = r.ok ? 'Authorization received locally. Close this window.' : 'Authorization rejected.';
   if(r.ok) window.opener.postMessage('journey-export-received', expected);
  } catch { document.getElementById('s').textContent = 'Local handoff failed.'; }
 };
 window.addEventListener('message', handler);
 const hello = setInterval(() => {
  if(window.opener) window.opener.postMessage('journey-export-ready', expected);
 }, 500);
 setTimeout(() => {
  if(!window.opener) status.textContent = 'Browser disconnected the popup opener. Use --auth prompt in Terminal.';
  else if(!connected) status.textContent = 'No response from the Journey snippet. Check the Console for an error; the wrong browser frame or an expired snippet may be selected.';
 }, 5000);
 setTimeout(() => { clearInterval(hello); status.textContent = 'Local handoff expired. Close this window and rerun the local command.'; }, 180000);
})();
</script>""".replace("ORIGIN", json.dumps(origin))
            self.reply(200, page.encode(), "text/html; charset=utf-8")

        def do_POST(self):
            if not self.allowed() or self.headers.get("Origin") != local_origin:
                return self.reply(403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    return self.reply(400)
                token = clean_token(self.rfile.read(size).decode("ascii"))
            except (ValueError, UnicodeError, AuthError):
                return self.reply(400)
            result.append(token)
            self.reply(200, b"OK")

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        host = "127.0.0.1:" + str(server.server_port)
        local_origin = "http://" + host
        template = Path(__file__).with_name("browser_handoff.js").read_text()
        snippet = template.replace("__LOCAL_URL__", json.dumps(local_origin + "/" + nonce))
        snippet = snippet.replace("__JOURNEY_ORIGIN__", json.dumps(origin))
        print("On the signed-in Journey tab, run this snippet in DevTools Console.\n"
              "It contains no Google credential. Allow the local popup.\n" + snippet, flush=True)
        server.timeout = 0.5
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise AuthError("Browser handoff timed out. Try --auth prompt; see README.")
    return result.pop()
