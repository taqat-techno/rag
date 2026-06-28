"""Tests for code-aware and config chunking (no embedding model needed)."""

from pathlib import Path

from ragtools.chunking.code import chunk_code_file
from ragtools.chunking.config_files import chunk_config_file
from ragtools.chunking.dispatch import chunk_file
from ragtools.chunking.languages import CODE, COMMENT, CONFIG, DOCUMENTATION

FIXTURES = Path(__file__).parent / "fixtures"
CODE_PROJECT = FIXTURES / "code_project"


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestPythonChunking:
    def test_extracts_classes_functions_imports(self, tmp_path):
        src = '''"""Module docstring."""
import os
from typing import List

CONST_VALUE = 42

def top_level(x):
    return x + 1

class Widget:
    """A widget."""

    def __init__(self, n):
        self.n = n

    def render(self):
        return "w" * self.n
'''
        p = _write(tmp_path, "m.py", src)
        chunks = chunk_code_file(p, "proj", "proj/m.py", "python")
        assert chunks
        all_symbols = {s for c in chunks for s in c.symbols}
        assert "top_level" in all_symbols
        assert "Widget" in all_symbols
        assert any("os" in s for s in all_symbols)
        assert "CONST_VALUE" in all_symbols
        # docstring became a comment chunk
        assert any(c.chunk_type == COMMENT for c in chunks)

    def test_class_kept_in_single_chunk(self, tmp_path):
        src = '''class Small:
    def a(self):
        return 1

    def b(self):
        return 2
'''
        p = _write(tmp_path, "s.py", src)
        chunks = chunk_code_file(p, "proj", "proj/s.py", "python")
        class_chunks = [c for c in chunks if c.class_name == "Small"]
        assert len(class_chunks) == 1
        assert "def a" in class_chunks[0].raw_text
        assert "def b" in class_chunks[0].raw_text

    def test_decorators_extracted(self, tmp_path):
        src = '''import functools

@functools.cache
def cached():
    return 1
'''
        p = _write(tmp_path, "d.py", src)
        chunks = chunk_code_file(p, "proj", "proj/d.py", "python")
        symbols = {s for c in chunks for s in c.symbols}
        assert "@cache" in symbols
        assert "cached" in symbols

    def test_metadata_on_chunks(self, tmp_path):
        src = "def f():\n    return 1\n"
        p = _write(tmp_path, "f.py", src)
        chunks = chunk_code_file(p, "proj", "proj/f.py", "python")
        c = chunks[0]
        assert c.language == "python"
        assert c.chunk_type == CODE
        assert c.file_name == "f.py"
        assert c.module == "proj"
        assert c.function_name == "f"

    def test_syntax_error_falls_back(self, tmp_path):
        src = "def broken(:\n  this is not python\n"
        p = _write(tmp_path, "b.py", src)
        chunks = chunk_code_file(p, "proj", "proj/b.py", "python")
        assert chunks  # did not crash, produced something

    def test_large_class_splits_by_method(self, tmp_path):
        body = "\n".join(f"    def method_{i}(self):\n        return {i} * 100000" for i in range(60))
        src = "class Big:\n" + body
        p = _write(tmp_path, "big.py", src)
        chunks = chunk_code_file(p, "proj", "proj/big.py", "python", chunk_size=100)
        assert len(chunks) > 1
        # methods preserved (a method body never split mid-line into a different chunk header)
        assert any("method_0" in c.raw_text for c in chunks)


class TestBraceChunking:
    def test_typescript_symbols(self, tmp_path):
        src = '''import { X } from "x";

export class Service {
  run(): void {}
}

interface Opts {
  flag: boolean;
}

function helper() { return 1; }
'''
        p = _write(tmp_path, "a.ts", src)
        chunks = chunk_code_file(p, "proj", "proj/a.ts", "typescript")
        symbols = {s for c in chunks for s in c.symbols}
        assert "Service" in symbols
        assert "Opts" in symbols
        assert "helper" in symbols
        assert all(c.language == "typescript" for c in chunks)

    def test_does_not_split_block_midway(self, tmp_path):
        # A function with a body — opening and closing brace must stay together.
        src = '''function big() {
  const a = 1;
  const b = 2;
  return a + b;
}
'''
        p = _write(tmp_path, "b.js", src)
        chunks = chunk_code_file(p, "proj", "proj/b.js", "javascript")
        joined = [c for c in chunks if "big" in c.raw_text]
        assert joined
        assert "{" in joined[0].raw_text and "}" in joined[0].raw_text


class TestSqlChunking:
    def test_statements_and_names(self, tmp_path):
        src = "CREATE TABLE users (id INT);\nCREATE VIEW active AS SELECT * FROM users;"
        p = _write(tmp_path, "s.sql", src)
        chunks = chunk_code_file(p, "proj", "proj/s.sql", "sql")
        symbols = {s for c in chunks for s in c.symbols}
        assert "users" in symbols
        assert "active" in symbols


class TestConfigChunking:
    def test_json_by_top_level_key(self, tmp_path):
        src = '{"name": "demo", "scripts": {"build": "tsc"}, "version": "1.0"}'
        p = _write(tmp_path, "package.json", src)
        chunks = chunk_config_file(p, "proj", "proj/package.json", "json")
        symbols = {s for c in chunks for s in c.symbols}
        assert "name" in symbols
        assert "scripts" in symbols
        assert all(c.chunk_type == CONFIG for c in chunks)

    def test_yaml_top_keys(self, tmp_path):
        src = "name: demo\nversion: 2\nservices:\n  web:\n    image: nginx\n"
        p = _write(tmp_path, "c.yaml", src)
        chunks = chunk_config_file(p, "proj", "proj/c.yaml", "yaml")
        symbols = {s for c in chunks for s in c.symbols}
        assert "name" in symbols
        assert "services" in symbols


class TestDispatch:
    def test_routes_markdown(self):
        chunks = chunk_file(CODE_PROJECT / "README.md", "demo", "demo/README.md")
        assert chunks
        assert all(c.chunk_type == DOCUMENTATION for c in chunks)
        assert all(c.language == "markdown" for c in chunks)

    def test_routes_python(self):
        chunks = chunk_file(CODE_PROJECT / "auth_service.py", "demo", "demo/auth_service.py")
        assert chunks
        assert any(c.chunk_type == CODE for c in chunks)
        symbols = {s for c in chunks for s in c.symbols}
        assert "AuthService" in symbols

    def test_routes_config(self):
        chunks = chunk_file(CODE_PROJECT / "config" / "settings.yaml", "demo", "demo/config/settings.yaml")
        assert chunks
        assert all(c.chunk_type == CONFIG for c in chunks)

    def test_unsupported_returns_empty(self, tmp_path):
        p = tmp_path / "image.png"
        p.write_bytes(b"\x89PNG")
        assert chunk_file(p, "demo", "demo/image.png") == []

    def test_deterministic_ids(self):
        a = chunk_file(CODE_PROJECT / "auth_service.py", "demo", "demo/auth_service.py")
        b = chunk_file(CODE_PROJECT / "auth_service.py", "demo", "demo/auth_service.py")
        assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
