import json

from codeanalyzer.core.scanner import scan
from codeanalyzer.parsers import parse_file
from codeanalyzer.resolvers.modules import resolve_modules
from codeanalyzer.resolvers.symbols import resolve_symbols


def _resolve(sample_root):
    files, _ = scan(sample_root, [], [], respect_gitignore=True)
    outputs = {}
    symbols = []
    for sf in files:
        out, _ = parse_file(sf)
        outputs[sf.id] = out
        symbols.extend(out.symbols)
    mod = resolve_modules(files, outputs, sample_root)
    sym_edges = resolve_symbols(symbols, outputs, mod.bindings)
    return mod, sym_edges


def _imports(mod):
    return {(e.src, e.dst) for e in mod.edges}


def test_python_relative_import(sample_root):
    mod, _ = _resolve(sample_root)
    assert ("py/services.py", "py/models.py") in _imports(mod)


def test_python_cycle_imports(sample_root):
    mod, _ = _resolve(sample_root)
    imps = _imports(mod)
    assert ("py/cyclic_a.py", "py/cyclic_b.py") in imps
    assert ("py/cyclic_b.py", "py/cyclic_a.py") in imps


def test_ts_alias_and_index(sample_root):
    mod, _ = _resolve(sample_root)
    imps = _imports(mod)
    # tsconfig paths alias @app/util -> src/app/util.ts
    assert ("src/app/main.ts", "src/app/util.ts") in imps
    # "." resolves to index.ts
    assert ("src/app/main.ts", "src/app/index.ts") in imps


def test_js_require(sample_root):
    mod, _ = _resolve(sample_root)
    assert ("src/legacy.js", "src/app/util.ts") in _imports(mod)


def test_c_include_relative(sample_root):
    mod, _ = _resolve(sample_root)
    imps = _imports(mod)
    assert ("c/main.c", "c/util.h") in imps
    # angle include not in project -> external
    assert ("c/main.c", "external:stdio.h") in imps


def test_java_import_resolution(sample_root):
    mod, _ = _resolve(sample_root)
    imps = _imports(mod)
    assert ("java/com/ex/Main.java", "java/com/ex/Helper.java") in imps
    # java.util.List -> external
    assert ("java/com/ex/Main.java", "external:java") in imps


def test_external_and_unresolved_classification(sample_root):
    mod, sym_edges = _resolve(sample_root)
    dsts = {e.dst for e in mod.edges}
    assert "external:lodash" in dsts
    # unresolved symbol calls appear in symbol edges
    unresolved = {e.dst for e in sym_edges if e.dst.startswith("unresolved:")}
    assert "unresolved:printf" in unresolved


def test_symbol_call_confidence(sample_root):
    _, sym_edges = _resolve(sample_root)
    by = {}
    for e in sym_edges:
        by.setdefault((e.src.split("::", 1)[1], e.dst.rsplit("::", 1)[-1]), e)
    # same-file / import-bound call -> certain
    greet = by[("User::greet", "describe")]
    assert greet.confidence == "certain"
    # inherit resolved same file -> certain
    inh = next(e for e in sym_edges if e.kind == "inherit"
               and e.src.endswith("::User"))
    assert inh.dst.endswith("::Base") and inh.confidence == "certain"


def test_ambiguous_overload_call(sample_root):
    _, sym_edges = _resolve(sample_root)
    amb = [e for e in sym_edges if e.ambiguous and e.kind == "call"]
    # run() calling run(0) is ambiguous between the two overloads
    targets = {e.dst for e in amb}
    assert any("Main::run" in t for t in targets)
    assert all(e.confidence == "inferred" for e in amb)


def test_compile_commands_include_dirs(tmp_path):
    # header lives in an include dir, referenced via angle include
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "lib.h").write_text("int f(void);\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.c").write_text('#include <lib.h>\nint g(void){ return f(); }\n')
    cc = tmp_path / "compile_commands.json"
    cc.write_text(json.dumps([{
        "directory": str(tmp_path),
        "command": f"cc -I{inc} -c src/a.c",
        "file": "src/a.c",
    }]))

    files, _ = scan(tmp_path, [], [], respect_gitignore=False)
    outputs = {sf.id: parse_file(sf)[0] for sf in files}
    mod = resolve_modules(files, outputs, tmp_path, compile_commands=str(cc))
    imps = {(e.src, e.dst) for e in mod.edges}
    assert ("src/a.c", "include/lib.h") in imps
