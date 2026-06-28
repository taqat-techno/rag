"""Code-aware chunking for source files.

Strategy
--------
Source files are split into *units* — semantically whole constructs such as a
class, a function/method, an import block, a constant block, or a comment
block. Units are then packed into chunks up to ``chunk_size`` tokens **without
splitting a unit across chunks** whenever possible. Only a single unit that on
its own exceeds the chunk size is split (line-based, with overlap), and even
then its signature line is preserved as a prefix.

Per language:
  - **python**  — parsed with the stdlib ``ast`` (precise: classes, methods,
    functions, decorators, imports, constants, docstrings). Falls back to the
    generic splitter on syntax errors.
  - **brace languages** (js/ts/tsx/jsx/java/go/csharp/php/css/scss) — a
    brace-depth scanner keeps each top-level ``{ ... }`` block intact and
    classifies it (class / interface / enum / function / rule).
  - **sql** — split on statement boundaries (``;``).
  - **shell / html / other** — paragraph + size packing.

Symbols (classes, functions, methods, interfaces, enums, imports, decorators,
constants) are extracted into chunk metadata for retrieval.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from ragtools.chunking.common import build_chunk
from ragtools.chunking.languages import CODE, COMMENT
from ragtools.chunking.metadata import estimate_tokens
from ragtools.models import Chunk


@dataclass
class CodeUnit:
    """A semantically whole piece of source code."""

    text: str
    kind: str  # imports | class | function | method | constant | comment | rule | statement | code
    name: str | None = None
    class_name: str | None = None
    chunk_type: str = CODE
    symbols: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def chunk_code_file(
    file_path: Path,
    project_id: str,
    relative_path: str,
    language: str,
    *,
    chunk_size: int = 400,
    chunk_overlap: int = 100,
    module: str = "",
) -> list[Chunk]:
    """Chunk a source-code file into structure-aware chunks."""
    module = module or project_id
    source = file_path.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        return []

    if language == "python":
        units = _extract_python_units(source)
    elif language in ("javascript", "typescript", "java", "go", "csharp", "php", "css", "scss"):
        units = _extract_brace_units(source, language)
    elif language == "sql":
        units = _extract_sql_units(source)
    else:
        units = _extract_generic_units(source)

    if not units:
        units = _extract_generic_units(source)

    return _pack_units(
        units,
        project_id=project_id,
        relative_path=relative_path,
        language=language,
        module=module,
        file_name=Path(relative_path).name,
        extension=file_path.suffix.lower(),
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


# ---------------------------------------------------------------------------
# Python (ast-based)
# ---------------------------------------------------------------------------


def _extract_python_units(source: str) -> list[CodeUnit]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _extract_generic_units(source)

    lines = source.splitlines()
    units: list[CodeUnit] = []

    # Module docstring → comment unit.
    docstring = ast.get_docstring(tree)
    if docstring:
        units.append(CodeUnit(text=docstring, kind="comment", chunk_type=COMMENT))

    import_segs: list[str] = []
    import_symbols: list[str] = []
    const_segs: list[str] = []
    const_symbols: list[str] = []

    def flush_imports() -> None:
        if import_segs:
            units.append(CodeUnit(
                text="\n".join(import_segs),
                kind="imports",
                name="imports",
                symbols=list(import_symbols),
            ))
            import_segs.clear()
            import_symbols.clear()

    def flush_consts() -> None:
        if const_segs:
            units.append(CodeUnit(
                text="\n".join(const_segs),
                kind="constant",
                name="constants",
                symbols=list(const_symbols),
            ))
            const_segs.clear()
            const_symbols.clear()

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            flush_consts()
            import_segs.append(_segment(lines, node))
            import_symbols.extend(_import_names(node))
        elif isinstance(node, ast.ClassDef):
            flush_imports()
            flush_consts()
            units.extend(_python_class_units(lines, source, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            flush_imports()
            flush_consts()
            units.append(_python_function_unit(lines, node, class_name=None))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            flush_imports()
            names = _assign_names(node)
            const_segs.append(_segment(lines, node))
            const_symbols.extend(names)
        elif isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            # standalone string/docstring expression — skip (handled above) or treat as comment
            continue
        else:
            flush_imports()
            flush_consts()
            seg = _segment(lines, node)
            if seg.strip():
                units.append(CodeUnit(text=seg, kind="code"))

    flush_imports()
    flush_consts()
    return units


def _python_class_units(lines: list[str], source: str, node: ast.ClassDef) -> list[CodeUnit]:
    """Return the class as one unit, or split into header + per-method units if large."""
    class_seg = _segment(lines, node, with_decorators=True)
    class_name = node.name
    decorators = _decorator_names(node)
    method_names = [
        n.name for n in node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    symbols = [class_name] + decorators + method_names

    if estimate_tokens(class_seg) <= 400 * 3:
        # Small/medium class → keep intact (don't split methods apart).
        return [CodeUnit(
            text=class_seg,
            kind="class",
            name=class_name,
            class_name=class_name,
            symbols=symbols,
        )]

    # Large class → header unit + one unit per method (methods preserved whole).
    units: list[CodeUnit] = []
    methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if methods:
        header_end = methods[0].lineno - 1
    else:
        header_end = node.end_lineno or node.lineno
    header_start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno)
    header_text = "\n".join(lines[header_start - 1:header_end]).strip()
    units.append(CodeUnit(
        text=header_text or f"class {class_name}:",
        kind="class",
        name=class_name,
        class_name=class_name,
        symbols=[class_name] + decorators,
    ))
    for m in methods:
        units.append(_python_function_unit(lines, m, class_name=class_name))
    return units


def _python_function_unit(
    lines: list[str],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    class_name: str | None,
) -> CodeUnit:
    seg = _segment(lines, node, with_decorators=True)
    decorators = _decorator_names(node)
    return CodeUnit(
        text=seg,
        kind="method" if class_name else "function",
        name=node.name,
        class_name=class_name,
        symbols=[node.name] + decorators,
    )


def _segment(lines: list[str], node: ast.AST, with_decorators: bool = False) -> str:
    """Extract the source lines for an AST node (1-based linenos)."""
    start = getattr(node, "lineno", 1)
    if with_decorators:
        decos = getattr(node, "decorator_list", []) or []
        if decos:
            start = min(start, decos[0].lineno)
    end = getattr(node, "end_lineno", start) or start
    return "\n".join(lines[start - 1:end]).rstrip()


def _import_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Import):
        names = [a.asname or a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        names = [f"{mod}.{a.name}" if mod else a.name for a in node.names]
    return names


def _assign_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names.append(node.target.id)
    return names


def _decorator_names(node: ast.AST) -> list[str]:
    out: list[str] = []
    for d in getattr(node, "decorator_list", []) or []:
        name = _decorator_to_str(d)
        if name:
            out.append(f"@{name}")
    return out


def _decorator_to_str(d: ast.AST) -> str:
    if isinstance(d, ast.Name):
        return d.id
    if isinstance(d, ast.Attribute):
        return d.attr
    if isinstance(d, ast.Call):
        return _decorator_to_str(d.func)
    return ""


# ---------------------------------------------------------------------------
# Brace languages (heuristic depth scanner)
# ---------------------------------------------------------------------------

_STRING_RE = re.compile(r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""")
_LINE_COMMENT_RE = re.compile(r"//.*$|#.*$")

_CLASS_RE = re.compile(r"\b(class|interface|enum|struct|trait)\s+([A-Za-z_$][\w$]*)")
_FUNC_PATTERNS = [
    re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)"),          # function foo(
    re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)"),  # go: func foo( / func (r R) foo(
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=].*=>"),  # const foo = () =>
    re.compile(r"\b(?:public|private|protected|internal|static|async|override|virtual|final)\b[^\n;{]*?\b([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"^([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{?"),    # bare method foo(...) {
]
_IMPORT_RE = re.compile(r"^\s*(import\b|export\b.*\bfrom\b|from\b|require\(|using\b|#include|package\b|use\b|@import\b)")


def _strip_code(line: str) -> str:
    """Remove string literals and line comments so braces inside them don't count."""
    no_strings = _STRING_RE.sub('""', line)
    return _LINE_COMMENT_RE.sub("", no_strings)


def _extract_brace_units(source: str, language: str) -> list[CodeUnit]:
    lines = source.split("\n")
    units: list[CodeUnit] = []
    buf: list[str] = []
    depth = 0
    opened_block = False

    def flush_block() -> None:
        nonlocal buf, opened_block
        text = "\n".join(buf).strip("\n")
        if text.strip():
            units.append(_classify_brace_unit(text, language))
        buf = []
        opened_block = False

    def flush_preamble() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            units.append(_classify_brace_unit(text, language))
        buf = []

    for raw in lines:
        buf.append(raw)
        code = _strip_code(raw)
        depth += code.count("{") - code.count("}")
        if depth < 0:
            depth = 0
        if depth == 0:
            if opened_block:
                flush_block()
            elif raw.strip() == "":
                flush_preamble()
        else:
            opened_block = True

    flush_preamble()
    return units


def _classify_brace_unit(text: str, language: str) -> CodeUnit:
    first = _first_code_line(text)

    # Import / top-level statement block (no block opened).
    if "{" not in _strip_code(text) and _IMPORT_RE.search(first):
        symbols = _scan_symbols(text)
        return CodeUnit(text=text, kind="imports", name="imports", symbols=symbols)

    m = _CLASS_RE.search(first)
    if m:
        keyword, name = m.group(1), m.group(2)
        kind = "class" if keyword in ("class", "struct", "trait") else keyword
        return CodeUnit(
            text=text, kind=kind, name=name,
            class_name=name if kind == "class" else None,
            symbols=_scan_symbols(text) or [name],
        )

    for pat in _FUNC_PATTERNS:
        fm = pat.search(first)
        if fm:
            name = fm.group(1)
            return CodeUnit(text=text, kind="function", name=name, symbols=[name])

    # CSS/SCSS rule, or unclassified block.
    if language in ("css", "scss") and "{" in first:
        selector = first.split("{")[0].strip()
        return CodeUnit(text=text, kind="rule", name=selector or None, symbols=[])

    return CodeUnit(text=text, kind="code", symbols=_scan_symbols(text))


def _first_code_line(text: str) -> str:
    for line in text.split("\n"):
        s = line.strip()
        if s and not s.startswith(("//", "#", "/*", "*", "<!--")):
            return s
    return text.strip().split("\n")[0] if text.strip() else ""


def _scan_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for m in _CLASS_RE.finditer(text):
        symbols.append(m.group(2))
    for pat in _FUNC_PATTERNS[:3]:
        for m in pat.finditer(text):
            symbols.append(m.group(1))
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SQL_NAME_RE = re.compile(
    r"\b(?:create|alter|drop)\s+(?:or\s+replace\s+)?(?:table|view|function|procedure|index|trigger|materialized\s+view)\s+(?:if\s+not\s+exists\s+)?[`\"\[]?([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)


def _extract_sql_units(source: str) -> list[CodeUnit]:
    # Split on semicolons that terminate statements (naive but effective).
    statements = [s.strip() for s in source.split(";") if s.strip()]
    units: list[CodeUnit] = []
    for stmt in statements:
        m = _SQL_NAME_RE.search(stmt)
        name = m.group(1) if m else None
        units.append(CodeUnit(
            text=stmt + ";",
            kind="statement",
            name=name,
            symbols=[name] if name else [],
        ))
    return units


# ---------------------------------------------------------------------------
# Generic (shell, html, fallback)
# ---------------------------------------------------------------------------

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SH_FUNC_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w]*)\s*\(\s*\)\s*\{")


def _extract_generic_units(source: str) -> list[CodeUnit]:
    paras = [p.strip("\n") for p in _PARAGRAPH_RE.split(source) if p.strip()]
    units: list[CodeUnit] = []
    for p in paras:
        m = _SH_FUNC_RE.search(p.split("\n", 1)[0])
        name = m.group(1) if m else None
        units.append(CodeUnit(
            text=p,
            kind="function" if name else "code",
            name=name,
            symbols=[name] if name else [],
        ))
    if not units and source.strip():
        units.append(CodeUnit(text=source.strip(), kind="code"))
    return units


# ---------------------------------------------------------------------------
# Packing units into chunks
# ---------------------------------------------------------------------------


def _pack_units(
    units: list[CodeUnit],
    *,
    project_id: str,
    relative_path: str,
    language: str,
    module: str,
    file_name: str,
    extension: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    index = 0

    buffer: list[CodeUnit] = []
    buffer_tokens = 0

    def emit(units_in_chunk: list[CodeUnit]) -> None:
        nonlocal index
        if not units_in_chunk:
            return
        raw_text = "\n\n".join(u.text for u in units_in_chunk).strip()
        if not raw_text:
            return
        headings = _headings_for(units_in_chunk)
        class_name, function_name = _names_for(units_in_chunk)
        symbols: list[str] = []
        for u in units_in_chunk:
            symbols.extend(u.symbols)
        chunk_type = _chunk_type_for(units_in_chunk)
        chunks.append(build_chunk(
            project_id=project_id,
            file_path=relative_path,
            chunk_index=index,
            raw_text=raw_text,
            language=language,
            chunk_type=chunk_type,
            file_name=file_name,
            extension=extension,
            module=module,
            headings=headings,
            class_name=class_name,
            function_name=function_name,
            symbols=_dedup(symbols),
        ))
        index += 1

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        emit(buffer)
        buffer = []
        buffer_tokens = 0

    for unit in units:
        utokens = estimate_tokens(unit.text)

        # Comment/docstring blocks stay as their own chunk so chunk_type=comment
        # is preserved and isn't absorbed into adjacent code.
        if unit.chunk_type == COMMENT:
            flush()
            if utokens > chunk_size:
                for piece in _split_oversized(unit, chunk_size, chunk_overlap):
                    emit([piece])
            else:
                emit([unit])
            continue

        if utokens > chunk_size:
            # Oversized single unit — flush buffer, then split this unit alone.
            flush()
            for piece in _split_oversized(unit, chunk_size, chunk_overlap):
                emit([piece])
            continue

        if buffer_tokens + utokens > chunk_size and buffer:
            flush()

        buffer.append(unit)
        buffer_tokens += utokens

    flush()
    return chunks


def _split_oversized(unit: CodeUnit, chunk_size: int, chunk_overlap: int) -> list[CodeUnit]:
    """Line-split a single oversized unit, preserving its signature as a prefix."""
    lines = unit.text.split("\n")
    signature = lines[0] if lines else ""
    pieces: list[CodeUnit] = []
    current: list[str] = []
    current_tokens = 0
    first = True

    for line in lines:
        ltok = estimate_tokens(line)
        if current_tokens + ltok > chunk_size and current:
            body = "\n".join(current)
            text = body if first else f"{signature}\n# ... (continued)\n{body}"
            pieces.append(CodeUnit(
                text=text, kind=unit.kind, name=unit.name,
                class_name=unit.class_name, chunk_type=unit.chunk_type,
                symbols=unit.symbols if first else [],
            ))
            first = False
            # overlap: keep tail lines
            overlap: list[str] = []
            otok = 0
            for l in reversed(current):
                lt = estimate_tokens(l)
                if otok + lt > chunk_overlap:
                    break
                overlap.insert(0, l)
                otok += lt
            current = overlap
            current_tokens = otok
        current.append(line)
        current_tokens += ltok

    if current:
        body = "\n".join(current)
        text = body if first else f"{signature}\n# ... (continued)\n{body}"
        pieces.append(CodeUnit(
            text=text, kind=unit.kind, name=unit.name,
            class_name=unit.class_name, chunk_type=unit.chunk_type,
            symbols=unit.symbols if first else [],
        ))
    return pieces


def _headings_for(units: list[CodeUnit]) -> list[str]:
    if len(units) == 1:
        u = units[0]
        if u.class_name and u.kind in ("method",):
            return [u.class_name, u.name or ""]
        if u.name:
            return [f"{u.kind} {u.name}" if u.kind in ("class", "function", "interface", "enum") else u.name]
        return [u.kind]
    # Mixed bag — label by the dominant kinds present.
    labels = _dedup([u.name or u.kind for u in units])
    return labels[:4]


def _names_for(units: list[CodeUnit]) -> tuple[str | None, str | None]:
    """Best-effort (class_name, function_name) for a chunk.

    Single-unit chunks map directly; multi-unit chunks (small files whose units
    pack together) surface the first class and first function/method they hold
    so the names aren't lost from the chunk metadata.
    """
    class_name: str | None = None
    function_name: str | None = None
    for u in units:
        if u.class_name and class_name is None:
            class_name = u.class_name
        if u.kind in ("function", "method") and function_name is None:
            function_name = u.name
        elif u.kind in ("class", "interface", "enum", "rule") and class_name is None:
            class_name = u.name
    return class_name, function_name


def _chunk_type_for(units: list[CodeUnit]) -> str:
    types = {u.chunk_type for u in units}
    if types == {COMMENT}:
        return COMMENT
    return CODE


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out
