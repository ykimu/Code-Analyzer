from codeanalyzer.core.pipeline import analyze_project


def test_pipeline_meta(sample_root):
    r = analyze_project(sample_root)
    assert r.meta.file_count == len(r.files)
    assert r.meta.gitignore_respected is True
    assert r.meta.languages["python"] == 6
    assert r.meta.languages["typescript"] == 3
    # broken.py recovers partially -> counted as a parse failure, not a crash
    assert r.meta.parse_failures == 1
    assert r.meta.generated_at == ""  # left for CLI
    assert "node_modules" in r.meta.excludes


def test_pipeline_import_edges_resolved(sample_root):
    r = analyze_project(sample_root)
    imps = {(e.src, e.dst) for e in r.edges if e.kind == "import"}
    assert ("py/services.py", "py/models.py") in imps
    assert ("src/app/main.ts", "src/app/util.ts") in imps
    assert ("java/com/ex/Main.java", "java/com/ex/Helper.java") in imps


def test_pipeline_detects_cycle(sample_root):
    r = analyze_project(sample_root)
    cycles = r.metrics["cycles"]
    assert ["py/cyclic_a.py", "py/cyclic_b.py"] in cycles


def test_pipeline_symbols_have_module(sample_root):
    r = analyze_project(sample_root)
    mod_ids = {s.id for s in r.symbols if s.kind == "module"}
    assert "py/models.py::<module>" in mod_ids
    # every file gets exactly one module symbol
    files_parsed = {f.id for f in r.files}
    module_files = {s.file_id for s in r.symbols if s.kind == "module"}
    assert module_files == files_parsed


def test_pipeline_defuse_present(sample_root):
    r = analyze_project(sample_root)
    du = [u for u in r.defuses
          if u.symbol_id == "java/com/ex/Helper.java::Helper::help"]
    byvar = {u.var: u for u in du}
    assert byvar["x"].is_param and byvar["x"].param_index == 0
    assert byvar["y"].def_lines and byvar["y"].return_lines


def test_pipeline_deterministic(sample_root):
    a = analyze_project(sample_root).to_dict()
    b = analyze_project(sample_root).to_dict()
    assert a == b


def test_pipeline_broken_isolated(sample_root):
    r = analyze_project(sample_root)
    kinds = {(d.file, d.kind) for d in r.diagnostics}
    assert ("broken/broken.py", "parse_partial") in kinds or \
        ("broken/broken.py", "parse_error") in kinds
    # analysis still produced results for the healthy files
    assert any(f.id == "py/models.py" and f.parse_ok for f in r.files)
