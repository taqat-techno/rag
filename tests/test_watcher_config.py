"""Watcher-consistency regression tests (P1).

The file watcher must honor the project **Mode** (so ``rag watch`` matches
``rag index``) and must never wake on / index secret-bearing files.
``is_indexable_change`` is the shared predicate used by both the CLI watcher
(observer) and the service watcher thread.
"""

from ragtools.ignore import IgnoreRules
from ragtools.watcher.observer import is_indexable_change


def _rules(root):
    return IgnoreRules(content_root=root)


def test_docs_indexed_in_docs_and_general(tmp_path):
    rules = _rules(tmp_path)
    md = str(tmp_path / "guide.md")
    assert is_indexable_change(md, rules, tmp_path, mode="docs")
    assert is_indexable_change(md, rules, tmp_path, mode="general")


def test_docs_excluded_in_code_mode(tmp_path):
    rules = _rules(tmp_path)
    md = str(tmp_path / "guide.md")
    assert not is_indexable_change(md, rules, tmp_path, mode="code")


def test_code_excluded_in_docs_mode(tmp_path):
    rules = _rules(tmp_path)
    py = str(tmp_path / "app.py")
    assert not is_indexable_change(py, rules, tmp_path, mode="docs")
    assert is_indexable_change(py, rules, tmp_path, mode="code")
    assert is_indexable_change(py, rules, tmp_path, mode="general")


def test_config_excluded_in_docs_mode(tmp_path):
    rules = _rules(tmp_path)
    cfg = str(tmp_path / "settings.yaml")
    assert not is_indexable_change(cfg, rules, tmp_path, mode="docs")
    assert is_indexable_change(cfg, rules, tmp_path, mode="code")
    assert is_indexable_change(cfg, rules, tmp_path, mode="general")


def test_secrets_never_wake_watcher(tmp_path):
    rules = _rules(tmp_path)
    for name in [".env", "server.key", "credentials.json", "id_rsa"]:
        p = str(tmp_path / name)
        # Secret exclusion is orthogonal and always-on, even in general mode.
        assert not is_indexable_change(p, rules, tmp_path, mode="general"), name


def test_ignored_dirs_excluded(tmp_path):
    rules = _rules(tmp_path)
    p = str(tmp_path / "node_modules" / "x.js")
    assert not is_indexable_change(p, rules, tmp_path, mode="general")


def test_unsupported_excluded(tmp_path):
    rules = _rules(tmp_path)
    assert not is_indexable_change(str(tmp_path / "image.png"), rules, tmp_path, mode="general")
