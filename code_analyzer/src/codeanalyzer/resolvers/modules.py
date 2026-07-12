"""Module / import resolution (SPEC F2).

Resolves each parser ``RawImport`` to a project file id, an ``external:<pkg>``
marker, or an ``unresolved:<raw>`` marker, producing IMPORT edges and
``unresolved_import`` diagnostics.  Also returns per-file *bindings*
(local name -> project file id) consumed by symbol resolution.
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field

from codeanalyzer.core.model import (
    Confidence,
    Diagnostic,
    Edge,
    EdgeKind,
    Language,
)

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


@dataclass
class ResolveResult:
    edges: list[Edge] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # file_id -> { local_name: project_file_id }
    bindings: dict[str, dict[str, str]] = field(default_factory=dict)
    # fully-qualified java class name -> file_id
    java_index: dict[str, str] = field(default_factory=dict)


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


class ModuleResolver:
    def __init__(self, files, outputs, root, compile_commands=None) -> None:
        self.files = list(files)
        self.outputs = outputs
        self.root = root
        self.ids: set[str] = {f.id for f in files}
        self.lang: dict[str, Language] = {f.id: f.language for f in files}
        self._build_python_index()
        self._build_java_index()
        self._load_tsconfig()
        self._load_compile_commands(compile_commands)

    # -- index building --------------------------------------------------
    def _py_source_roots(self) -> list[str]:
        roots = {""}
        for fid in self.ids:
            parts = fid.split("/")
            for i, p in enumerate(parts[:-1]):
                if p == "src":
                    roots.add("/".join(parts[: i + 1]) + "/")
        return sorted(roots)

    def _build_python_index(self) -> None:
        # dotted module -> set of file ids
        self.py_modules: dict[str, set[str]] = {}
        self.py_roots = self._py_source_roots()
        for fid in sorted(self.ids):
            if not fid.endswith(".py"):
                continue
            for r in self.py_roots:
                if r and not fid.startswith(r):
                    continue
                rel = fid[len(r):] if r else fid
                rel = rel[:-3]  # strip .py
                if rel.endswith("/__init__"):
                    rel = rel[: -len("/__init__")]
                if not rel:
                    continue
                dotted = rel.replace("/", ".")
                self.py_modules.setdefault(dotted, set()).add(fid)

    def _build_java_index(self) -> None:
        self.java_fqn: dict[str, str] = {}          # fqcn -> file
        self.java_pkg: dict[str, list[str]] = {}     # package -> files
        for f in self.files:
            if f.language != Language.JAVA:
                continue
            out = self.outputs.get(f.id)
            if out is None:
                continue
            pkg = ""
            for imp in out.raw_imports:
                if imp.kind == "package":
                    pkg = imp.raw
                    break
            self.java_pkg.setdefault(pkg, []).append(f.id)
            # primary type = first top-level class symbol
            for s in out.symbols:
                if s.kind == "class" and s.parent_id and \
                        s.parent_id.endswith("::<module>"):
                    fqcn = (pkg + "." + s.name) if pkg else s.name
                    self.java_fqn.setdefault(fqcn, f.id)

    def _load_tsconfig(self) -> None:
        self.ts_base = ""
        self.ts_paths: dict[str, list[str]] = {}
        cfg = self.root / "tsconfig.json"
        if not cfg.is_file():
            return
        try:
            data = json.loads(_strip_jsonc(cfg.read_text(
                encoding="utf-8", errors="replace")))
        except Exception:  # noqa: BLE001
            return
        opts = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
        base = opts.get("baseUrl", "") or ""
        self.ts_base = posixpath.normpath(base) if base not in ("", ".") else ""
        if self.ts_base == ".":
            self.ts_base = ""
        paths = opts.get("paths", {})
        if isinstance(paths, dict):
            self.ts_paths = {k: list(v) for k, v in paths.items()
                             if isinstance(v, list)}

    def _load_compile_commands(self, compile_commands) -> None:
        self.c_include_dirs: list[str] = []
        if not compile_commands:
            return
        try:
            from pathlib import Path
            data = json.loads(Path(compile_commands).read_text(
                encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return
        dirs: set[str] = set()
        for entry in data if isinstance(data, list) else []:
            base = entry.get("directory", "")
            args: list[str] = []
            if "arguments" in entry:
                args = entry["arguments"]
            elif "command" in entry:
                args = entry["command"].split()
            for a in args:
                inc = None
                if a.startswith("-I") and len(a) > 2:
                    inc = a[2:]
                if inc is not None:
                    from pathlib import Path
                    p = Path(inc)
                    if not p.is_absolute() and base:
                        p = Path(base) / inc
                    try:
                        rel = p.resolve().relative_to(self.root).as_posix()
                        dirs.add(rel)
                    except (ValueError, OSError):
                        pass
        self.c_include_dirs = sorted(dirs)

    # -- resolution ------------------------------------------------------
    def resolve(self) -> ResolveResult:
        res = ResolveResult(java_index=self.java_fqn)
        seen: set[tuple] = set()
        for f in self.files:
            out = self.outputs.get(f.id)
            if out is None:
                continue
            bindings: dict[str, str] = {}
            for imp in out.raw_imports:
                if imp.kind == "package":
                    continue
                dsts = self._resolve_one(f, imp, bindings)
                for dst in dsts:
                    edge = Edge(
                        src=f.id, dst=dst, kind=EdgeKind.IMPORT.value,
                        confidence=(Confidence.UNRESOLVED.value
                                    if dst.startswith("unresolved:")
                                    else Confidence.CERTAIN.value),
                        line=imp.line,
                    )
                    k = edge.key()
                    if k in seen:
                        continue
                    seen.add(k)
                    res.edges.append(edge)
                    if dst.startswith("unresolved:"):
                        res.diagnostics.append(Diagnostic(
                            f.id, "unresolved_import",
                            f"could not resolve import {imp.raw!r}", imp.line))
            if bindings:
                res.bindings[f.id] = bindings
        res.edges.sort(key=lambda e: (e.src, e.dst, e.kind, e.line))
        res.diagnostics.sort(key=lambda d: (d.file, d.kind, d.line))
        return res

    def _resolve_one(self, f, imp, bindings) -> list[str]:
        lang = f.language
        if lang == Language.PYTHON:
            return self._py(f, imp, bindings)
        if lang in (Language.JAVASCRIPT, Language.TYPESCRIPT):
            return self._js(f, imp, bindings)
        if lang in (Language.C, Language.CPP):
            return self._c(f, imp, bindings)
        if lang == Language.JAVA:
            return self._java(f, imp, bindings)
        return [self._external(imp.raw)]

    # -- Python ----------------------------------------------------------
    def _py_lookup(self, dotted: str):
        cands = self.py_modules.get(dotted)
        if cands:
            return sorted(cands)[0]
        return None

    def _py(self, f, imp, bindings) -> list[str]:
        results: list[str] = []
        if imp.kind == "import":
            dotted = imp.raw
            hit = self._py_lookup(dotted)
            while hit is None and "." in dotted:
                dotted = dotted.rsplit(".", 1)[0]
                hit = self._py_lookup(dotted)
            if hit:
                results.append(hit)
                for orig, local in imp.names:
                    bindings.setdefault(local, hit)
            else:
                results.append(self._external(imp.raw.split(".")[0]))
            return results

        # from ... import
        raw = imp.raw
        dots = len(raw) - len(raw.lstrip("."))
        base_mod = None
        if dots:
            # relative import
            pkg_parts = f.id[:-3].split("/")  # drop .py
            if pkg_parts and pkg_parts[-1] == "__init__":
                pkg_parts = pkg_parts[:-1]
            else:
                pkg_parts = pkg_parts[:-1]  # containing package
            up = dots - 1
            if up:
                pkg_parts = pkg_parts[:-up] if up <= len(pkg_parts) else []
            # strip source-root prefix from pkg_parts
            base_pkg = "/".join(pkg_parts)
            for r in self.py_roots:
                if r and base_pkg.startswith(r.rstrip("/")):
                    base_pkg = base_pkg[len(r):]
                    break
            tail = raw[dots:]
            base_mod = ".".join(p for p in
                                (base_pkg.replace("/", "."), tail) if p)
        else:
            base_mod = raw

        hit = self._py_lookup(base_mod) if base_mod else None
        if hit:
            results.append(hit)
        # imported names may be submodules of a package
        for orig, local in imp.names:
            sub = f"{base_mod}.{orig}" if base_mod else orig
            subhit = self._py_lookup(sub)
            if subhit:
                results.append(subhit)
                bindings.setdefault(local, subhit)
            elif hit:
                bindings.setdefault(local, hit)
        if results:
            return sorted(set(results))
        # unresolved
        if dots:
            return [f"unresolved:{imp.raw}"]
        return [self._external(imp.raw.split(".")[0])]

    # -- JS / TS ---------------------------------------------------------
    def _node_resolve(self, target_rel: str):
        target_rel = posixpath.normpath(target_rel)
        # direct with extension
        if target_rel in self.ids:
            return target_rel
        for ext in _JS_EXTS:
            if target_rel + ext in self.ids:
                return target_rel + ext
        for ext in _JS_EXTS:
            cand = posixpath.normpath(target_rel + "/index" + ext)
            if cand in self.ids:
                return cand
        # already had an extension that we should strip and retry
        return None

    def _js(self, f, imp, bindings) -> list[str]:
        raw = imp.raw
        hit = None
        if raw.startswith("."):
            base = posixpath.dirname(f.id)
            hit = self._node_resolve(posixpath.join(base, raw))
        else:
            hit = self._js_alias(raw)
        if hit:
            for orig, local in imp.names:
                bindings.setdefault(local, hit)
            return [hit]
        if raw.startswith("."):
            return [f"unresolved:{raw}"]
        return [self._external(self._js_pkg(raw))]

    def _js_alias(self, raw: str):
        for pattern, targets in sorted(self.ts_paths.items()):
            if "*" in pattern:
                pre, _, post = pattern.partition("*")
                if raw.startswith(pre) and raw.endswith(post):
                    mid = raw[len(pre): len(raw) - len(post) if post else None]
                    for t in targets:
                        sub = t.replace("*", mid)
                        cand = self._node_resolve(
                            posixpath.join(self.ts_base, sub))
                        if cand:
                            return cand
            elif raw == pattern:
                for t in targets:
                    cand = self._node_resolve(posixpath.join(self.ts_base, t))
                    if cand:
                        return cand
        # bare baseUrl resolution
        if self.ts_base or True:
            cand = self._node_resolve(posixpath.join(self.ts_base, raw))
            if cand:
                return cand
        return None

    @staticmethod
    def _js_pkg(raw: str) -> str:
        if raw.startswith("@"):
            parts = raw.split("/")
            return "/".join(parts[:2])
        return raw.split("/")[0]

    # -- C / C++ ---------------------------------------------------------
    def _c(self, f, imp, bindings) -> list[str]:
        raw = imp.raw
        search = [posixpath.dirname(f.id), ""] + list(self.c_include_dirs)
        for d in search:
            cand = posixpath.normpath(posixpath.join(d, raw)) if d else \
                posixpath.normpath(raw)
            if cand in self.ids:
                return [cand]
            # also try just the basename anywhere is too loose; skip
        if imp.angle:
            return [self._external(posixpath.basename(raw))]
        return [f"unresolved:{raw}"]

    # -- Java ------------------------------------------------------------
    def _java(self, f, imp, bindings) -> list[str]:
        raw = imp.raw
        if imp.is_wildcard:
            pkg = raw
            files = self.java_pkg.get(pkg)
            if files:
                return sorted(set(files) - {f.id})
            return [self._external(raw.split(".")[0])]
        fqcn = raw
        if imp.is_static:
            fqcn = raw.rsplit(".", 1)[0]  # a.b.C.d -> a.b.C
        hit = self.java_fqn.get(fqcn)
        if hit:
            for orig, local in imp.names:
                bindings.setdefault(local, hit)
            # also bind the class simple name
            bindings.setdefault(fqcn.split(".")[-1], hit)
            return [hit]
        return [self._external(raw.split(".")[0])]

    # -- misc ------------------------------------------------------------
    @staticmethod
    def _external(name: str) -> str:
        return f"external:{name}"


def resolve_modules(files, outputs, root, compile_commands=None) -> ResolveResult:
    return ModuleResolver(files, outputs, root, compile_commands).resolve()
