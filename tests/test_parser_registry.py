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


# --- Increment 2: more brace-family languages via the registry --------------

def test_new_brace_languages_classified_and_chunked(tmp_path):
    from ragtools.chunking.languages import CODE, classify_file
    snippet = "struct Point { x: i32 }\n\nclass Foo {\n    bar() { return 1; }\n}\n"
    for ext, lang in [(".rs", "rust"), (".kt", "kotlin"), (".scala", "scala"),
                      (".swift", "swift"), (".c", "c"), (".cpp", "cpp"), (".hpp", "cpp")]:
        fc = classify_file(f"a{ext}")
        assert fc is not None and fc.chunk_type == CODE and fc.language == lang, ext
        assert lang in codemod._LANGUAGE_EXTRACTORS, lang
        f = tmp_path / f"a{ext}"
        f.write_text(snippet, encoding="utf-8")
        chunks = codemod.chunk_code_file(f, "p", f"a{ext}", language=lang)
        assert chunks, ext
        syms = [s for c in chunks for s in c.symbols]
        assert "Point" in syms or "Foo" in syms, (ext, syms)


def test_logic_languages_named_secret_stay_indexable():
    from ragtools.ignore import is_secret
    assert is_secret("secret_config.rs") is False        # rust logic module
    assert is_secret("CredentialStore.kt") is False      # kotlin logic module
    assert is_secret("secrets.yaml") is True             # config still excluded
