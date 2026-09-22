# Security Policy

## Reporting a Vulnerability

If you find a security vulnerability, please report it responsibly:

1. **Do NOT open a public issue**
2. **Use GitHub's [private vulnerability reporting](https://github.com/dev-bricks/ApiProber/security/advisories/new)**
3. Include: description, steps to reproduce, potential impact

### How to Report

1. Go to: https://github.com/dev-bricks/ApiProber/security/advisories/new
2. Fill out the form (title, description, severity, affected versions)
3. Submit privately (not visible to public until disclosed)

## Security Scope & Principles

- **Zero-Credential Leakage:** Credentials provided via environment variables (`APIPROBER_AUTH_VALUE`) or interactive prompts are strictly redacted from logs, databases (`***REDACTED***`), and export files.
- **Ethical Reconnaissance:** Default operation is read-only (GET, HEAD, OPTIONS) with automatic `robots.txt` compliance and inter-request delays.
- **Local Isolation:** SQLite databases and exports reside exclusively in local project directories.

## Response SLA & Invariants

- **INV-SLA-10 (48-Hour Response SLA):** You will receive an initial response and acknowledgment of your report within 48 hours.
- **INV-SLA-09 (5-Day Vulnerability Triage):** Full vulnerability verification, severity assessment, and mitigation plan within 5 business days.
- **Coordinated Disclosure:** We kindly request allowing a reasonable remediation window before any public disclosure.
