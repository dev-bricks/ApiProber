"""Contract and repository hygiene tests for ApiProber (Pfad A)."""
import json
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_pep621_metadata():
    """Verify PEP 621 metadata compliance in pyproject.toml."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml must exist"

    content = pyproject_path.read_text(encoding="utf-8")
    assert '"apiprober"' in content
    assert 'version = "0.1.0"' in content
    assert "Passive API discovery" in content
    assert 'readme = "README.md"' in content
    assert 'license = "MIT"' in content
    assert 'requires-python = ">=3.8"' in content

    # Required URLs
    assert 'Homepage = "https://github.com/dev-bricks/ApiProber"' in content
    assert 'Repository = "https://github.com/dev-bricks/ApiProber"' in content
    assert 'Issues = "https://github.com/dev-bricks/ApiProber/issues"' in content
    assert 'Changelog = "https://github.com/dev-bricks/ApiProber/blob/main/CHANGELOG.md"' in content
    assert 'Documentation = "https://github.com/dev-bricks/ApiProber#readme"' in content

    # Optional dependencies
    assert "[project.optional-dependencies]" in content
    assert "test =" in content
    assert "pytest>=" in content


def test_version_parity():
    """Verify version synchronicity across pyproject.toml, package, and docs."""
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import ApiProber
    import api_prober

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'version\s*=\s*"([^"]+)"', pyproject_text)
    assert match is not None, "pyproject.toml must specify version"
    pyproject_version = match.group(1)

    assert pyproject_version == "0.1.0"
    assert ApiProber.__version__ == pyproject_version
    assert api_prober.VERSION == pyproject_version


def test_ci_matrix_coverage():
    """Verify GitHub Actions CI matrix hardening and security."""
    workflow_path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
    assert workflow_path.exists(), "tests.yml must exist"

    content = workflow_path.read_text(encoding="utf-8")
    assert "ubuntu-latest" in content
    assert "windows-latest" in content
    assert "cancel-in-progress: true" in content
    assert "actions/checkout@v4" in content
    assert "actions/setup-python@v5" in content
    assert "python -m compileall -q ." in content
    assert "pytest -ra -v" in content


def test_gitignore_protection():
    """Verify .gitignore excludes conflict copies, multi-agent locks, and caches."""
    gitignore_path = REPO_ROOT / ".gitignore"
    assert gitignore_path.exists(), ".gitignore must exist"

    content = gitignore_path.read_text(encoding="utf-8")
    assert "*-conflict-*" in content
    assert "*-ASUS-GEI.*" in content
    assert "*-WORKSTATION-LG.*" in content
    assert "LOCK" in content
    assert "LOCK.*" in content
    assert ".pytest_cache/" in content
    assert ".ruff_cache/" in content


def test_cli_entrypoint_defined():
    """Verify entry points in pyproject.toml and callable main function."""
    content = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.scripts]" in content
    assert 'apiprober = "ApiProber.api_prober:main"' in content

    from ApiProber.api_prober import main
    assert callable(main)


def test_module_manifest_parity():
    """Verify ellmos-module.v2.json exists, is valid JSON, and points to canonical repo."""
    manifest_path = REPO_ROOT / "ellmos-module.v2.json"
    assert manifest_path.exists(), "ellmos-module.v2.json must exist"

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data.get("schema") == "ellmos.module.v2"
    assert data.get("id") == "ApiProber"
    assert data.get("category") == "tools"
    assert data.get("status") == "staging"
    assert data.get("source_of_truth", {}).get("repository") == "https://github.com/dev-bricks/ApiProber"


def test_readme_badges_and_links():
    """Verify README.md and README_de.md existence, language switchers, and badges."""
    readme_en = REPO_ROOT / "README.md"
    readme_de = REPO_ROOT / "README_de.md"

    assert readme_en.exists(), "README.md must exist"
    assert readme_de.exists(), "README_de.md must exist"

    en_content = readme_en.read_text(encoding="utf-8")
    de_content = readme_de.read_text(encoding="utf-8")

    # Mutual language links
    assert "README_de.md" in en_content
    assert "README.md" in de_content

    # Badges
    assert "badge" in en_content.lower()
    assert "badge" in de_content.lower()


def test_notice_and_sbom_contracts():
    """Verify NOTICE and THIRD_PARTY_LICENSES.md (SBOM) exist and define invariants."""
    notice_path = REPO_ROOT / "NOTICE"
    sbom_path = REPO_ROOT / "THIRD_PARTY_LICENSES.md"

    assert notice_path.exists(), "NOTICE must exist"
    assert sbom_path.exists(), "THIRD_PARTY_LICENSES.md must exist"

    notice_content = notice_path.read_text(encoding="utf-8")
    sbom_content = sbom_path.read_text(encoding="utf-8")

    for inv in [f"INV-LOCAL-0{i}" for i in range(1, 9)] + ["INV-SLA-09", "INV-SLA-10"]:
        assert inv in notice_content, f"NOTICE must define {inv}"
        assert inv in sbom_content, f"SBOM must define {inv}"

    assert "RunAsInvoker" in sbom_content
    assert "Zero Runtime Dependencies" in sbom_content
    assert "Lukas Geiger" in notice_content
    assert "dev-bricks" in notice_content
    assert "open-bricks" in notice_content


def test_bilingual_navigation_and_anchors():
    """Verify synchronized 18-point navigation anchors across EN and DE READMEs."""
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    for i in range(1, 19):
        anchor = f'<a id="sec-{i:02d}"></a>'
        assert anchor in readme_en, f"README.md missing anchor {anchor}"
        assert anchor in readme_de, f"README_de.md missing anchor {anchor}"

    for persona in ["[PERSONA-01]", "[PERSONA-02]", "[PERSONA-03]", "[PERSONA-04]"]:
        assert persona in readme_en, f"README.md missing {persona}"
        assert persona in readme_de, f"README_de.md missing {persona}"


def test_dual_mermaid_diagrams():
    """Verify dual Mermaid diagrams (flowchart TD + sequenceDiagram) in both READMEs."""
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    for content, name in [(readme_en, "README.md"), (readme_de, "README_de.md")]:
        assert "flowchart TD" in content, f"{name} must contain flowchart TD"
        assert "sequenceDiagram" in content, f"{name} must contain sequenceDiagram"
        assert "autonumber" in content, f"{name} sequenceDiagram must use autonumber"


def test_bgb_statutory_disclaimer():
    """Verify § 521 BGB gratuitous bailee liability limitation in both READMEs."""
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    assert "§ 521 BGB" in readme_en
    assert "§ 521 BGB" in readme_de
    assert "unentgeltliche Open-Source-Schenkung" in readme_de


def test_security_policy_sla():
    """Verify SECURITY.md includes 48-hour response SLA and 5-day triage commitment."""
    sec_path = REPO_ROOT / "SECURITY.md"
    assert sec_path.exists(), "SECURITY.md must exist"

    sec_content = sec_path.read_text(encoding="utf-8")
    assert "INV-SLA-10" in sec_content
    assert "48-Hour Response SLA" in sec_content
    assert "INV-SLA-09" in sec_content
    assert "5-Day Vulnerability Triage" in sec_content


def test_pep621_license_files_and_keywords():
    """Verify license-files includes NOTICE and SBOM, and keywords are saturated."""
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"LICENSE"' in pyproject_text
    assert '"NOTICE"' in pyproject_text
    assert '"THIRD_PARTY_LICENSES.md"' in pyproject_text

    match = re.search(r"keywords\s*=\s*\[(.*?)\]", pyproject_text, re.DOTALL)
    assert match is not None, "keywords array must exist in pyproject.toml"
    raw_keywords = [k.strip(' \n\t"') for k in match.group(1).split(",") if k.strip(' \n\t"')]
    assert len(raw_keywords) >= 15, f"Expected >= 15 keywords, found {len(raw_keywords)}"

