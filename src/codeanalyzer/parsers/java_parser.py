"""Java parser."""
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

_CLASS_TYPES = {
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "annotation_type_declaration",
}
_METHOD_TYPES = {"method_declaration", "constructor_declaration"}
_SCOPE_TYPES = _CLASS_TYPES | _METHOD_TYPES


class JavaParser(LanguageParser):
    grammar = "java"

    def parse(self, sf) -> ParseOutput:
        root, out, mod = self._prepare(sf)
        func_nodes: list[tuple[str, Node]] = []

        # (node, chain, parent_sym_id, call_owner)
        stack = [(root, (), mod.id, mod.id)]
        while stack:
            node, chain, parent_id, owner = stack.pop()
            t = node.type

            if t == "package_declaration":
                for c in node.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        out.raw_imports.append(RawImport(
                            text_of(c), node.start_point[0] + 1, "package"))
                continue
            if t == "import_declaration":
                self._import(node, out)
                continue

            if t in _CLASS_TYPES:
                name = text_of(node.child_by_field_name("name")) or "<anon>"
                new_chain = chain + (name,)
                sym = self._make(sf.id, new_chain, name, node,
                                 SymbolKind.CLASS, parent_id, f"class {name}")
                out.symbols.append(sym)
                self._inherits(node, sym.id, out)
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, new_chain, sym.id, owner))
                continue

            if t in _METHOD_TYPES:
                name = text_of(node.child_by_field_name("name")) or "<init>"
                new_chain = chain + (name,)
                sym = self._make(sf.id, new_chain, name, node,
                                 SymbolKind.METHOD, parent_id,
                                 self._signature(node, name))
                out.symbols.append(sym)
                func_nodes.append((sym.id, node))
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, new_chain, sym.id, sym.id))
                continue

            if t == "method_invocation":
                self._call(node, owner, out)

            for c in reversed(node.children):
                stack.append((c, chain, parent_id, owner))

        for sym_id, node in func_nodes:
            self._defuse(sym_id, node, out)

        out.symbols.sort(key=lambda s: (s.span.start_line, s.id))
        return out

    # ------------------------------------------------------------------
    def _make(self, file_id, chain, name, node, kind, parent_id, sig) -> Symbol:
        return Symbol(
            id=self.make_symbol_id(file_id, list(chain)),
            name=name, kind=kind.value, file_id=file_id,
            span=span_of(node), parent_id=parent_id, signature=sig,
        )

    def _signature(self, node: Node, name: str) -> str:
        params = node.child_by_field_name("parameters")
        ret = node.child_by_field_name("type")
        rtext = text_of(ret) + " " if ret is not None else ""
        return f"{rtext}{name}{text_of(params) if params is not None else '()'}"

    def _import(self, node: Node, out: ParseOutput) -> None:
        is_static = any(c.type == "static" for c in node.children)
        wildcard = any(c.type == "asterisk" for c in node.children)
        target = None
        for c in node.children:
            if c.type in ("scoped_identifier", "identifier"):
                target = c
        if target is None:
            return
        raw = text_of(target)
        names: list[tuple[str, str]] = []
        if not wildcard:
            last = raw.split(".")[-1]
            names.append((last, last))
        out.raw_imports.append(RawImport(
            raw, node.start_point[0] + 1, "java_import",
            is_static=is_static, is_wildcard=wildcard, names=names))

    def _inherits(self, cls: Node, cls_id: str, out: ParseOutput) -> None:
        sc = cls.child_by_field_name("superclass")
        if sc is not None:
            for c in sc.children:
                if c.type in ("type_identifier", "scoped_type_identifier",
                              "generic_type"):
                    out.inherits.append(RawInherit(
                        cls_id, text_of(c).split("<")[0], c.start_point[0] + 1))
        ifaces = cls.child_by_field_name("interfaces")
        if ifaces is not None:
            for tl in ifaces.children:
                if tl.type == "type_list":
                    for c in tl.children:
                        if c.type in ("type_identifier",
                                      "scoped_type_identifier", "generic_type"):
                            out.inherits.append(RawInherit(
                                cls_id, text_of(c).split("<")[0],
                                c.start_point[0] + 1))

    def _call(self, node: Node, owner: str, out: ParseOutput) -> None:
        name = node.child_by_field_name("name")
        obj = node.child_by_field_name("object")
        out.calls.append(RawCall(
            owner, text_of(name), text_of(obj) if obj is not None else None,
            node.start_point[0] + 1))

    # -- def/use ---------------------------------------------------------
    def _defuse(self, sym_id: str, func: Node, out: ParseOutput) -> None:
        b = DefUseBuilder(sym_id)
        start = func.start_point[0] + 1
        params = func.child_by_field_name("parameters")
        if params is not None:
            idx = 0
            for c in params.children:
                if c.type in ("formal_parameter", "spread_parameter"):
                    nm = c.child_by_field_name("name")
                    if nm is None:
                        for x in c.children:
                            if x.type == "variable_declarator":
                                nm = x.child_by_field_name("name")
                    if nm is not None:
                        b.add_param(text_of(nm), start, idx)
                        idx += 1
        body = func.child_by_field_name("body")
        if body is None:
            out.defuses.extend(b.build())
            return
        stack = list(reversed(body.children))
        while stack:
            n = stack.pop()
            t = n.type
            if t in _SCOPE_TYPES:
                continue
            if t == "variable_declarator":
                nm = n.child_by_field_name("name")
                if nm is not None:
                    b.add_def(text_of(nm), n.start_point[0] + 1)
                val = n.child_by_field_name("value")
                if val is not None:
                    for ident in self._idents(val):
                        b.add_use(text_of(ident), ident.start_point[0] + 1)
                continue
            if t == "assignment_expression":
                left = n.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    b.add_def(text_of(left), n.start_point[0] + 1)
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

    def _idents(self, node: Node) -> list[Node]:
        out = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                out.append(n)
            elif n.type == "field_access":
                obj = n.child_by_field_name("object")
                if obj is not None:
                    stack.append(obj)
            elif n.type == "method_invocation":
                # count receiver + args as uses, not the method name
                obj = n.child_by_field_name("object")
                if obj is not None:
                    stack.append(obj)
                args = n.child_by_field_name("arguments")
                if args is not None:
                    stack.append(args)
            else:
                stack.extend(n.children)
        return out
