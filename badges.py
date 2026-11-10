"""Team badges: small square images for our team and opponents.

The phone crops and shrinks every badge to about 128px before upload, so the server only checks and stores.
Images are stored by the SHA-256 of their bytes and served at /badge/<hash>, which never changes, so phones
and browsers can keep them forever (and show them with no signal).

A badge from a link is downloaded by the server, never by the people viewing it, with guards so a pasted
link can't be used to reach machines on the home network: http(s) only, public addresses only (checked on
every redirect, and the connection goes to the address that was checked), 5 second limit, 2 MB limit.
"""
import base64
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import urljoin, urlparse

MAX_STORED = 300 * 1024        # a 128px badge from the phone is far smaller
MAX_FETCH = 2 * 1024 * 1024
FETCH_SECONDS = 5
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


class BadgeError(Exception):
    pass


def image_type(b):
    """The image type from the file's first bytes, or None. SVG is never accepted (it can carry scripts)."""
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def from_data_url(s):
    m = re.match(r"^data:image/[a-z+.-]+;base64,([A-Za-z0-9+/=\s]+)$", str(s or ""))
    if not m:
        raise BadgeError("That isn't an image.")
    try:
        b = base64.b64decode(m.group(1), validate=False)
    except ValueError:
        raise BadgeError("That isn't an image.")
    if len(b) > MAX_STORED:
        raise BadgeError("That image is too big.")
    mime = image_type(b)
    if not mime:
        raise BadgeError("Use a PNG, JPG, WebP or GIF image.")
    return b, mime


def save(c, team_id, b, mime):
    h = hashlib.sha256(b).hexdigest()
    c.execute("INSERT INTO badges VALUES(?,?,?,?,?) ON CONFLICT(hash) DO NOTHING",
              (h, team_id, mime, base64.b64encode(b).decode(), int(time.time())))
    return h


def load(c, h):
    if not HASH_RE.match(h or ""):
        return None
    r = c.execute("SELECT mime, data FROM badges WHERE hash=?", (h,)).fetchone()
    return (base64.b64decode(r["data"]), r["mime"]) if r else None


def exists(c, h):
    return bool(HASH_RE.match(h or "")) and bool(c.execute("SELECT 1 FROM badges WHERE hash=?", (h,)).fetchone())


def color(v):
    v = str(v or "").lower()
    return v if COLOR_RE.match(v) else None


# ---------- downloading a badge from a link ----------
def public_ip(host, port):
    """Resolve host and return one address, only if every address it has is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise BadgeError("Couldn't find that website.")
    ips = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
        raise BadgeError("That link points to a private address, so it can't be used.")
    return str(sorted(ips, key=lambda ip: ip.version)[0])


class _Pinned(http.client.HTTPConnection):
    """Connects to the address that was checked, not whatever the name resolves to a moment later."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(url):
    """Download an image from a public http(s) link. Returns (bytes, mime)."""
    deadline = time.time() + FETCH_SECONDS
    for _ in range(4):  # the link plus up to 3 redirects
        u = urlparse(str(url or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            raise BadgeError("Paste a link that starts with http:// or https://")
        if u.username or u.password:
            raise BadgeError("Links with a username or password can't be used.")
        port = u.port or (443 if u.scheme == "https" else 80)
        ip = public_ip(u.hostname, port)
        left = deadline - time.time()
        if left <= 0:
            raise BadgeError("That website took too long to answer.")
        conn = (_PinnedTLS if u.scheme == "https" else _Pinned)(u.hostname, ip, port, left)
        try:
            conn.request("GET", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                         headers={"User-Agent": "TouchlineTracker/1 (badge)", "Accept": "image/*"})
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
                url = urljoin(url, r.getheader("Location"))
                continue
            if r.status != 200:
                raise BadgeError(f"Couldn't load that image (the website said {r.status}).")
            b = r.read(MAX_FETCH + 1)
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise BadgeError("Couldn't load that image.")
        finally:
            conn.close()
        if len(b) > MAX_FETCH:
            raise BadgeError("That image is over 2 MB.")
        mime = image_type(b)
        if not mime:
            raise BadgeError("That link isn't a PNG, JPG, WebP or GIF image.")
        return b, mime
    raise BadgeError("That link redirects too many times.")
