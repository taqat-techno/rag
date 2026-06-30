"""Cross-file code-graph v1: symbol-definition lookup (P1-E / W12).

Generic, LSP-complementary discovery: find where a symbol is likely defined by
matching against the symbols/exports/function/class metadata already stored on
each chunk — returning file:line leads. Not authoritative (use an LSP for that).
"""

from ragtools.config import Settings
from ragtools.embedding.encoder import Encoder
from ragtools.indexing.indexer import ensure_collection, index_file
from ragtools.retrieval.codegraph import find_definitions
from ragtools.retrieval.searcher import Searcher


def _index(tmp_path, rel, body):
    settings = Settings(state_db=str(tmp_path / "s.db"))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)
    f = tmp_path / rel.split("/")[-1]
    f.write_text(body, encoding="utf-8")
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=f, relative_path=rel)
    return Searcher(client=client, encoder=encoder, settings=settings)


def test_find_function_definition(tmp_path):
    searcher = _index(tmp_path, "p/auth.py",
                      "def authenticate_user(name):\n    '''Check a user.'''\n    return name\n\n"
                      "def logout(session):\n    return None\n")
    defs = find_definitions(searcher, "authenticate_user", project_id="p")
    assert any(d["function_name"] == "authenticate_user" and d["file_path"] == "p/auth.py"
               for d in defs)
    # carries a line anchor (provenance)
    hit = next(d for d in defs if d["function_name"] == "authenticate_user")
    assert hit["line_start"] >= 1


def test_find_class_definition(tmp_path):
    searcher = _index(tmp_path, "p/models.py",
                      "class DisasterEvent:\n    '''A disaster record.'''\n    def save(self):\n        return True\n")
    defs = find_definitions(searcher, "DisasterEvent", project_id="p")
    assert any(d["class_name"] == "DisasterEvent" for d in defs)


def test_unknown_symbol_returns_empty(tmp_path):
    searcher = _index(tmp_path, "p/auth.py", "def real_function():\n    return 1\n")
    assert find_definitions(searcher, "nonexistent_symbol_xyz", project_id="p") == []


def test_empty_symbol_returns_empty(tmp_path):
    searcher = _index(tmp_path, "p/auth.py", "def real_function():\n    return 1\n")
    assert find_definitions(searcher, "   ", project_id="p") == []


def test_lexical_lookup_finds_symbol_buried_under_decoys(tmp_path):
    """The recall fix: a terse definition the semantic seed would never surface
    (many decoys mention the word) is still resolved by the lexical scroll."""
    from ragtools.config import Settings
    from ragtools.embedding.encoder import Encoder
    from ragtools.indexing.indexer import ensure_collection, index_file
    from ragtools.retrieval.searcher import Searcher

    settings = Settings(state_db=str(tmp_path / "s.db"))
    client = Settings.get_memory_client()
    encoder = Encoder(settings.embedding_model)
    ensure_collection(client, settings.collection_name, encoder.dimension)

    tgt = tmp_path / "bilingual.ts"
    tgt.write_text("export function makeBilingual(a: string, b: string) { return [a, b]; }\n",
                   encoding="utf-8")
    index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
               project_id="p", file_path=tgt, relative_path="p/lib/bilingual.ts")
    # 30 decoy docs that mention the word a lot (semantically closer) but never define it.
    for i in range(30):
        d = tmp_path / f"decoy{i}.md"
        d.write_text(f"# makeBilingual notes {i}\n\nThe makeBilingual makeBilingual "
                     f"makeBilingual helper concept is discussed at length in doc {i}.\n",
                     encoding="utf-8")
        index_file(client=client, encoder=encoder, collection_name=settings.collection_name,
                   project_id="p", file_path=d, relative_path=f"p/docs/decoy{i}.md")

    searcher = Searcher(client=client, encoder=encoder, settings=settings)
    defs = find_definitions(searcher, "makeBilingual", project_id="p", top_k=10)
    assert any(d["file_path"] == "p/lib/bilingual.ts" for d in defs), defs


def test_prisma_model_and_enum_extracted(tmp_path):
    from ragtools.chunking.dispatch import chunk_file
    f = tmp_path / "schema.prisma"
    f.write_text(
        "model BranchBatchCapacity {\n  id    Int @id\n  count Int\n}\n\n"
        "enum ReservationStatus {\n  PENDING\n  CONFIRMED\n}\n",
        encoding="utf-8",
    )
    syms = set()
    for c in chunk_file(f, "p", "p/schema.prisma"):
        syms.update(c.symbols)
    assert "BranchBatchCapacity" in syms
    assert "ReservationStatus" in syms


def test_class_method_extracted_as_symbol(tmp_path):
    from ragtools.chunking.dispatch import chunk_file
    f = tmp_path / "sms.service.ts"
    f.write_text(
        "export class SmsService {\n"
        "  async sendSMS(to: string) {\n"
        "    return this.client.send(to);\n"
        "  }\n}\n",
        encoding="utf-8",
    )
    syms = set()
    for c in chunk_file(f, "p", "p/sms.service.ts"):
        syms.update(c.symbols)
    assert "SmsService" in syms
    assert "sendSMS" in syms


def test_format_definitions_output():
    from ragtools.retrieval.formatter import format_definitions
    out = format_definitions("authenticate_user", [
        {"project_id": "p", "file_path": "auth.py", "line_start": 12,
         "function_name": "authenticate_user", "class_name": None, "match": "definition"},
    ])
    assert "p/auth.py:L12" in out
    assert "def authenticate_user" in out
    # empty case is explicit "not proof of absence"
    empty = format_definitions("ghost", [])
    assert "No definition found" in empty
    assert "NOT proof of absence" in empty
