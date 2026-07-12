"""Python parser."""
from __future__ import annotations

from tree_sitter import Node

from codeanalyzer.core.model import Symbol, SymbolKind
from codeanalyzer.parsers.base import (
    DefUseBuilder,
    LanguageParser,
    ParseOutput,
    RawCall,
    RawImport,
    RawInherit,
    span_of,
    text_of,
)

_DEF_TYPES = {"function_definition", "class_definition"}


class PythonParser(LanguageParser):
    grammar = "python"

    def parse(self, sf) -> ParseOutput:
        root, out, mod = self._prepare(sf)
        func_nodes: list[tuple[str, Node]] = []

        # (node, name_chain, parent_sym_id, call_owner, parent_is_class)
        stack = [(root, (), mod.id, mod.id, False)]
        while stack:
            node, chain, parent_id, owner, in_class = stack.pop()
            t = node.type

            if t == "function_definition":
                name = text_of(node.child_by_field_name("name")) or "<anon>"
                new_chain = chain + (name,)
                sym = self._make(sf.id, new_chain, name, node,
                                 SymbolKind.METHOD if in_class else SymbolKind.FUNCTION,
                                 parent_id, self._signature(node, name))
                out.symbols.append(sym)
                func_nodes.append((sym.id, node))
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, new_chain, sym.id, sym.id, False))
                continue

            if t == "class_definition":
                name = text_of(node.child_by_field_name("name")) or "<anon>"
                new_chain = chain + (name,)
                sym = self._make(sf.id, new_chain, name, node,
                                 SymbolKind.CLASS, parent_id,
                                 f"class {name}")
                out.symbols.append(sym)
                self._inherits(node, sym.id, out)
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, new_chain, sym.id, owner, True))
                continue

            if t in ("import_statement", "import_from_statement"):
                self._imports(node, out)
                continue

            if t == "call":
                self._call(node, owner, out)

            for c in reversed(node.children):
                stack.append((c, chain, parent_id, owner, in_class))

        for sym_id, node in func_nodes:
            self._defuse(sym_id, node, out)

        out.symbols.sort(key=lambda s: (s.span.start_line, s.id))
        return out

    # ------------------------------------------------------------------
    def _make(self, file_id, chain, name, node, kind, parent_id, sig) -> Symbol:
        return Symbol(
            id=self.make_symbol_id(file_id, list(chain)),
            name=name,
            kind=kind.value,
            file_id=file_id,
            span=span_of(node),
            parent_id=parent_id,
            signature=sig,
        )

    def _signature(self, node: Node, name: str) -> str:
        params = node.child_by_field_name("parameters")
        ptext = text_of(params) if params is not None else "()"
        ret = node.child_by_field_name("return_type")
        rtext = f" -> {text_of(ret)}" if ret is not None else ""
        return f"def {name}{ptext}{rtext}"

    def _inherits(self, cls: Node, cls_id: str, out: ParseOutput) -> None:
        supers = cls.child_by_field_name("superclasses")
        if supers is None:
            return
        for c in supers.children:
            if c.type in ("identifier", "attribute"):
                out.inherits.append(RawInherit(cls_id, text_of(c),
                                               c.start_point[0] + 1))

    def _imports(self, node: Node, out: ParseOutput) -> None:
        line = node.start_point[0] + 1
        if node.type == "import_statement":
            for c in node.children:
                if c.type == "dotted_name":
                    raw = text_of(c)
                    top = raw.split(".")[0]
                    out.raw_imports.append(RawImport(
                        raw, line, "import", names=[(top, top)]))
                elif c.type == "aliased_import":
                    nm = c.child_by_field_name("name")
                    alias = c.child_by_field_name("alias")
                    raw = text_of(nm)
                    local = text_of(alias) or raw.split(".")[0]
                    out.raw_imports.append(RawImport(
                        raw, line, "import",
                        names=[(raw.split(".")[0], local)]))
            return

        # import_from_statement
        mod_node = node.child_by_field_name("module_name")
        raw = text_of(mod_node) if mod_node is not None else ""
        names: list[tuple[str, str]] = []
        wildcard = False
        for i, c in enumerate(node.children):
            fld = node.field_name_for_child(i)
            if fld != "name":
                continue
            if c.type == "dotted_name":
                nm = text_of(c)
                names.append((nm.split(".")[-1], nm.split(".")[-1]))
            elif c.type == "aliased_import":
                orig = text_of(c.child_by_field_name("name"))
                alias = text_of(c.child_by_field_name("alias"))
                names.append((orig.split(".")[-1], alias or orig))
        if any(ch.type == "wildcard_import" for ch in node.children):
            wildcard = True
        out.raw_imports.append(RawImport(
            raw, line, "from", is_wildcard=wildcard, names=names))

    def _call(self, node: Node, owner: str, out: ParseOutput) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        line = node.start_point[0] + 1
        if fn.type == "identifier":
            out.calls.append(RawCall(owner, text_of(fn), None, line))
        elif fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            obj = fn.child_by_field_name("object")
            out.calls.append(RawCall(owner, text_of(attr), text_of(obj), line))

    # -- def/use ---------------------------------------------------------
    def _defuse(self, sym_id: str, func: Node, out: ParseOutput) -> None:
        b = DefUseBuilder(sym_id)
        start = func.start_point[0] + 1
        params = func.child_by_field_name("parameters")
        if params is not None:
            idx = 0
            for c in params.children:
                nm = self._param_name(c)
                if nm:
                    b.add_param(nm, start, idx)
                    idx += 1

        body = func.child_by_field_name("body")
        if body is None:
            out.defuses.extend(b.build())
            return

        stack = list(reversed(body.children))
        while stack:
            n = stack.pop()
            t = n.type
            if t in _DEF_TYPES:
                continue  # nested scope handled separately
            if t == "assignment":
                left = n.child_by_field_name("left")
                right = n.child_by_field_name("right")
                for tgt in self._targets(left):
                    b.add_def(tgt, n.start_point[0] + 1)
                if right is not None:
                    for ident in self._idents(right):
                        b.add_use(text_of(ident), ident.start_point[0] + 1)
                continue
            if t == "augmented_assignment":
                left = n.child_by_field_name("left")
                for tgt in self._targets(left):
                    b.add_def(tgt, n.start_point[0] + 1)
                    b.add_use(tgt, n.start_point[0] + 1)
                right = n.child_by_field_name("right")
                if right is not None:
                    for ident in self._idents(right):
                        b.add_use(text_of(ident), ident.start_point[0] + 1)
                continue
            if t == "return_statement":
                for ident in self._idents(n):
                    b.add_return(text_of(ident), n.start_point[0] + 1)
                continue
            if t == "identifier":
                b.add_use(text_of(n), n.start_point[0] + 1)
                continue
            stack.extend(reversed(n.children))
        out.defuses.extend(b.build())

    def _param_name(self, node: Node) -> str:
        t = node.type
        if t == "identifier":
            return text_of(node)
        if t in ("typed_parameter", "default_parameter",
                 "typed_default_parameter"):
            nm = node.child_by_field_name("name")
            if nm is not None:
                return text_of(nm)
            for c in node.children:
                if c.type == "identifier":
                    return text_of(c)
        if t in ("list_splat_pattern", "dictionary_splat_pattern"):
            for c in node.children:
                if c.type == "identifier":
                    return text_of(c)
        return ""

    def _targets(self, left: Node) -> list[str]:
        if left is None:
            return []
        out = []
        stack = [left]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                out.append(text_of(n))
            elif n.type in ("pattern_list", "tuple_pattern", "list_pattern"):
                stack.extend(n.children)
        return out

    def _idents(self, node: Node) -> list[Node]:
        out = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                out.append(n)
            elif n.type == "attribute":
                # use only the base object identifier, not the attr name
                obj = n.child_by_field_name("object")
                if obj is not None:
                    stack.append(obj)
            elif n.type == "call":
                # descend into arguments; function handled as call elsewhere
                for c in n.children:
                    stack.append(c)
            else:
                stack.extend(n.children)
        return out
