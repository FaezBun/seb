#!/usr/bin/env python3
"""axl auth-svc: login page + session gate + proxy auth for Caddy forward_auth.

Serves the static login page, validates a single shared password, issues an
HMAC-signed session cookie, enforces per-IP rate limiting and a blocklist that
fail2ban keeps updated. Standard library only (no pip).
"""

import base64
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import mimetypes
import os
import posixpath
import secrets
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- config ----

def load_env(path):
    env = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

ENV = load_env(os.environ.get("AXL_ENV", "/etc/axl-login/env"))

def cfg(key, default=""):
    return os.environ.get(key) or ENV.get(key, default)

BIND = cfg("AXL_BIND", "127.0.0.1")
PORT = int(cfg("AXL_PORT", "9091"))
LOGIN_DIR = cfg("AXL_LOGIN_DIR", "/var/www/html")
LOG_FILE = cfg("AXL_LOG", "/var/log/axl-login.log")
BLOCKLIST = cfg("AXL_BLOCKLIST", "/etc/axl-login/blocklist.txt")
NOTIFY_CMD = cfg("AXL_NOTIFY", "/usr/local/sbin/axl-notify.sh")
SEEN_FILE = cfg("AXL_SEEN", "/var/lib/axl-login/seen_ips.txt")
PASSWORD_HASH = cfg("AXL_PASSWORD_HASH", "")
SECRET = cfg("AXL_SECRET", "")
SESSION_TTL = int(cfg("AXL_SESSION_TTL", "43200"))      # 12h
MAX_FAIL = int(cfg("AXL_MAX_FAIL", "5"))
FAIL_WINDOW = int(cfg("AXL_FAIL_WINDOW", "600"))         # 10 min
CSRF_TTL = int(cfg("AXL_CSRF_TTL", "1800"))              # 30 min
COOKIE_SECURE = cfg("AXL_COOKIE_SECURE", "true").lower() == "true"
SESSION_COOKIE = "axl_session"

# ----------------------------------------------------------------- utils ----

def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        sys.stderr.write(line)

def verify_password(password):
    if not PASSWORD_HASH:
        return False
    try:
        algo, salt_hex, hash_hex = PASSWORD_HASH.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=2 ** 14, r=8, p=1, dklen=len(expected))
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False

def make_session():
    exp = int(time.time()) + SESSION_TTL
    rand = secrets.token_hex(8)
    payload = "%d.%s" % (exp, rand)
    sig = hmac.new(SECRET.encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(("%s.%s" % (payload, sig)).encode()).decode()

def check_session(token):
    if not token or not SECRET:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit(".", 1)
        exp_s = payload.split(".", 1)[0]
        expected = hmac.new(SECRET.encode("utf-8"), payload.encode("utf-8"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        return int(exp_s) > time.time()
    except (ValueError, TypeError):
        return False

# Stateless CSRF token: HMAC-signed timestamp. No cookie needed, so it cannot
# break when cookies are blocked/expired. Cross-site attackers cannot forge a
# valid signature and cannot read the token (same-origin policy).
def make_csrf():
    ts = int(time.time())
    msg = "csrf.%d" % ts
    sig = hmac.new(SECRET.encode("utf-8"), msg.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return "%d.%s" % (ts, sig)

def check_csrf(token):
    if not token or not SECRET:
        return False
    try:
        ts_s, sig = token.split(".", 1)
        ts = int(ts_s)
    except (ValueError, AttributeError):
        return False
    if time.time() - ts > CSRF_TTL:
        return False
    expected = hmac.new(SECRET.encode("utf-8"), ("csrf.%d" % ts).encode("utf-8"),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)

# client IP tracked through the Cloudflare tunnel
def _norm_ip(value):
    # Sahkan betul-betul IP; tolak nilai sampah supaya fail2ban tak tersalah ban.
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return ""
    if addr.version == 6 and addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    return str(addr)

def client_ip(handler):
    for key in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        val = handler.headers.get(key)
        if val:
            ip = _norm_ip(val.split(",")[0])
            if ip:
                return ip
    return _norm_ip(handler.client_address[0]) or handler.client_address[0]

def is_blocked(ip):
    try:
        with open(BLOCKLIST, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip() == ip:
                    return True
    except FileNotFoundError:
        pass
    return False

# ------------------------------------------------------- notify/visitors ---

SEEN = None

def notify(text):
    # Hantar ke Telegram (bot seb) secara async; jangan lengahkan respons login.
    try:
        subprocess.Popen([NOTIFY_CMD, text],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)
    except OSError:
        pass

def is_public(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local
                or a.is_reserved or a.is_multicast or a.is_unspecified)

def _load_seen():
    global SEEN
    if SEEN is None:
        SEEN = set()
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as fh:
                SEEN = {ln.strip() for ln in fh if ln.strip()}
        except OSError:
            pass
    return SEEN

def note_visitor(ip, ua, path):
    if path not in ("/login", "/login/", "/auth"):
        return
    if not is_public(ip):
        return
    seen = _load_seen()
    if ip in seen:
        return
    seen.add(ip)
    try:
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        with open(SEEN_FILE, "a", encoding="utf-8") as fh:
            fh.write(ip + "\n")
    except OSError:
        pass
    ua = (ua or "-").replace("\n", " ").replace("\r", " ")[:120]
    log("NEW-IP ip=%s path=%s ua=%s" % (ip, path, ua))
    notify("\U0001F310 IP LUAR BARU: %s\npath: %s\nUA: %s" % (ip, path, ua))

# --------------------------------------------------------------- limiting ---

FAILS = {}  # ip -> [timestamps]

def recent_fails(ip):
    now = time.time()
    lst = [t for t in FAILS.get(ip, []) if now - t < FAIL_WINDOW]
    FAILS[ip] = lst
    return lst

def record_fail(ip):
    lst = recent_fails(ip)
    lst.append(time.time())
    FAILS[ip] = lst
    return len(lst)

# ------------------------------------------------------------------ pages ---

def read_login_page(csrf, error=""):
    path = os.path.join(LOGIN_DIR, "index.html")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
    except OSError:
        html = "<h1>Login</h1><form method=post action=/login>" \
               "<input name=password type=password>" \
               "<input type=hidden name=csrf value=\"__CSRF__\">" \
               "<button>Sign in</button></form>__ERROR__"
    if not error:
        err_html = ""
    else:
        err_html = "<div class=\"axl-error\">%s</div>" % error
    return (html.replace("__CSRF__", csrf)
                .replace("__ERROR__", err_html))

def read_error_page():
    try:
        with open(os.path.join(LOGIN_DIR, "404.html"), "rb") as fh:
            return fh.read()
    except OSError:
        return b"<h1>404 Not Found</h1>"

# --------------------------------------------------------------- handler ----

class Handler(BaseHTTPRequestHandler):
    server_version = "axl-auth"

    def log_message(self, *args):
        pass

    # -- helpers -----------------------------------------------------------
    def send(self, code, body=b"", ctype="text/html; charset=utf-8",
             headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def set_cookie(self, name, value, max_age, httponly=True):
        parts = ["%s=%s" % (name, value), "Path=/", "SameSite=Lax",
                 "Max-Age=%d" % max_age]
        if httponly:
            parts.append("HttpOnly")
        if COOKIE_SECURE:
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def cookies(self):
        raw = self.headers.get("Cookie", "")
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            pass
        return {k: m.value for k, m in jar.items()}

    def asset(self, rel):
        rel = posixpath.normpath(rel).lstrip("/")
        if rel.startswith(".."):
            self.send(404, read_error_page())
            return
        full = os.path.join(LOGIN_DIR, rel)
        if not os.path.isfile(full):
            self.send(404, read_error_page())
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            data = fh.read()
        self.send(200, data, ctype)

    # -- routing -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        ip = client_ip(self)
        path = urlparse(self.path).path
        note_visitor(ip, self.headers.get("User-Agent", ""), path)

        if is_blocked(ip):
            log("BLOCKED ip=%s path=%s" % (ip, path))
            self.send(403, read_error_page())
            return

        if path in ("/login", "/login/"):
            self.send(200, read_login_page(make_csrf()))
            return

        if path.startswith("/login/"):
            self.asset(path[len("/login/"):])
            return

        if path == "/auth":
            if check_session(self.cookies().get(SESSION_COOKIE)):
                self.send(200, "ok", "text/plain")
            else:
                self.send(401, "unauthorized", "text/plain")
            return

        if path == "/logout":
            self.send(302, b"", headers=[
                ("Location", "/login"),
                self.set_cookie(SESSION_COOKIE, "", 0),
            ])
            return

        if path == "/":
            self.send(302, b"", headers=[("Location", "/login")])
            return

        if path.startswith(("/css/", "/js/", "/images/", "/fonts/", "/assets/")):
            self.asset(path.lstrip("/"))
            return

        if path == "/__404":
            self.send(404, read_error_page())
            return

        self.send(404, read_error_page())

    def do_POST(self):
        ip = client_ip(self)
        path = urlparse(self.path).path
        note_visitor(ip, self.headers.get("User-Agent", ""), path)

        if is_blocked(ip):
            log("BLOCKED ip=%s path=%s" % (ip, path))
            self.send(403, read_error_page())
            return

        if path not in ("/login", "/login/"):
            self.send(404, read_error_page())
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 8192:
            self.send(400, read_error_page())
            return
        body = self.rfile.read(length).decode("utf-8", "replace")
        form = parse_qs(body)
        password = (form.get("password") or [""])[0]
        username = (form.get("username") or [""])[0]
        csrf = (form.get("csrf") or [""])[0]

        if not check_csrf(csrf):
            log("LOGIN-FAIL ip=%s user=%s reason=csrf" % (ip, username))
            record_fail(ip)
            self.send(400, read_login_page(make_csrf(),
                      "Your session expired. Please reload the page and try again."))
            return

        if verify_password(password):
            log("LOGIN-OK ip=%s user=%s" % (ip, username))
            notify("\u2705 LOGIN BERJAYA: %s\nuser: %s" % (ip, username or "-"))
            q = parse_qs(urlparse(self.path).query)
            nxt = (q.get("next") or ["/"])[0]
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            self.send(302, b"", headers=[
                ("Location", nxt),
                self.set_cookie(SESSION_COOKIE, make_session(), SESSION_TTL),
            ])
            return

        n = record_fail(ip)
        log("LOGIN-FAIL ip=%s user=%s" % (ip, username))
        if n >= MAX_FAIL:
            log("RATE-LIMIT ip=%s" % ip)
            self.send(429, read_login_page(make_csrf(),
                      "Too many attempts. Please try again later."))
            return
        self.send(401, read_login_page(make_csrf(), "Incorrect password."))


def main():
    if not SECRET:
        sys.stderr.write("AXL_SECRET missing\n")
        sys.exit(1)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    log("START bind=%s:%d" % (BIND, PORT))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
