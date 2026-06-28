"""Regression tests: secret-bearing files are NEVER indexed (P0 security).

Balanced policy:
  * Specific secret artifacts (.env*, *.pem, *.key, id_rsa*, credentials*, ...)
    are denied for ALL file types.
  * Broad ``*secret*`` / ``*credential*`` names are denied only for
    non-source files (so e.g. ``secret_manager.py`` stays indexable).
  * An explicit allowlist can re-include a path.

Enforcement is verified at three layers: classification/``is_secret``,
discovery (scanner), and storage (indexer — nothing reaches Qdrant).
"""

from pathlib import Path

import pytest

from ragtools.ignore import IgnoreRules, is_secret
from ragtools.indexing.scanner import discover_indexable_files


HARD_SECRET_ARTIFACTS = [
    ".env", "prod.env", ".env.local", ".env.production",
    "server.key", "private.pem", "cert.p12", "keystore.pfx", "app.jks",
    "id_rsa", "id_rsa.pub", "id_ed25519", "id_ecdsa",
    "secrets.json", "secrets.yaml", "credentials.json", "credentials.yaml",
    "credentials", "terraform.tfvars", "main.tfstate",
    ".netrc", ".pgpass", ".npmrc", ".pypirc",
]


@pytest.mark.parametrize("name", HARD_SECRET_ARTIFACTS)
def test_hard_secret_artifacts_flagged(name):
    assert is_secret(name) is True, name


def test_secret_dirs_flagged():
    assert is_secret(".aws/credentials")
    assert is_secret(".ssh/id_rsa")
    assert is_secret("project/.gnupg/secring.gpg")


def test_broad_secret_names_blocked_for_config_and_text():
    # Non-source files whose name screams "secret" are excluded.
    assert is_secret("app.secrets.yaml")
    assert is_secret("db-credentials.txt")
    assert is_secret("my_secret_config.json")


def test_broad_secret_names_allowed_for_source_code():
    # Legitimate source modules must stay indexable (no over-exclusion).
    assert is_secret("secret_manager.py") is False
    assert is_secret("credential_service.ts") is False
    assert is_secret("secrets.go") is False


def test_normal_files_not_secret():
    assert is_secret("auth_service.py") is False
    assert is_secret("README.md") is False
    assert is_secret("config.toml") is False
    assert is_secret("package.json") is False


def test_allowlist_reincludes_specific_paths():
    assert is_secret("prod.env") is True
    assert is_secret("prod.env", allowlist=["prod.env"]) is False
    assert is_secret("config/app.secrets.yaml", allowlist=["**/app.secrets.yaml"]) is False


def test_ignore_rules_is_secret_method_honors_allowlist():
    strict = IgnoreRules(content_root=".")
    assert strict.is_secret(Path("prod.env"))
    lenient = IgnoreRules(content_root=".", secret_allowlist=["prod.env"])
    assert not lenient.is_secret(Path("prod.env"))


def test_discover_excludes_secrets_keeps_source(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=sk-test-123\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"token": "abc"}\n', encoding="utf-8")
    (tmp_path / "app.secrets.yaml").write_text("password: hunter2\n", encoding="utf-8")
    (tmp_path / "server.key").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tmp_path / "secret_manager.py").write_text("def load():\n    return {}\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Docs\n", encoding="utf-8")

    found = {p.name for p in discover_indexable_files(tmp_path, include_code=True)}
    assert found == {"app.py", "secret_manager.py", "README.md"}


def test_index_file_does_not_store_secret_values(tmp_path):
    """Storage gate: a secret file never reaches Qdrant payload or vectors.

    Uses ``credentials.json`` (a SUPPORTED, chunkable extension) so that n==0 is
    attributable to the is_secret gate, not to the file being non-classifiable.
    """
    from ragtools.config import Settings
    from ragtools.embedding.encoder import Encoder
    from ragtools.indexing.indexer import ensure_collection, index_file
    from ragtools.chunking.languages import classify_file

    secret_token = "sk-LEAK-CANARY-d34db33f"
    secret = tmp_path / "credentials.json"
    secret.write_text('{"api_key": "' + secret_token + '"}\n', encoding="utf-8")
    # Precondition: this file WOULD chunk if the gate were absent.
    assert classify_file(secret) is not None

    settings = Settings()
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    n = index_file(
        client=client,
        encoder=encoder,
        collection_name=settings.collection_name,
        project_id="proj",
        file_path=secret,
        relative_path="proj/credentials.json",
    )
    assert n == 0
    points, _ = client.scroll(
        collection_name=settings.collection_name, limit=16, with_payload=True
    )
    assert points == []


# --- Adversarial-review follow-ups: secret-layer correctness ----------------

@pytest.mark.parametrize("name", [
    "secrets.sh", "export-secrets.sh", "set-credentials.bash",
    "db_credentials.sql", "secret_dump.sql", "load_credentials.sql",
])
def test_data_bearing_scripts_named_secret_are_excluded(name):
    # .sh/.bash/.sql named secret/credential are the secret artifact itself
    # (e.g. `export AWS_SECRET_ACCESS_KEY=...`), not logic modules.
    assert is_secret(name) is True, name


@pytest.mark.parametrize("name", [
    "secret_manager.py", "credential_service.ts", "secrets.go", "secretStore.java",
])
def test_logic_modules_named_secret_stay_indexable(name):
    assert is_secret(name) is False, name


@pytest.mark.parametrize("name", [
    "docs/credential-rotation-runbook.md", "secrets-policy.md", "our-credentials.rst",
])
def test_prose_docs_named_secret_stay_indexable(name):
    # Markdown/rst prose named secret/credential is documentation, not a store —
    # excluding it would be a silent regression vs the docs-only baseline.
    assert is_secret(name) is False, name


@pytest.mark.parametrize("path", [
    "secrets/app.json", "secrets/db.yml", "config/secrets/token.yaml",
])
def test_secrets_directory_excluded(path):
    assert is_secret(path) is True, path


@pytest.mark.parametrize("name", [
    "ID_RSA", "PROD.ENV", "SERVER.KEY", "Credentials.JSON", "App.Secrets.YAML",
])
def test_secret_detection_is_case_insensitive(name):
    assert is_secret(name) is True, name
