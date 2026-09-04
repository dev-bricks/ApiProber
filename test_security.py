"""Security regressions for credential and probe-target boundaries."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.request
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent
PARENT = str(ROOT.parent)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from ApiProber import api_prober  # noqa: E402
from ApiProber.core.config import DEFAULT_CONFIG, load_config, save_config, set_config_value  # noqa: E402
from ApiProber.core.database import Database  # noqa: E402
from ApiProber.core.http_client import (  # noqa: E402
    HttpClient,
    _SafeRedirectHandler,
    normalize_base_url,
)
from ApiProber.discovery.orchestrator import ProbeOrchestrator  # noqa: E402
from ApiProber.export.json_export import export_json  # noqa: E402
from ApiProber.export.markdown import export_markdown  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://example.com", "http://example.com"),
        ("HTTPS://Example.COM/", "https://example.com"),
        ("http://example.com:80/api/", "http://example.com/api"),
        ("https://example.com:443/api", "https://example.com/api"),
        ("https://example.com:8443/api/v1", "https://example.com:8443/api/v1"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://[2001:0db8::1]:8080/api", "http://[2001:db8::1]:8080/api"),
        ("https://münich.example/api", "https://xn--mnich-kva.example/api"),
        ("https://example.com./café", "https://example.com/caf%C3%A9"),
        ("http://localhost", "http://localhost"),
    ],
)
def test_normalize_base_url_accepts_decided_host_forms(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:password@example.com",
        "https://user@example.com",
        "https://user%40name@example.com",
        "file:///etc/passwd",
        "ftp://example.com",
        "example.com/api",
        "//example.com/api",
        "https:///missing-host",
        "https://example.com/api?token=value",
        "https://example.com/api?",
        "https://example.com/api#fragment",
        "https://example.com/api#",
        "https://example.com/api\nadmin",
        "https://example.com/api\\admin",
        "https://example.com/api//admin",
        "https://example.com/api/../admin",
        "https://example.com/api/%2e%2e/admin",
        "https://example.com/api/%252e%252e/admin",
        "https://example.com/api%2fadmin",
        "https://example.com:0",
        "https://example.com:65536",
        "https://999.999.999.999",
        "http://2001:db8::1/api",
        "https://example.com..",
        "https://example.com...",
        "https://faß.de",
        "https://xn--a.example",
        "https://xn--abc.example",
        "https://xn--not-valid.example",
        "https://xn--fa-hia.de",
    ],
)
def test_normalize_base_url_rejects_unsafe_or_ambiguous_forms(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:canary@example.com",
        "file:///private/canary",
        "example.com/canary",
        "https://example.com/path#canary",
        "https://example.com/path\ncanary",
    ],
)
def test_invalid_probe_has_no_request_database_or_raw_url(raw, tmp_path, capsys, monkeypatch):
    config = deepcopy(DEFAULT_CONFIG)
    db_path = tmp_path / "must-not-exist.db"
    config["db_path"] = str(db_path)
    orchestrator = ProbeOrchestrator(config)

    def unexpected_request(*args, **kwargs):
        raise AssertionError("invalid target reached the request boundary")

    monkeypatch.setattr(orchestrator.client, "request", unexpected_request)
    result = orchestrator.probe(raw)
    captured = capsys.readouterr()

    assert result.get("error")
    assert not db_path.exists()
    assert raw not in captured.out
    assert raw not in captured.err


def test_http_client_rejects_non_http_before_urlopen(monkeypatch):
    client = HttpClient(deepcopy(DEFAULT_CONFIG))

    def unexpected_urlopen(*args, **kwargs):
        raise AssertionError("unsafe scheme reached urllib")

    monkeypatch.setattr("ApiProber.core.http_client.urllib.request.urlopen", unexpected_urlopen)
    with pytest.raises(ValueError):
        client.get("file:///private/data")
    assert client.request_count == 0


@pytest.mark.parametrize("source", ["local", "environment"])
def test_config_show_redacts_resolved_canary(source, tmp_path, monkeypatch, capsys):
    canary = "CANARY-" + uuid.uuid4().hex
    config_path = tmp_path / "config.json"
    config_path.write_text('{}\n', encoding="utf-8")
    monkeypatch.delenv("APIPROBER_AUTH_VALUE", raising=False)
    if source == "local":
        (tmp_path / "config.local.json").write_text(
            json.dumps({"auth": {"type": "bearer", "value": canary}}), encoding="utf-8"
        )
    else:
        monkeypatch.setenv("APIPROBER_AUTH_VALUE", canary)

    resolved = load_config(config_path)
    monkeypatch.setattr("ApiProber.core.config.load_config", lambda: resolved)
    args = SimpleNamespace(show=True, set_auth=False, key=None, value=None)
    assert api_prober.cmd_config(args) == 0
    captured = capsys.readouterr()

    assert canary not in captured.out
    assert canary not in captured.err
    assert json.loads(captured.out)["auth"]["value"] == "***REDACTED***"


def test_deprecated_argv_secret_paths_fail_without_echo_or_writes(tmp_path, capsys, monkeypatch):
    canary = "CANARY-" + uuid.uuid4().hex
    monkeypatch.setattr("ApiProber.core.config.BASE_DIR", tmp_path)
    probe_args = SimpleNamespace(
        auth_value=canary,
        auth_prompt=False,
        url="https://example.com",
        depth=None,
        delay_ms=None,
        max_requests=None,
        auth_type="bearer",
        test_all_methods=False,
    )
    assert api_prober.cmd_probe(probe_args) == 2
    config_args = SimpleNamespace(show=False, set_auth=False, key="auth.value", value=canary)
    assert api_prober.cmd_config(config_args) == 2
    captured = capsys.readouterr()

    assert canary not in captured.out
    assert canary not in captured.err
    assert not any(tmp_path.iterdir())


def test_cli_rejects_legacy_secret_and_invalid_target_without_artifacts(tmp_path):
    canary = "CANARY-" + uuid.uuid4().hex
    copied_root = tmp_path / "ApiProber"
    shutil.copytree(
        ROOT,
        copied_root,
        ignore=shutil.ignore_patterns(".git", "data", "exports", "build", "dist", "__pycache__"),
    )
    commands = [
        [sys.executable, "api_prober.py", "probe", "https://example.com", "--auth-value", canary],
        [sys.executable, "api_prober.py", "config", "--set", "auth.value", canary],
        [sys.executable, "api_prober.py", "probe", f"https://user:{canary}@example.com"],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=copied_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert canary not in result.stdout
        assert canary not in result.stderr
        assert not (copied_root / "data").exists()
        assert not (copied_root / "exports").exists()


def test_hidden_prompt_supplies_secret_without_output(monkeypatch, capsys):
    canary = "CANARY-" + uuid.uuid4().hex

    class TtyInput:
        @staticmethod
        def isatty():
            return True

    received = {}

    class FakeOrchestrator:
        def __init__(self, config):
            received["secret"] = config["auth"]["value"]

        @staticmethod
        def probe(url, depth=None):
            return {"status": "stubbed", "base_url": url, "depth": depth}

    monkeypatch.setattr(api_prober.sys, "stdin", TtyInput())
    monkeypatch.setattr(api_prober.getpass, "getpass", lambda prompt: canary)
    monkeypatch.setattr("ApiProber.core.config.load_config", lambda: deepcopy(DEFAULT_CONFIG))
    monkeypatch.setattr("ApiProber.discovery.orchestrator.ProbeOrchestrator", FakeOrchestrator)
    args = SimpleNamespace(
        auth_value=None,
        auth_prompt=True,
        url="HTTPS://Example.COM/",
        depth=0,
        delay_ms=0,
        max_requests=1,
        auth_type="bearer",
        test_all_methods=False,
    )

    assert api_prober.cmd_probe(args) == 0
    captured = capsys.readouterr()
    assert received["secret"] == canary
    assert canary not in captured.out
    assert canary not in captured.err


def test_hidden_prompt_fails_closed_without_tty(monkeypatch):
    class NonTtyInput:
        @staticmethod
        def isatty():
            return False

    monkeypatch.setattr(api_prober.sys, "stdin", NonTtyInput())
    with pytest.raises(RuntimeError, match="interaktives Terminal"):
        api_prober._read_auth_secret()


@pytest.mark.parametrize("stored_value", ["", "***REDACTED***", "legacy-plaintext-secret"])
def test_resume_never_overrides_current_secret_with_stored_auth(stored_value, tmp_path, monkeypatch):
    current_secret = "CANARY-" + uuid.uuid4().hex
    config = deepcopy(DEFAULT_CONFIG)
    config["db_path"] = str(tmp_path / "resume.db")
    config["auth"] = {"type": "bearer", "value": current_secret}
    orchestrator = ProbeOrchestrator(config)
    service_id = orchestrator.db.upsert_service("example", "https://example.com")

    with sqlite3.connect(orchestrator.db.db_path) as conn:
        conn.execute(
            "INSERT INTO probe_runs (service_id, config_json) VALUES (?, ?)",
            (service_id, json.dumps({"auth": {"type": "none", "value": stored_value}})),
        )
    monkeypatch.setattr(
        orchestrator,
        "probe",
        lambda url, **kwargs: {"url": url, "auth": deepcopy(orchestrator.config["auth"])},
    )

    result = orchestrator.resume("example")
    assert result["auth"] == {"type": "bearer", "value": current_secret}
    assert stored_value not in str(result) or stored_value in ("", "***REDACTED***")


def test_resume_discards_malformed_stored_auth(tmp_path, monkeypatch):
    current_secret = "CANARY-" + uuid.uuid4().hex
    malformed_secret = "MALFORMED-" + uuid.uuid4().hex
    config = deepcopy(DEFAULT_CONFIG)
    config["db_path"] = str(tmp_path / "resume-malformed.db")
    config["auth"] = {"type": "api_key", "value": current_secret}
    orchestrator = ProbeOrchestrator(config)
    service_id = orchestrator.db.upsert_service("example", "https://example.com")

    with sqlite3.connect(orchestrator.db.db_path) as conn:
        conn.execute(
            "INSERT INTO probe_runs (service_id, config_json) VALUES (?, ?)",
            (service_id, json.dumps({"auth": malformed_secret})),
        )
    monkeypatch.setattr(
        orchestrator,
        "probe",
        lambda url, **kwargs: {"auth": deepcopy(orchestrator.config["auth"])},
    )

    result = orchestrator.resume("example")
    assert result["auth"] == {"type": "api_key", "value": current_secret}
    assert malformed_secret not in str(result)


def test_config_and_database_sinks_remove_raw_secrets(tmp_path):
    canary = "CANARY-" + uuid.uuid4().hex
    tracked_config = tmp_path / "config.json"
    save_config({"auth": {"type": "bearer", "value": canary}}, tracked_config)
    assert canary not in tracked_config.read_text(encoding="utf-8")

    nested_config = tmp_path / "nested.json"
    nested_config.write_text('{}\n', encoding="utf-8")
    target = set_config_value(
        "auth", {"type": "bearer", "value": canary}, config_path=nested_config
    )
    assert target.name == "config.local.json"
    assert canary not in nested_config.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        set_config_value("auth", canary, config_path=nested_config)
    with pytest.raises(ValueError):
        save_config({"auth": canary}, tracked_config)

    db_path = tmp_path / "security.db"
    db = Database(db_path)
    service_id = db.upsert_service("example", "https://example.com")
    db.create_probe_run(service_id, {"auth": {"type": "bearer", "value": canary}})
    db.create_probe_run(service_id, {"auth": canary})
    run = db.get_last_probe_run(service_id)
    assert canary not in run["config_json"]
    assert canary.encode() not in db_path.read_bytes()

    md_path = tmp_path / "example.md"
    json_path = tmp_path / "example.json"
    service = db.get_service("example")
    export_markdown(db, service, md_path)
    export_json(db, service, json_path)
    assert canary not in md_path.read_text(encoding="utf-8")
    assert canary not in json_path.read_text(encoding="utf-8")


def test_database_and_export_sinks_reject_unsafe_base_urls(tmp_path):
    canary = "CANARY-" + uuid.uuid4().hex
    db_path = tmp_path / "security.db"
    db = Database(db_path)
    unsafe_url = f"https://user:{canary}@example.com"

    with pytest.raises(ValueError):
        db.upsert_service("unsafe", unsafe_url)
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT COUNT(*) FROM services").fetchone()[0]
    assert stored == 0
    assert canary.encode() not in db_path.read_bytes()

    fake_service = {"id": 999, "name": "unsafe", "base_url": unsafe_url}
    for exporter, output_name in ((export_markdown, "unsafe.md"), (export_json, "unsafe.json")):
        output_path = tmp_path / output_name
        with pytest.raises(ValueError):
            exporter(db, fake_service, output_path)
        assert not output_path.exists()


def test_stored_url_display_redacts_legacy_userinfo():
    assert api_prober._safe_stored_base_url("https://example.com/") == "https://example.com"
    assert api_prober._safe_stored_base_url("https://user:secret@example.com") == "[ungültige URL redigiert]"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1", "ipv4-127-0-0-1"),
        ("http://127.0.0.1:8080", "ipv4-127-0-0-1-8080"),
        ("http://[::1]", "ipv6-1"),
        ("https://xn--mnich-kva.example", "xn--mnich-kva"),
    ],
)
def test_service_names_are_safe_for_ipv4_ipv6_idn_and_ports(url, expected, tmp_path):
    config = deepcopy(DEFAULT_CONFIG)
    config["db_path"] = str(tmp_path / "unused.db")
    orchestrator = ProbeOrchestrator(config)
    assert orchestrator._derive_service_name(normalize_base_url(url)) == expected


@pytest.mark.parametrize(
    ("auth_type", "expected_header"),
    [
        ("bearer", "Authorization"),
        ("basic", "Authorization"),
        ("api_key", "X-API-Key"),
    ],
)
def test_cross_origin_redirect_strips_auth(auth_type, expected_header):
    received = {}

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.update(self.headers)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            return

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{destination.server_port}/target"
            )
            self.end_headers()

        def log_message(self, format, *args):
            return

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=destination.serve_forever, daemon=True),
        threading.Thread(target=redirector.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        config = deepcopy(DEFAULT_CONFIG)
        config["delay_ms"] = 0
        config["auth"] = {"type": auth_type, "value": "CANARY-REDIRECT"}
        response = HttpClient(config).get(
            f"http://127.0.0.1:{redirector.server_port}/start"
        )
    finally:
        redirector.shutdown()
        destination.shutdown()
        redirector.server_close()
        destination.server_close()

    assert response.status_code == 200
    assert response.url == f"http://127.0.0.1:{destination.server_port}/target"
    assert expected_header not in received


@pytest.mark.parametrize(
    "target",
    ["file:///secret", "https://user:secret@example.com/", "https://example.com/a//b"],
)
def test_redirect_target_is_validated_before_following(target):
    handler = _SafeRedirectHandler()
    request = urllib.request.Request("https://example.com/start")
    with pytest.raises(ValueError):
        handler.redirect_request(request, None, 302, "Found", {}, target)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-list contract")
def test_environment_secret_is_absent_from_windows_process_arguments(tmp_path):
    canary = "CANARY-" + uuid.uuid4().hex
    request_started = threading.Event()
    release_request = threading.Event()
    received_auth = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received_auth.append(self.headers.get("Authorization", ""))
            if self.path == "/robots.txt":
                request_started.set()
                release_request.wait(timeout=10)
                self.send_response(404)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.path != "/robots.txt":
                self.wfile.write(b'{}')

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    copied_root = tmp_path / "ApiProber"
    shutil.copytree(
        ROOT,
        copied_root,
        ignore=shutil.ignore_patterns(".git", "data", "exports", "build", "dist", "__pycache__"),
    )
    env = os.environ.copy()
    env["APIPROBER_AUTH_TYPE"] = "bearer"
    env["APIPROBER_AUTH_VALUE"] = canary
    command = [
        sys.executable,
        "api_prober.py",
        "probe",
        f"http://127.0.0.1:{server.server_port}",
        "--depth",
        "0",
        "--delay-ms",
        "0",
        "--max-requests",
        "1",
    ]
    process = subprocess.Popen(
        command,
        cwd=copied_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert request_started.wait(timeout=10), "Probe erreichte den lokalen Testserver nicht"
        query = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f'(Get-CimInstance Win32_Process -Filter "ProcessId = {process.pid}").CommandLine',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert canary not in query.stdout
        assert canary not in query.stderr
    finally:
        release_request.set()
        stdout, stderr = process.communicate(timeout=15)
        server.shutdown()
        server.server_close()

    assert process.returncode == 0
    assert canary not in stdout
    assert canary not in stderr
    assert f"Bearer {canary}" in received_auth
    for path in copied_root.rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes(), f"Canary persisted in {path}"
