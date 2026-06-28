"""Tests for file classification (chunking/languages.py)."""

from ragtools.chunking.languages import (
    CODE,
    CONFIG,
    DOCUMENTATION,
    classify_file,
    is_documentation,
    is_supported,
)


class TestClassifyExtensions:
    def test_python(self):
        fc = classify_file("foo/bar.py")
        assert fc.language == "python"
        assert fc.chunk_type == CODE
        assert fc.extension == ".py"

    def test_all_required_code_extensions(self):
        for ext, lang in [
            (".js", "javascript"), (".jsx", "javascript"), (".ts", "typescript"),
            (".tsx", "typescript"), (".java", "java"), (".go", "go"),
            (".cs", "csharp"), (".php", "php"), (".html", "html"),
            (".css", "css"), (".scss", "scss"), (".sql", "sql"), (".sh", "shell"),
        ]:
            fc = classify_file(f"x{ext}")
            assert fc is not None, ext
            assert fc.chunk_type == CODE, ext
            assert fc.language == lang, ext

    def test_config_extensions(self):
        for ext in [".json", ".yaml", ".yml", ".xml", ".toml", ".ini"]:
            fc = classify_file(f"x{ext}")
            assert fc is not None and fc.chunk_type == CONFIG, ext

    def test_markdown_is_documentation(self):
        assert classify_file("notes.md").chunk_type == DOCUMENTATION


class TestClassifyFilenames:
    def test_dockerfile(self):
        assert classify_file("Dockerfile").language == "dockerfile"
        assert classify_file("Dockerfile").chunk_type == CONFIG

    def test_compound_dockerfile(self):
        assert classify_file("service.dockerfile").language == "dockerfile"

    def test_requirements(self):
        assert classify_file("requirements.txt").chunk_type == CONFIG

    def test_pyproject(self):
        assert classify_file("pyproject.toml").language == "toml"

    def test_package_json(self):
        assert classify_file("package.json").chunk_type == CONFIG

    def test_readme_without_extension(self):
        assert classify_file("README").chunk_type == DOCUMENTATION

    def test_readme_variants(self):
        assert classify_file("README.md").chunk_type == DOCUMENTATION
        assert classify_file("readme.rst").chunk_type == DOCUMENTATION

    def test_package_lock_not_indexed(self):
        # Lockfiles are noise — classification still allows json, but they are
        # excluded by ignore rules. package-lock.json is NOT a special filename.
        fc = classify_file("package-lock.json")
        assert fc is not None and fc.language == "json"  # by extension only


class TestSupport:
    def test_supported(self):
        assert is_supported("a.py")
        assert is_supported("Dockerfile")
        assert is_supported("readme.md")

    def test_unsupported(self):
        assert not is_supported("image.png")
        assert not is_supported("binary.exe")
        assert classify_file("x.zip") is None

    def test_is_documentation(self):
        assert is_documentation("a.md")
        assert not is_documentation("a.py")
