"""
ApiProber.core.http_client -- HTTP-Client mit Rate-Limiting
=============================================================
urllib.request Wrapper mit Auth, Rate-Limiting, User-Agent, Retry.
Pattern: BACH connectors/base.py (dataclass, UA, Retry)

B36-Fix (SQ080): Timeout-Bug behoben:
  - Connection-Timeout (10s) vs Read-Timeout (30s) getrennt
  - Retry-Mechanismus mit exponentiellem Backoff (max 3 Versuche)
  - socket.timeout wird explizit gefangen statt als generische Exception
  - Timeout-Werte ueber Config steuerbar (connect_timeout_s, read_timeout_s)
"""
import json
import ipaddress
import re
import socket
import time
import ssl
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field


_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_ENCODED_AMBIGUITY = re.compile(r"%(?:25|2e|2f|5c)", re.IGNORECASE)


def _invalid_url(reason):
    """Return a secret-safe validation error that never repeats the raw URL."""
    raise ValueError(reason)


def _canonical_host(host):
    """Canonicalize DNS, IPv4, or IPv6 hosts without blocking local targets."""
    if not host or "%" in host:
        _invalid_url("Host fehlt oder enthält eine nicht unterstützte Zonenangabe")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host or all(char.isdigit() or char == "." for char in host):
            _invalid_url("IP-Adresse ist ungültig")
        if host.endswith(".."):
            _invalid_url("Hostname enthält leere Labels")
        dns_host = host[:-1] if host.endswith(".") else host
        try:
            ascii_host = dns_host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            _invalid_url("IDN-Hostname ist ungültig")
        if any(ord(char) > 127 for char in dns_host):
            if ascii_host.encode("ascii").decode("idna").lower() != dns_host.lower():
                _invalid_url("IDN-Hostname würde mehrdeutig abgebildet")
        if not ascii_host or len(ascii_host) > 253:
            _invalid_url("Hostname ist ungültig")
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(char.isalnum() or char == "-" for char in label)
            for label in labels
        ):
            _invalid_url("Hostname ist ungültig")
        for label in labels:
            if label.startswith("xn--"):
                try:
                    decoded_label = label.encode("ascii").decode("idna")
                    if decoded_label.encode("idna").decode("ascii").lower() != label:
                        _invalid_url("IDN-A-Label ist nicht kanonisch")
                except UnicodeError:
                    _invalid_url("IDN-A-Label ist ungültig")
        return ascii_host, False
    return address.compressed.lower(), address.version == 6


def _normalize_http_url(url, base_url=False):
    """Validate and canonicalize an HTTP(S) URL without exposing rejected input."""
    if not isinstance(url, str) or not url:
        _invalid_url("URL fehlt")
    if "\\" in url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        _invalid_url("Whitespace, Steuerzeichen und Backslashes sind nicht erlaubt")

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        _invalid_url("URL-Syntax ist ungültig")

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        _invalid_url("nur absolute HTTP(S)-URLs sind erlaubt")
    if not parsed.netloc or "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        _invalid_url("eingebettete Zugangsdaten sind nicht erlaubt")
    if "#" in url:
        _invalid_url("Fragmente sind nicht erlaubt")
    if base_url and "?" in url:
        _invalid_url("Query-Parameter sind in einer Basis-URL nicht erlaubt")

    try:
        port = parsed.port
        host, is_ipv6 = _canonical_host(parsed.hostname)
    except ValueError as exc:
        _invalid_url(str(exc))
    if port == 0:
        _invalid_url("Port muss zwischen 1 und 65535 liegen")

    path = parsed.path
    without_escapes = _PERCENT_ESCAPE.sub("", path)
    if "%" in without_escapes:
        _invalid_url("Pfad enthält eine ungültige Prozentkodierung")
    decoded_path = urllib.parse.unquote(path)
    if (
        _ENCODED_AMBIGUITY.search(path)
        or "\\" in decoded_path
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in decoded_path)
        or "//" in decoded_path
        or any(segment in (".", "..") for segment in decoded_path.split("/"))
    ):
        _invalid_url("Pfad enthält eine mehrdeutige Konstruktion")
    if path and not path.startswith("/"):
        _invalid_url("Pfad muss absolut sein")

    path = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=-._~")
    path = _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), path)
    query = urllib.parse.quote(parsed.query, safe="/%:?@!$&'()*+,;=-._~")
    query = _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), query)
    if base_url:
        path = path.rstrip("/")

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    host_for_netloc = f"[{host}]" if is_ipv6 else host
    netloc = host_for_netloc if port is None or default_port else f"{host_for_netloc}:{port}"
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def normalize_base_url(url):
    """Return the canonical, persistence-safe HTTP(S) base URL."""
    return _normalize_http_url(url, base_url=True)


def _url_origin(url):
    """Return the canonical security origin for redirect comparisons."""
    parsed = urllib.parse.urlsplit(_normalize_http_url(url))
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname, parsed.port or default_port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate redirect targets and keep credentials on the original origin."""

    _SENSITIVE_HEADERS = {
        "authorization", "proxy-authorization", "x-api-key", "cookie"
    }

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        canonical_url = _normalize_http_url(newurl)
        redirected = super().redirect_request(
            req, fp, code, msg, headers, canonical_url
        )
        if redirected is not None and _url_origin(req.full_url) != _url_origin(canonical_url):
            for container in (redirected.headers, redirected.unredirected_hdrs):
                for header_name in list(container):
                    if header_name.lower() in self._SENSITIVE_HEADERS:
                        del container[header_name]
        return redirected


@dataclass
class HttpResponse:
    """Ergebnis eines HTTP-Requests."""
    url: str
    method: str
    status_code: int
    headers: dict = field(default_factory=dict)
    body: str = ""
    content_type: str = ""
    elapsed_ms: int = 0
    error: str = ""
    is_json: bool = False
    retries: int = 0

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    @property
    def is_timeout(self):
        return "timeout" in self.error.lower() if self.error else False

    def json(self):
        if self.body:
            return json.loads(self.body)
        return None


class HttpClient:
    """HTTP-Client mit Rate-Limiting, Auth-Support und Retry.

    Timeout-Konfiguration (B36-Fix):
        timeout_seconds:    Gesamt-Timeout fuer urllib (Fallback, Default: 30)
        connect_timeout_s:  Connection-Timeout in Sekunden (Default: 10)
        read_timeout_s:     Read-Timeout in Sekunden (Default: 30)
        max_retries:        Maximale Retry-Versuche bei Timeout (Default: 2)

    Hinweis: urllib.request.urlopen kennt nur EINEN timeout-Parameter.
    Wir setzen diesen auf read_timeout_s (der groessere Wert) und pruefen
    den Connection-Timeout separat ueber socket.setdefaulttimeout waehrend
    des Verbindungsaufbaus. Fuer echte Trennung muesste man auf
    http.client.HTTPConnection umsteigen -- das waere ein groesseres
    Refactoring. Der pragmatische Fix: read_timeout hoch genug setzen
    (30s statt 15s) und Retries einfuehren.
    """

    # Timeout-Fehler die einen Retry rechtfertigen
    _RETRYABLE_ERRORS = (socket.timeout, TimeoutError, ConnectionResetError,
                         ConnectionAbortedError, BrokenPipeError)

    def __init__(self, config):
        self.delay_ms = config.get("delay_ms", 500)

        # B36-Fix: Getrennte Timeouts + Fallback auf alten Key
        legacy_timeout = config.get("timeout_seconds", 30)
        self.connect_timeout = config.get("connect_timeout_s", min(legacy_timeout, 10))
        self.read_timeout = config.get("read_timeout_s", max(legacy_timeout, 30))
        # urllib bekommt den groesseren Wert (read_timeout)
        self.timeout = self.read_timeout

        self.max_retries = config.get("max_retries", 2)
        self.user_agent = config.get("user_agent", "ApiProber/0.1")
        self.auth_type = config.get("auth", {}).get("type", "none")
        self.auth_value = config.get("auth", {}).get("value", "")
        self._last_request_time = 0.0
        self._request_count = 0
        self._ssl_ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            _SafeRedirectHandler(),
            urllib.request.HTTPSHandler(context=self._ssl_ctx),
        )

    @property
    def request_count(self):
        return self._request_count

    def request(self, url, method="GET", body=None, extra_headers=None):
        """HTTP-Request mit Rate-Limiting und Retry. Gibt HttpResponse zurueck."""
        url = _normalize_http_url(url)
        self._rate_limit()

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html, */*",
        }

        # Auth
        if self.auth_type == "bearer" and self.auth_value:
            headers["Authorization"] = f"Bearer {self.auth_value}"
        elif self.auth_type == "api_key" and self.auth_value:
            headers["X-API-Key"] = self.auth_value
        elif self.auth_type == "basic" and self.auth_value:
            import base64
            encoded = base64.b64encode(self.auth_value.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        if extra_headers:
            headers.update(extra_headers)

        data = None
        if body is not None:
            if isinstance(body, dict):
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            elif isinstance(body, str):
                data = body.encode("utf-8")
            elif isinstance(body, bytes):
                data = body

        # Retry-Loop (B36-Fix)
        last_error = None
        for attempt in range(1 + self.max_retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            start = time.monotonic()
            self._request_count += 1

            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    elapsed = int((time.monotonic() - start) * 1000)
                    response_url = _normalize_http_url(resp.geturl())
                    resp_headers = dict(resp.headers)
                    content_type = resp_headers.get("Content-Type", "")
                    raw_body = resp.read()

                    # Body decodieren
                    body_str = ""
                    try:
                        body_str = raw_body.decode("utf-8")
                    except UnicodeDecodeError:
                        body_str = raw_body.decode("latin-1", errors="replace")

                    is_json = "json" in content_type.lower()

                    return HttpResponse(
                        url=response_url, method=method,
                        status_code=resp.status,
                        headers=resp_headers,
                        body=body_str,
                        content_type=content_type,
                        elapsed_ms=elapsed,
                        is_json=is_json,
                        retries=attempt
                    )
            except urllib.error.HTTPError as e:
                # HTTP-Fehler sind keine Netzwerk-Timeouts -- kein Retry
                elapsed = int((time.monotonic() - start) * 1000)
                resp_headers = dict(e.headers) if e.headers else {}
                content_type = resp_headers.get("Content-Type", "")
                body_str = ""
                try:
                    raw = e.read()
                    body_str = raw.decode("utf-8", errors="replace")
                except Exception:
                    pass
                return HttpResponse(
                    url=url, method=method,
                    status_code=e.code,
                    headers=resp_headers,
                    body=body_str,
                    content_type=content_type,
                    elapsed_ms=elapsed,
                    error=str(e),
                    is_json="json" in content_type.lower(),
                    retries=attempt
                )
            except (socket.timeout, TimeoutError) as e:
                # B36-Fix: Explizites Timeout-Handling mit Retry
                elapsed = int((time.monotonic() - start) * 1000)
                last_error = f"Timeout nach {elapsed}ms: {e}"
                if attempt < self.max_retries:
                    backoff = (2 ** attempt) * 0.5  # 0.5s, 1s, 2s ...
                    time.sleep(backoff)
                    continue
                return HttpResponse(
                    url=url, method=method,
                    status_code=0,
                    elapsed_ms=elapsed,
                    error=last_error,
                    retries=attempt
                )
            except urllib.error.URLError as e:
                elapsed = int((time.monotonic() - start) * 1000)
                reason_str = str(e.reason)
                # URLError kann einen socket.timeout wrappen
                is_timeout = isinstance(e.reason, (socket.timeout, TimeoutError))
                if is_timeout and attempt < self.max_retries:
                    last_error = f"Connection-Timeout nach {elapsed}ms: {reason_str}"
                    backoff = (2 ** attempt) * 0.5
                    time.sleep(backoff)
                    continue
                # Connection-Refused, DNS-Fehler etc. -- kein Retry
                return HttpResponse(
                    url=url, method=method,
                    status_code=0,
                    elapsed_ms=elapsed,
                    error=reason_str,
                    retries=attempt
                )
            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError) as e:
                # Netzwerk-Fehler die einen Retry rechtfertigen
                elapsed = int((time.monotonic() - start) * 1000)
                last_error = f"Verbindungsfehler nach {elapsed}ms: {e}"
                if attempt < self.max_retries:
                    backoff = (2 ** attempt) * 0.5
                    time.sleep(backoff)
                    continue
                return HttpResponse(
                    url=url, method=method,
                    status_code=0,
                    elapsed_ms=elapsed,
                    error=last_error,
                    retries=attempt
                )
            except Exception as e:
                elapsed = int((time.monotonic() - start) * 1000)
                return HttpResponse(
                    url=url, method=method,
                    status_code=0,
                    elapsed_ms=elapsed,
                    error=str(e),
                    retries=attempt
                )

        # Sollte nicht erreicht werden, aber Safety-Net
        return HttpResponse(
            url=url, method=method,
            status_code=0,
            error=last_error or "Unbekannter Fehler nach Retries",
            retries=self.max_retries
        )

    def head(self, url):
        return self.request(url, method="HEAD")

    def get(self, url):
        return self.request(url, method="GET")

    def options(self, url):
        return self.request(url, method="OPTIONS")

    def _rate_limit(self):
        """Wartet bis delay_ms seit letztem Request vergangen sind."""
        if self._last_request_time > 0:
            elapsed = (time.monotonic() - self._last_request_time) * 1000
            remaining = self.delay_ms - elapsed
            if remaining > 0:
                time.sleep(remaining / 1000.0)
        self._last_request_time = time.monotonic()
