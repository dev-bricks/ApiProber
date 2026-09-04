"""
ApiProber.discovery.orchestrator -- Zentrale Steuerung aller Strategien
========================================================================
Koordiniert OpenAPI-Detection, Wordlist, Pattern und Response-Driven.
"""
import json
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from ..core.config import load_config, get_db_path, redact_config, _deep_merge
from ..core.database import Database
from ..core.http_client import HttpClient, normalize_base_url
from ..core.robots import RobotsChecker
from ..core.schema_extractor import extract_schema_from_body, extract_params_from_error
from .openapi_detect import detect_openapi, extract_endpoints_from_spec
from .wordlist import probe_wordlist
from .pattern import probe_patterns
from .response_driven import discover_from_responses
from .method_tester import test_methods


class ProbeOrchestrator:
    """Orchestriert den gesamten Probing-Vorgang."""

    def __init__(self, config=None):
        self.config = config or load_config()
        self._db = None
        self._db_path = get_db_path(self.config)
        self.client = HttpClient(self.config)
        self._stop_requested = False

    @property
    def db(self):
        """Initialize persistence lazily, after a probe target passed validation."""
        if self._db is None:
            self._db = Database(self._db_path)
        return self._db

    def probe(self, url, depth=None):
        """Hauptmethode: Vollstaendiges Probing eines Service.

        Args:
            url: Basis-URL des zu probenden Service
            depth: Maximale Rekursionstiefe (ueberschreibt Config)

        Returns:
            dict: Zusammenfassung der Ergebnisse
        """
        if depth is not None:
            self.config["max_depth"] = depth

        try:
            base_url = normalize_base_url(url)
        except ValueError as exc:
            error = f"Ungültige Basis-URL: {exc}"
            print(f"[FEHLER] {error}")
            return {"error": error}
        service_name = self._derive_service_name(base_url)
        max_requests = self.config.get("max_requests", 500)
        skip_destructive = self.config.get("skip_destructive", True)

        print(f"[ApiProber] Starte Probing: {base_url}")
        print(f"  Service-Name: {service_name}")
        print(f"  Max Requests: {max_requests}")
        print(f"  Delay: {self.config.get('delay_ms', 500)}ms")
        print(f"  Destructive: {'Nein' if skip_destructive else 'Ja'}")
        print()

        # 1. Service in DB anlegen
        service_id = self.db.upsert_service(service_name, base_url)
        # Credentials (auth.value) duerfen nie im Klartext in die DB
        run_id = self.db.create_probe_run(service_id, redact_config(self.config))
        known_paths = self.db.get_endpoint_paths(service_id)
        endpoints_found = 0

        # 2. robots.txt laden
        robots = None
        if self.config.get("respect_robots_txt", True):
            robots = RobotsChecker(base_url, self.config.get("user_agent", ""))
            success, raw = robots.load()
            if success:
                self.db.upsert_service(service_name, base_url, robots_txt=raw)
                print(f"  robots.txt: Geladen ({len(raw)} Bytes)")
                crawl_delay = robots.crawl_delay
                if crawl_delay:
                    effective_delay = max(self.config["delay_ms"], int(crawl_delay * 1000))
                    self.client.delay_ms = effective_delay
                    print(f"  Crawl-Delay: {crawl_delay}s (effektiv: {effective_delay}ms)")
            elif robots.unavailable_status:
                # 5xx beim robots.txt-Abruf: Regeln unbekannt -> konservativ
                # abbrechen statt ungeprueft zu sondieren (RFC 9309)
                print(f"  robots.txt: Server-Fehler HTTP {robots.unavailable_status} -- "
                      "Regeln unbekannt, konservativ alle Pfade gesperrt. Abbruch.")
                self.db.update_probe_run(run_id, status="error")
                return {"error": f"robots.txt nicht abrufbar (HTTP {robots.unavailable_status}), "
                                 "konservativer Abbruch"}
            else:
                print("  robots.txt: Nicht vorhanden (alles erlaubt)")
            print()

        # 3. Base-URL testen
        print("[Phase 0] Base-URL testen...")
        base_resp = self.client.get(base_url)
        if base_resp.status_code > 0:
            server = base_resp.headers.get("Server", "")
            if server:
                self.db.upsert_service(service_name, base_url, server_header=server)
                print(f"  Server: {server}")
            print(f"  Status: {base_resp.status_code}")
            print(f"  Content-Type: {base_resp.content_type}")
        else:
            print(f"  FEHLER: {base_resp.error}")
            self.db.update_probe_run(run_id, status="error")
            return {"error": base_resp.error}
        print()

        # Callback fuer Endpoint-Verarbeitung
        def on_endpoint_found(path, resp):
            nonlocal endpoints_found
            endpoints_found += 1
            status_char = "+" if resp.ok else "~"
            print(f"  [{status_char}] {path} -> {resp.status_code}")

        strategies = self.config.get("strategies", ["openapi", "wordlist", "pattern", "response_driven"])

        # 4. OpenAPI-Detection (Prio 1)
        if "openapi" in strategies and not self._check_limits(max_requests):
            print("[Phase 1] OpenAPI/Swagger Detection...")
            spec_url, spec = detect_openapi(self.client, base_url, robots)
            if spec:
                print(f"  GEFUNDEN: {spec_url}")
                spec_endpoints = extract_endpoints_from_spec(spec)
                print(f"  {len(spec_endpoints)} Endpoints in Spec")
                for ep in spec_endpoints:
                    ep_id = self.db.upsert_endpoint(
                        service_id, ep["path"],
                        methods=ep["methods"],
                        discovered_by="openapi"
                    )
                    known_paths.add(ep["path"])
                    endpoints_found += 1
                    # Parameter speichern
                    for param in ep.get("parameters", []):
                        self.db.upsert_parameter(
                            ep_id,
                            name=param["name"],
                            param_type=param.get("type", "string"),
                            location=param.get("location", "query"),
                            required=param.get("required", False)
                        )
                # Spec als Metadata speichern
                meta = {"openapi_spec_url": spec_url}
                if "info" in spec:
                    meta["api_title"] = spec["info"].get("title", "")
                    meta["api_version"] = spec["info"].get("version", "")
                    meta["api_description"] = spec["info"].get("description", "")
                self.db.upsert_service(service_name, base_url, metadata=meta)
            else:
                print("  Keine OpenAPI/Swagger-Spec gefunden")
            print()

        # 5. Wordlist-Probing (Prio 2)
        if "wordlist" in strategies and not self._check_limits(max_requests):
            print("[Phase 2] Wordlist-Probing...")
            wordlist_names = self.config.get("wordlists", [])
            results = probe_wordlist(
                self.client, base_url, wordlist_names,
                robots_checker=robots, known_paths=known_paths,
                callback=on_endpoint_found, max_requests=max_requests
            )
            self._process_results(service_id, results, "wordlist")
            print(f"  {len(results)} neue Endpoints entdeckt")
            print()

        # 6. Pattern-Probing (Prio 3)
        if "pattern" in strategies and not self._check_limits(max_requests):
            print("[Phase 3] Pattern-Probing...")
            results = probe_patterns(
                self.client, base_url, self.config,
                robots_checker=robots, known_paths=known_paths,
                callback=on_endpoint_found, max_requests=max_requests
            )
            self._process_results(service_id, results, "pattern")
            print(f"  {len(results)} neue Endpoints entdeckt")
            print()

        # 7. Method-Testing fuer entdeckte Endpoints
        if not self._check_limits(max_requests):
            print("[Phase 4] Method-Testing...")
            endpoints = self.db.get_endpoints(service_id)
            tested = 0
            for ep in endpoints:
                if self._check_limits(max_requests):
                    break
                # robots.txt gilt auch fuer Endpoints aus einer OpenAPI-Spec
                # (Phase 1 traegt sie ungeprueft ein) -- sonst wuerden hier
                # sogar POST/PUT/DELETE an gesperrte Pfade gehen
                if robots and not robots.is_allowed(ep["path"]):
                    continue
                method_info = test_methods(
                    self.client, base_url, ep["path"],
                    skip_destructive=skip_destructive
                )
                self.db.upsert_endpoint(
                    service_id, ep["path"],
                    methods=method_info["methods"],
                    status_codes=list(method_info["status_codes"].values()),
                    auth_required=method_info["auth_required"],
                    auth_type_hint=method_info["auth_type_hint"],
                    content_types=method_info["content_types"]
                )
                tested += 1
            print(f"  {tested} Endpoints getestet")
            print()

        # 8. Detaillierte GET-Responses fuer Schema-Extraktion
        if not self._check_limits(max_requests):
            print("[Phase 5] Schema-Extraktion...")
            endpoints = self.db.get_endpoints(service_id)
            schemas_extracted = 0
            for ep in endpoints:
                if self._check_limits(max_requests):
                    break
                methods = json.loads(ep.get("methods_json", "[]"))
                if "GET" not in methods:
                    continue
                if robots and not robots.is_allowed(ep["path"]):
                    continue
                url = f"{base_url}{ep['path']}"
                resp = self.client.get(url)
                if resp.ok and resp.body:
                    schema = extract_schema_from_body(resp.body)
                    if schema:
                        self.db.add_response(
                            ep["id"], "GET", resp.status_code,
                            headers=resp.headers,
                            body_schema=schema,
                            body_sample=resp.body[:2048],
                            content_type=resp.content_type,
                            elapsed_ms=resp.elapsed_ms
                        )
                        schemas_extracted += 1
                elif resp.status_code in (400, 422) and resp.body:
                    # Parameter-Hints aus Error-Body
                    params = extract_params_from_error(resp.body)
                    for name, required in params:
                        self.db.upsert_parameter(
                            ep["id"], name, required=required
                        )
            print(f"  {schemas_extracted} Schemas extrahiert")
            print()

        # 9. Response-Driven Discovery (Prio 4)
        if "response_driven" in strategies and not self._check_limits(max_requests):
            print("[Phase 6] Response-Driven Discovery (HATEOAS)...")
            results = discover_from_responses(
                self.client, base_url, self.db, service_id,
                robots_checker=robots, known_paths=known_paths,
                max_depth=self.config.get("max_depth", 2),
                callback=on_endpoint_found, max_requests=max_requests
            )
            self._process_results(service_id, results, "response_driven")
            print(f"  {len(results)} neue Endpoints entdeckt")
            print()

        # 10. Abschluss
        self.db.update_service_last_probed(service_id)
        total_requests = self.client.request_count
        final_ep_count = len(self.db.get_endpoints(service_id))

        self.db.update_probe_run(
            run_id,
            status="completed",
            total_requests=total_requests,
            endpoints_found=final_ep_count,
            progress={"completed_strategies": strategies}
        )

        summary = {
            "service": service_name,
            "base_url": base_url,
            "endpoints_found": final_ep_count,
            "total_requests": total_requests,
            "status": "completed"
        }

        print("=" * 60)
        print(f"[Ergebnis] {service_name}")
        print(f"  Endpoints entdeckt: {final_ep_count}")
        print(f"  Requests gesamt:    {total_requests}")
        print(f"  DB: {self.db.db_path}")
        print("=" * 60)

        return summary

    def resume(self, service_name):
        """Setzt ein vorheriges Probing fort.

        Laedt den letzten unvollstaendigen Run und macht weiter.
        """
        service = self.db.get_service(service_name)
        if not service:
            print(f"Service '{service_name}' nicht gefunden.")
            return None

        last_run = self.db.get_last_probe_run(service["id"])
        if not last_run:
            print(f"Kein vorheriger Run fuer '{service_name}'.")
            return None

        if last_run["status"] == "completed":
            print("Letzter Run bereits abgeschlossen. Starte neuen Probe.")

        # Config aus letztem Run laden (auth.value ist dort redigiert gespeichert)
        try:
            run_config = json.loads(last_run.get("config_json", "{}"))
        except (json.JSONDecodeError, ValueError):
            run_config = {}
        if not isinstance(run_config, dict):
            run_config = {}

        if run_config:
            current_auth = self.config.get("auth", {})
            current_value = current_auth.get("value", "") if isinstance(current_auth, dict) else ""
            current_type = current_auth.get("type", "") if isinstance(current_auth, dict) else ""
            run_auth = run_config.pop("auth", None)
            stored_value = ""
            if isinstance(run_auth, dict):
                # Persistierte Werte nie wieder als Credential verwenden. Das gilt
                # auch für historische Klartext-Runs vor Einführung der Redaction.
                stored_value = run_auth.pop("value", "")
                if run_auth:
                    run_config["auth"] = run_auth
            if not isinstance(self.config.get("auth"), dict):
                self.config["auth"] = {}
            # Deep-Merge statt flachem update(): verschachtelte Keys wie
            # 'auth' werden gemergt statt komplett ersetzt
            _deep_merge(self.config, run_config)
            effective_auth = self.config.setdefault("auth", {})
            effective_auth["value"] = current_value
            if current_value and current_type and current_type != "none":
                effective_auth["type"] = current_type
            if stored_value and not current_value:
                print("  [WARN] Der gespeicherte Run nutzte Authentifizierung, aber "
                      "in der aktuellen Config/Umgebung (APIPROBER_AUTH_VALUE) ist "
                      "kein Auth-Wert gesetzt -- Probing laeuft ohne Auth.")

        return self.probe(service["base_url"])

    def _process_results(self, service_id, results, discovered_by):
        """Verarbeitet Probe-Ergebnisse in die DB."""
        for path, resp in results:
            content_types = []
            if resp.content_type:
                ct = resp.content_type.split(";")[0].strip()
                if ct:
                    content_types = [ct]

            auth_required = resp.status_code in (401, 403)
            auth_hint = ""
            if auth_required:
                www_auth = resp.headers.get("WWW-Authenticate", "")
                if "bearer" in www_auth.lower():
                    auth_hint = "bearer"
                elif "basic" in www_auth.lower():
                    auth_hint = "basic"

            ep_id = self.db.upsert_endpoint(
                service_id, path,
                methods=[resp.method] if resp.method else [],
                status_codes=[resp.status_code],
                auth_required=auth_required,
                auth_type_hint=auth_hint,
                content_types=content_types,
                discovered_by=discovered_by
            )

            # Response speichern wenn Body vorhanden
            if resp.body and resp.ok:
                schema = extract_schema_from_body(resp.body)
                self.db.add_response(
                    ep_id, resp.method or "GET", resp.status_code,
                    headers=resp.headers,
                    body_schema=schema,
                    body_sample=resp.body[:2048],
                    content_type=resp.content_type,
                    elapsed_ms=resp.elapsed_ms
                )

    def _check_limits(self, max_requests):
        """Prueft ob Request-Limit erreicht ist."""
        if self.client.request_count >= max_requests:
            print(f"  [LIMIT] Max Requests erreicht ({max_requests})")
            return True
        # STOP-Datei pruefen
        stop_file = Path(__file__).resolve().parent.parent / "STOP"
        if stop_file.exists():
            print("  [STOP] STOP-Datei gefunden -- Abbruch")
            return True
        return False

    def _derive_service_name(self, url):
        """Leitet einen Service-Namen aus der URL ab."""
        parsed = urlsplit(url)
        host = parsed.hostname or "unknown"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Bestehende DNS-Namenssemantik beibehalten.
            parts = host.split(".")
            name = parts[-2] if len(parts) >= 2 else host
        else:
            safe_address = address.compressed.replace(":", "-").replace(".", "-").strip("-")
            name = f"ipv{address.version}-{safe_address or 'unspecified'}"
        if parsed.port is not None:
            name = f"{name}-{parsed.port}"
        if not name:
            name = "service"
        return name
