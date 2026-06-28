"""P3 — extensible parser registry.

New languages should plug into the code chunker by registering a unit
extractor, WITHOUT editing chunk_code_file's dispatch.
"""

from ragtools.chunking import code as codemod


def test_register_language_makes_chunker_pluggable(tmp_path):
    calls = []

    def fake_extractor(source, language):
        calls.append(language)
        return [codemod.CodeUnit(text=source.strip(), kind="code",
                                 name="whole", symbols=["whole"])]

    codemod.register_language("fakelang", fake_extractor)
    try:
        f = tmp_path / "x.fakelang"
        f.write_text("content of the file", encoding="utf-8")
        chunks = codemod.chunk_code_file(f, "p", "x.fakelang", language="fakelang")
        assert calls == ["fakelang"]            # the registered extractor ran
        assert chunks and chunks[0].language == "fakelang"
        assert "whole" in chunks[0].symbols
    finally:
        codemod._LANGUAGE_EXTRACTORS.pop("fakelang", None)


def test_builtin_languages_are_registered():
    # The previously hard-coded languages are now registry entries.
    for lang in ["python", "javascript", "typescript", "java", "go",
                 "csharp", "php", "css", "scss", "sql"]:
        assert lang in codemod._LANGUAGE_EXTRACTORS, lang


def test_unregistered_language_falls_back_to_generic(tmp_path):
    # An indexable-but-unregistered code language still chunks (generic), no crash.
    f = tmp_path / "x.unknownlang"
    f.write_text("alpha\n\nbeta gamma\n", encoding="utf-8")
    chunks = codemod.chunk_code_file(f, "p", "x.unknownlang", language="unknownlang")
    assert chunks  # generic fallback produced something
