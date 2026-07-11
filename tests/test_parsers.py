from pathlib import Path

from codeanalyzer.core.model import Language, SymbolKind
from codeanalyzer.core.scanner import ScannedFile
from codeanalyzer.parsers import parse_file


def _mk(src: str, lang: Language, name: str) -> ScannedFile:
    return ScannedFile(id=name, path=Path(name), language=lang,
                       size=len(src), source=src, is_test=False)


def _scan_one(sample_root, rel):
    from codeanalyzer.core.scanner import scan
    files, _ = scan(sample_root, [], [], respect_gitignore=True)
    return next(f for f in files if f.id == rel)


def test_module_symbol_id_format(sample_root):
    sf = _scan_one(sample_root, "py/models.py")
    out, _ = parse_file(sf)
    mods = [s for s in out.symbols if s.kind == SymbolKind.MODULE.value]
    assert len(mods) == 1
    assert mods[0].id == "py/models.py::<module>"
    assert mods[0].parent_id is None


def test_python_symbols_and_spans(sample_root):
    out, _ = parse_file(_scan_one(sample_root, "py/models.py"))
    byid = {s.id: s for s in out.symbols}
    assert "py/models.py::User" in byid
    assert byid["py/models.py::User"].kind == SymbolKind.CLASS.value
    greet = byid["py/models.py::User::greet"]
    assert greet.kind == SymbolKind.METHOD.value
    assert greet.parent_id == "py/models.py::User"
    # 1-indexed inclusive span
    assert greet.span.start_line == 13
    assert greet.span.end_line == 15
    assert "def greet" in greet.signature
    # inheritance recorded
    bases = {(i.base) for i in out.inherits}
    assert "Base" in bases


def test_python_calls_and_defuse(sample_root):
    out, _ = parse_file(_scan_one(sample_root, "py/models.py"))
    callees = {(c.callee, c.receiver) for c in out.calls}
    assert ("describe", "self") in callees
    # def-use for make_user: param name, assignment u, return u
    du = {u.var: u for u in out.defuses
          if u.symbol_id.endswith("make_user")}
    assert du["name"].is_param and du["name"].param_index == 0
    assert du["u"].def_lines and du["u"].return_lines


def test_typescript_class_and_imports(sample_root):
    out, _ = parse_file(_scan_one(sample_root, "src/app/main.ts"))
    byid = {s.id: s for s in out.symbols}
    assert "src/app/main.ts::Widget" in byid
    assert "src/app/main.ts::Widget::render" in byid
    raws = {(i.raw, i.kind) for i in out.raw_imports}
    assert ("@app/util", "import") in raws
    assert (".", "import") in raws


def test_javascript_require(sample_root):
    out, _ = parse_file(_scan_one(sample_root, "src/legacy.js"))
    reqs = {i.raw for i in out.raw_imports if i.kind == "require"}
    assert "./app/util" in reqs


def test_c_includes(sample_root):
    out, _ = parse_file(_scan_one(sample_root, "c/main.c"))
    incs = {(i.raw, i.angle) for i in out.raw_imports}
    assert ("util.h", False) in incs
    assert ("stdio.h", True) in incs


def test_cpp_namespace_method(sample_root):
    out, _ = parse_file(_scan_one(sample_root, "cpp/shape.cpp"))
    ids = {s.id for s in out.symbols}
    # namespace is part of the FQN chain
    assert "cpp/shape.cpp::geo::Circle" in ids
    assert "cpp/shape.cpp::geo::Circle::area" in ids
    circle_area = next(s for s in out.symbols
                       if s.id == "cpp/shape.cpp::geo::Circle::area")
    assert circle_area.parent_id == "cpp/shape.cpp::geo::Circle"
    assert {i.base for i in out.inherits} == {"Shape"}


def test_java_overload_uniquification(sample_root):
    out, _ = parse_file(_scan_one(sample_root, "java/com/ex/Main.java"))
    ids = {s.id for s in out.symbols}
    assert "java/com/ex/Main.java::Main::run" in ids
    assert "java/com/ex/Main.java::Main::run#2" in ids


def test_parse_error_isolated(sample_root):
    out, diags = parse_file(_scan_one(sample_root, "broken/broken.py"))
    # partial recovery: parse_ok False but pipeline keeps going
    assert out.parse_ok is False
    assert any(d.kind in ("parse_partial", "parse_error") for d in diags)


def test_nested_and_anonymous_functions():
    src = (
        "function outer(){\n"
        "  function inner(){ return 1; }\n"
        "  const f = () => 2;\n"
        "  return inner() + f();\n"
        "}\n"
    )
    sf = _mk(src, Language.JAVASCRIPT, "n.js")
    out, _ = parse_file(sf)
    ids = {s.id for s in out.symbols}
    assert "n.js::outer" in ids
    assert "n.js::outer::inner" in ids
    # arrow assigned to const f gets its name
    assert "n.js::outer::f" in ids
