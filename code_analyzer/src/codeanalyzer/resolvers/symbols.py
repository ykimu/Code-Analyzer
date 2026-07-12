"""Symbol resolution (SPEC F3): build CALL and INHERIT edges.

Priority: (1) same-file scope -> (2) imported symbols -> (3) unique
project-wide name match.  Confidence is ``certain`` for same-file/import-bound
single matches, ``inferred`` for name-based matches, ``unresolved`` otherwise.
Several same-name candidates yield one ``ambiguous`` edge each at ``inferred``.
"""
from __future__ import annotations

from codeanalyzer.core.model import (
    Confidence,
    Edge,
    EdgeKind,
    Symbol,
    SymbolKind,
)


class SymbolResolver:
    def __init__(self, symbols: list[Symbol], outputs,
                 bindings: dict[str, dict[str, str]]) -> None:
        self.symbols = symbols
        self.outputs = outputs
        self.bindings = bindings
        self.by_id = {s.id: s for s in symbols}
        # per-file name -> [symbol ids]
        self.file_name: dict[str, dict[str, list[str]]] = {}
        # project name -> [symbol ids]
        self.proj_name: dict[str, list[str]] = {}
        # project method-name -> [symbol ids] (for class-qualified matching)
        self.method_name: dict[str, list[str]] = {}
        # project class-name -> [symbol ids]
        self.class_name: dict[str, list[str]] = {}
        for s in symbols:
            if s.kind == SymbolKind.MODULE.value:
                continue
            self.file_name.setdefault(s.file_id, {}).setdefault(
                s.name, []).append(s.id)
            self.proj_name.setdefault(s.name, []).append(s.id)
            if s.kind == SymbolKind.METHOD.value:
                self.method_name.setdefault(s.name, []).append(s.id)
            if s.kind == SymbolKind.CLASS.value:
                self.class_name.setdefault(s.name, []).append(s.id)

    # ------------------------------------------------------------------
    def resolve(self) -> list[Edge]:
        edges: list[Edge] = []
        seen: set[tuple] = set()

        def emit(src, dst, kind, conf, ambiguous, line):
            e = Edge(src=src, dst=dst, kind=kind, confidence=conf,
                     ambiguous=ambiguous, line=line)
            k = e.key()
            if k not in seen:
                seen.add(k)
                edges.append(e)

        for fid in sorted(self.outputs):
            out = self.outputs[fid]
            for call in out.calls:
                self._resolve_call(fid, call, emit)
            for inh in out.inherits:
                self._resolve_inherit(fid, inh, emit)

        edges.sort(key=lambda e: (e.src, e.dst, e.kind, e.line))
        return edges

    # ------------------------------------------------------------------
    def _emit_candidates(self, src, cands, kind, tier, line, emit) -> bool:
        cands = sorted(set(cands))
        if not cands:
            return False
        if len(cands) == 1:
            conf = (Confidence.CERTAIN.value if tier in ("file", "import")
                    else Confidence.INFERRED.value)
            emit(src, cands[0], kind, conf, False, line)
        else:
            for c in cands:
                emit(src, c, kind, Confidence.INFERRED.value, True, line)
        return True

    def _resolve_call(self, fid, call, emit) -> None:
        name = call.callee
        src = call.caller_symbol_id
        line = call.line
        kind = EdgeKind.CALL.value

        # (1) same-file scope
        same = self.file_name.get(fid, {}).get(name)
        if same and self._emit_candidates(src, same, kind, "file", line, emit):
            return
        # (2) imported symbols
        bound_file = self.bindings.get(fid, {}).get(name)
        if bound_file:
            cands = self.file_name.get(bound_file, {}).get(name)
            if cands and self._emit_candidates(src, cands, kind, "import",
                                               line, emit):
                return
        # (3) project-wide
        if call.receiver is not None:
            cands = self.method_name.get(name) or self.proj_name.get(name)
        else:
            cands = self.proj_name.get(name)
        if cands and self._emit_candidates(src, cands, kind, "proj", line,
                                           emit):
            return
        emit(src, f"unresolved:{name}", kind, Confidence.UNRESOLVED.value,
             False, line)

    def _resolve_inherit(self, fid, inh, emit) -> None:
        raw = inh.base
        simple = raw.replace("::", ".").split(".")[-1]
        first = raw.replace("::", ".").split(".")[0]
        src = inh.class_symbol_id
        line = inh.line
        kind = EdgeKind.INHERIT.value

        # (1) same-file class
        same = [i for i in self.file_name.get(fid, {}).get(simple, [])
                if self.by_id[i].kind == SymbolKind.CLASS.value]
        if same and self._emit_candidates(src, same, kind, "file", line, emit):
            return
        # (2) import-bound
        bf = self.bindings.get(fid, {}).get(simple) or \
            self.bindings.get(fid, {}).get(first)
        if bf:
            cands = [i for i in self.file_name.get(bf, {}).get(simple, [])
                     if self.by_id[i].kind == SymbolKind.CLASS.value]
            if cands and self._emit_candidates(src, cands, kind, "import",
                                               line, emit):
                return
        # (3) project-wide unique class
        cands = self.class_name.get(simple)
        if cands and self._emit_candidates(src, cands, kind, "proj", line,
                                           emit):
            return
        emit(src, f"unresolved:{simple}", kind, Confidence.UNRESOLVED.value,
             False, line)


def resolve_symbols(symbols, outputs, bindings) -> list[Edge]:
    return SymbolResolver(symbols, outputs, bindings).resolve()
