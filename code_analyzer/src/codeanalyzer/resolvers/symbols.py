"""Symbol resolution (SPEC F3): build CALL and INHERIT edges.

Priority: (1) same-file scope -> (2) imported symbols -> (3) unique
project-wide name match.  Confidence is ``certain`` for same-file/import-bound
single matches, ``inferred`` for name-based matches, ``unresolved`` otherwise.
Several same-name candidates yield one ``ambiguous`` edge each at ``inferred``.

v1.6 Python type-aware narrowing (contract items a-d): for Python method calls
carrying a receiver, the name-matched candidate set is *narrowed* (never
widened) using the per-file :class:`TypeHints` symbol table and the INHERIT
ancestor map.  A narrowing that yields exactly one candidate promotes the edge
to ``certain`` (``ambiguous=False``); a narrowing to a smaller-but-still-plural
set keeps ``inferred``/``ambiguous`` but emits edges only to the reduced set.
When the receiver type is unknown or narrowing would empty the set, the
original resolution is left untouched.  Set ``CODE_ANALYZER_NO_TYPE_NARROWING``
to disable narrowing (used by the never-widen regression test).
"""
from __future__ import annotations

import os

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
        # class name -> set of direct base class names (built from INHERIT
        # edges during resolve()).
        self.bases_by_name: dict[str, set[str]] = {}
        self._narrow_enabled = (
            os.environ.get("CODE_ANALYZER_NO_TYPE_NARROWING") is None)

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

        # Pass 1: inheritance (needed to build the ancestor map before calls).
        for fid in sorted(self.outputs):
            out = self.outputs[fid]
            for inh in out.inherits:
                self._resolve_inherit(fid, inh, emit)
        self._build_ancestors(edges)

        # Pass 2: calls (type-aware narrowing applies here).
        for fid in sorted(self.outputs):
            out = self.outputs[fid]
            for call in out.calls:
                self._resolve_call(fid, call, emit)

        edges.sort(key=lambda e: (e.src, e.dst, e.kind, e.line))
        return edges

    # -- ancestor map ---------------------------------------------------
    def _build_ancestors(self, edges: list[Edge]) -> None:
        for e in edges:
            if e.kind != EdgeKind.INHERIT.value:
                continue
            child = self.by_id.get(e.src)
            if child is None:
                continue
            if e.dst in self.by_id:
                base = self.by_id[e.dst].name
            elif e.dst.startswith("unresolved:"):
                base = e.dst.split(":", 1)[1]
            else:
                continue
            self.bases_by_name.setdefault(child.name, set()).add(base)

    def _expand_ancestors(self, names: set[str]) -> set[str]:
        """Transitive closure of ``names`` over base classes (cycle-safe)."""
        out: set[str] = set()
        stack = sorted(names)
        while stack:
            n = stack.pop()
            if n in out:
                continue
            out.add(n)
            for base in sorted(self.bases_by_name.get(n, ())):
                if base not in out:
                    stack.append(base)
        return out

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

    def _emit_call_candidates(self, fid, call, cands, tier, line,
                              emit) -> bool:
        """Emit CALL edges for ``cands``, applying type-aware narrowing.

        ``cands`` is the name-matched set chosen by the resolution tier. When
        narrowing genuinely reduces it, a single survivor becomes ``certain``
        and a plural survivor stays ``inferred``/``ambiguous``; otherwise the
        standard tier-based confidence applies.
        """
        original = sorted(set(cands))
        if not original:
            return False
        src = call.caller_symbol_id
        kind = EdgeKind.CALL.value
        narrowed = self._narrow(fid, call, original)
        if narrowed is not None and len(narrowed) < len(original):
            if len(narrowed) == 1:
                emit(src, narrowed[0], kind, Confidence.CERTAIN.value,
                     False, line)
            else:
                for c in narrowed:
                    emit(src, c, kind, Confidence.INFERRED.value, True, line)
            return True
        return self._emit_candidates(src, original, kind, tier, line, emit)

    def _resolve_call(self, fid, call, emit) -> None:
        name = call.callee
        src = call.caller_symbol_id
        line = call.line
        kind = EdgeKind.CALL.value

        # (1) same-file scope
        same = self.file_name.get(fid, {}).get(name)
        if same and self._emit_call_candidates(fid, call, same, "file",
                                                line, emit):
            return
        # (2) imported symbols
        bound_file = self.bindings.get(fid, {}).get(name)
        if bound_file:
            cands = self.file_name.get(bound_file, {}).get(name)
            if cands and self._emit_call_candidates(fid, call, cands, "import",
                                                    line, emit):
                return
        # (3) project-wide
        if call.receiver is not None:
            cands = self.method_name.get(name) or self.proj_name.get(name)
        else:
            cands = self.proj_name.get(name)
        if cands and self._emit_call_candidates(fid, call, cands, "proj",
                                                line, emit):
            return
        emit(src, f"unresolved:{name}", kind, Confidence.UNRESOLVED.value,
             False, line)

    # -- type-aware narrowing (Python only) -----------------------------
    def _narrow(self, fid, call, cands: list[str]):
        """Return a subset of ``cands`` given the receiver type, or ``None``.

        ``None`` means "do not narrow" (unknown receiver type, non-Python file,
        narrowing disabled, or the filter would produce an empty set). The
        result is always a subset of ``cands`` (never widens).
        """
        if not self._narrow_enabled:
            return None
        if not fid.endswith(".py") or call.receiver is None:
            return None
        target = self._receiver_class_names(fid, call)
        if not target:
            return None
        allowed = self._expand_ancestors(target)
        filtered = [c for c in cands
                    if self._enclosing_class_name(c) in allowed]
        if not filtered or len(filtered) == len(cands):
            return None
        return sorted(set(filtered))

    def _receiver_class_names(self, fid, call):
        r = (call.receiver or "").strip()
        if not r:
            return None
        owner = call.caller_symbol_id
        th = self.outputs[fid].type_hints
        # (c) self./cls. receivers -> enclosing class
        if r in ("self", "cls"):
            cname = self._owner_class_name(owner)
            return {cname} if cname else None
        # self.attr / cls.attr -> class attribute type
        if r.startswith("self.") or r.startswith("cls."):
            attr = r.split(".", 1)[1]
            if "." in attr or "(" in attr or "[" in attr:
                return None
            cls_id = self._owner_class_id(owner)
            if cls_id is None:
                return None
            vt = th.class_attrs.get(cls_id, {}).get(attr)
            return self._interp_vartype(fid, vt) if vt is not None else None
        # (a/b) simple identifier -> scope variable; (d) direct class name
        if r.isidentifier():
            vt = self._lookup_var(fid, owner, r)
            if vt is not None:
                return self._interp_vartype(fid, vt)
            if r in self.class_name:
                return {r}
            return None
        # g().method() -> return annotation of g (module-local)
        if r.endswith(")") and "(" in r:
            head = r[:r.index("(")].strip()
            if head.isidentifier():
                rts = th.returns.get(head)
                if rts:
                    known = {n for n in rts if n in self.class_name}
                    return known or None
        return None

    def _interp_vartype(self, fid, vt):
        """Interpret a RawVarType against known project classes.

        Annotations are authoritative when they name a known class. Otherwise
        constructor assignments contribute their class (or a module-local
        factory function's return type); an assignment to a non-class/unknown
        callee, or to a non-call expression, poisons inference -> ``None``.
        """
        anno = {n for n in vt.annos if n in self.class_name}
        if anno:
            return anno
        if vt.annos:
            # annotation present but names no known project class -> skip
            return None
        classes: set[str] = set()
        poisoned = vt.other
        returns = self.outputs[fid].type_hints.returns
        for n in vt.ctors:
            if n in self.class_name:
                classes.add(n)
            else:
                known = {x for x in returns.get(n, ()) if x in self.class_name}
                if known:
                    classes |= known
                else:
                    poisoned = True
        if poisoned:
            return None
        return classes or None

    def _lookup_var(self, fid, owner, name):
        th = self.outputs[fid].type_hints
        sv = th.scope_vars.get(owner)
        if sv and name in sv:
            return sv[name]
        mod_id = fid + "::<module>"
        if owner != mod_id:
            sv = th.scope_vars.get(mod_id)
            if sv and name in sv:
                return sv[name]
        return None

    def _owner_class_id(self, owner):
        sid = owner
        while sid:
            s = self.by_id.get(sid)
            if s is None:
                return None
            pid = s.parent_id
            p = self.by_id.get(pid) if pid else None
            if p is not None and p.kind == SymbolKind.CLASS.value:
                return p.id
            sid = pid
        return None

    def _owner_class_name(self, owner):
        cid = self._owner_class_id(owner)
        return self.by_id[cid].name if cid is not None else None

    def _enclosing_class_name(self, sym_id):
        s = self.by_id.get(sym_id)
        if s is None or not s.parent_id:
            return None
        p = self.by_id.get(s.parent_id)
        if p is not None and p.kind == SymbolKind.CLASS.value:
            return p.name
        return None

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
