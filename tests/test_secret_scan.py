"""Content-level secret detection + redaction (P0-C / W5).

Generalizes the secret problem beyond filenames: secrets appear inside ordinary
indexed text (docs/source/config) on every stack. We redact the VALUE at index
time and at serve time, while preserving the key NAME so "which key does X use"
still answers usefully.
"""

from ragtools.secret_scan import redact, scan


def test_redacts_google_api_key():
    text = "map_key = AIzaSyB4aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"
    out, findings = redact(text)
    assert "AIzaSyB4" not in out
    assert "REDACTED" in out
    assert any(f["rule"] == "google_api_key" for f in findings)


def test_redacts_assigned_secret_keeps_key_name():
    text = 'disaster.api.geoapify.key = "a7101db0c0ffee1234567890abcdef168d2"'
    out, findings = redact(text)
    assert "geoapify.key" in out           # key NAME preserved (still useful)
    assert "a7101db0c0ffee" not in out     # value masked
    assert "REDACTED" in out
    assert findings


def test_redacts_pem_private_key():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc123secretmaterial\n-----END RSA PRIVATE KEY-----"
    out, _ = redact(text)
    assert "MIIabc123secretmaterial" not in out


def test_redacts_jwt():
    text = ("auth: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c")
    out, _ = redact(text)
    assert "eyJhbGci" not in out


def test_redacts_aws_access_key():
    out, findings = redact("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert any(f["rule"] == "aws_access_key_id" for f in findings)


def test_non_secret_text_unchanged():
    text = "This section explains authentication and how tokens are used in general."
    out, findings = redact(text)
    assert out == text
    assert findings == []


def test_env_reads_and_identifiers_not_flagged():
    """Precision (W6): the audit's FPs were env-reads, imports and identifier
    references — none are literal secrets and must not be flagged."""
    for s in [
        "const CRON_SECRET = process.env.CRON_SECRET;",
        "const apiKey = getApiKeyFromVault();",
        "import { SignJWT } from 'jose';",
        "const tokenName = userToken;",
        "password: req.body.password,",
    ]:
        out, findings = redact(s)
        assert findings == [], (s, findings)
        assert out == s


def test_quoted_literal_secret_flagged_medium():
    out, findings = redact('const apiKey = "a7101db0c0ffee1234567890";')
    assert "a7101db0c0ffee1234567890" not in out
    assert "apiKey" in out
    f = [x for x in findings if x["rule"] == "assigned_secret"]
    assert f and f[0].get("severity") == "medium"


def test_unquoted_high_entropy_token_flagged():
    out, findings = redact("apiKey=a7101db0c0ffee1234567890abcdef0123")
    assert "a7101db0c0ffee" not in out
    assert any(x["rule"] == "assigned_secret" for x in findings)


def test_provider_token_severity_high():
    findings = scan("k = AKIAIOSFODNN7EXAMPLE")
    assert any(x.get("severity") == "high" for x in findings)


def test_common_non_secret_keys_not_redacted():
    """Precision guard: ordinary *_key identifiers must not be flagged."""
    for s in [
        'cache_key = "user:1234:profile"',
        'foreign_key = "orders_id_seq"',
        'monkey = "abcdefghijkl"',
        "the partition_key determines sharding across the cluster nodes",
    ]:
        out, findings = redact(s)
        assert out == s, s
        assert findings == []


def test_scan_reports_without_leaking_value():
    findings = scan("key=AKIAIOSFODNN7EXAMPLE and AIzaSyB4aBcDeFgHiJkLmNoPqRsTuVwXyZ012345")
    rules = {f["rule"] for f in findings}
    assert "aws_access_key_id" in rules
    assert "google_api_key" in rules
    # The audit output must NOT contain the secret values.
    blob = str(findings)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "AIzaSyB4" not in blob


def test_format_secret_audit_no_value_leak():
    from ragtools.retrieval.formatter import format_secret_audit
    out = format_secret_audit({"scanned": 100, "files_with_secrets": 1, "findings": [
        {"project_id": "p", "file_path": "README.md", "line_start": 5,
         "rules": ["google_api_key"], "redacted_markers": 1}]})
    assert "p/README.md:L5" in out
    assert "google_api_key" in out
    assert "rotate" in out.lower()
    clean = format_secret_audit({"scanned": 50, "findings": []})
    assert "no secrets detected" in clean.lower()


def test_owner_audit_secrets_flags_file_without_leaking_value(tmp_path):
    """End-to-end: a key in an indexed README is flagged by audit (via the
    redaction marker) and its value never appears in the audit output."""
    from ragtools.config import ProjectConfig, Settings
    from ragtools.service.owner import QdrantOwner

    settings = Settings(state_db=str(tmp_path / "s.db"),
                        projects=[ProjectConfig(id="p", path=str(tmp_path), mode="docs")])
    (tmp_path / "README.md").write_text(
        "## Config\n\nThe service uses an API key for the maps provider integration.\n"
        "Set map_key = AIzaSyB4aBcDeFgHiJkLmNoPqRsTuVwXyZ012345 to enable it.\n",
        encoding="utf-8",
    )
    owner = QdrantOwner(settings=settings, client=Settings.get_memory_client())
    owner.run_full_index()

    audit = owner.audit_secrets(project_id="p")
    assert audit["files_with_secrets"] >= 1
    assert any("README.md" in f["file_path"] for f in audit["findings"])
    assert "AIzaSyB4aBcDeFgHiJkLmNoPqRsTuVwXyZ012345" not in str(audit)


def test_index_redacts_secret_in_indexed_text(tmp_path):
    """A key committed in a Markdown file is name-shown / value-masked after index."""
    from ragtools.config import Settings
    from ragtools.embedding.encoder import Encoder
    from ragtools.indexing.indexer import ensure_collection, index_file
    from ragtools.retrieval.searcher import Searcher

    settings = Settings(state_db=str(tmp_path / "s.db"))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    readme = tmp_path / "README.md"
    readme.write_text(
        "## Geocoding configuration\n\n"
        "The map uses an API key for geocoding requests against the provider.\n"
        "Set `geoapify_api_key = AIzaSyB4aBcDeFgHiJkLmNoPqRsTuVwXyZ012345` in config.\n",
        encoding="utf-8",
    )
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=readme, relative_path="p/README.md")

    searcher = Searcher(client=client, encoder=encoder, settings=settings)
    results = searcher.search(query="which API key is used for geocoding", top_k=5, score_threshold=0.0)
    blob = " ".join(r.raw_text for r in results)
    assert "geoapify_api_key" in blob          # name still retrievable
    assert "AIzaSyB4aBcDeFgHiJkLmNoPqRsTuVwXyZ012345" not in blob  # value gone
