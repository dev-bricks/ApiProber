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
