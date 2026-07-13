"""C / C++ parser.

Syntax-level only: no preprocessing, no template instantiation (SPEC scope).
"""
from __future__ import annotations

from tree_sitter import Node

from codeanalyzer.core.model import Language, Symbol, SymbolKind
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

_CLASS_TYPES = {"class_specifier", "struct_specifier"}
_SCOPE_TYPES = _CLASS_TYPES | {"function_definition", "namespace_definition"}
# tree-sitter-c/cpp node types that introduce a preprocessor conditional
# region: an #include whose closest such ancestor is one of these (and is
# not the whole-file include-guard wrapper, see _detect_include_guard) is
# marked Edge.conditional=True (v1.6).
_COND_TYPES = {"preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else"}


class CCppParser(LanguageParser):
    def __init__(self, grammar: str = "c") -> None:
        super().__init__()
        self.grammar = grammar

    @classmethod
    def for_file(cls, sf) -> "CCppParser":
        return cls("c" if sf.language == Language.C else "cpp")

    def parse(self, sf) -> ParseOutput:
        root, out, mod = self._prepare(sf)
        guard_node = self._detect_include_guard(root)
        func_nodes: list[tuple[str, Node]] = []
        chain_to_id: dict[tuple, str] = {(): mod.id}

        # (node, chain, parent_sym_id, in_class, in_conditional)
        stack = [(root, (), mod.id, False, False)]
        while stack:
            node, chain, parent_id, in_class, in_cond = stack.pop()
            t = node.type

            if t == "preproc_include":
                self._include(node, out, in_cond)
                continue

            if t == "namespace_definition":
                name = text_of(node.child_by_field_name("name"))
                new_chain = chain + (name,) if name else chain
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, new_chain, parent_id, False, in_cond))
                continue

            if t in _CLASS_TYPES:
                name = text_of(node.child_by_field_name("name"))
                if name:
                    new_chain = chain + (name,)
                    sym = self._make(sf.id, new_chain, name, node,
                                     SymbolKind.CLASS, parent_id, f"class {name}")
                    out.symbols.append(sym)
                    chain_to_id[new_chain] = sym.id
                    self._inherits(node, sym.id, out)
                    body = node.child_by_field_name("body")
                    if body is not None:
                        stack.append((body, new_chain, sym.id, True, in_cond))
                    continue

            if t == "function_definition":
                self._function(node, sf.id, chain, parent_id, in_class,
                                out, func_nodes, chain_to_id)
                continue

            if t == "call_expression":
                self._call(node, self._owner(chain, chain_to_id, mod.id), out)

            # Entering a conditional-preprocessor node makes everything
            # nested inside it "conditional", UNLESS this is the whole-file
            # include-guard wrapper (its direct content is unconditional;
            # further nested conditionals inside it still count).
            child_cond = in_cond
            if t in _COND_TYPES and node != guard_node:
                child_cond = True

            for c in reversed(node.children):
                stack.append((c, chain, parent_id, in_class, child_cond))

        for sym_id, node in func_nodes:
            self._defuse(sym_id, node, out)

        out.symbols.sort(key=lambda s: (s.span.start_line, s.id))
        return out

    # -- preprocessor conditionals ---------------------------------------
    def _detect_include_guard(self, root: Node) -> Node | None:
        """Detect the classic whole-file ``#ifndef GUARD / #define GUARD
        ... #endif`` include-guard pattern.

        Returns the ``preproc_ifdef`` node acting as the guard, or ``None``.
        Requirements (kept intentionally narrow -- SPEC scope is detection,
        not general preprocessor evaluation):
        - it is the only top-level construct in the file (comments aside),
          i.e. it spans to the end of the file;
        - it is written as ``#ifndef`` (not ``#ifdef``/``#if``);
        - the first statement inside its body is ``#define`` of the exact
          same identifier used in the ``#ifndef`` condition.
        """
        top_items = [c for c in root.children if c.type != "comment"]
        if len(top_items) != 1:
            return None
        guard = top_items[0]
        if guard.type != "preproc_ifdef":
            return None
        if not guard.children or guard.children[0].type != "#ifndef":
            return None
        name_node = guard.child_by_field_name("name")
        if name_node is None:
            return None
        guard_name = text_of(name_node)
        if not guard_name:
            return None
        # Body items: everything after the leading "#ifndef" token and the
        # condition identifier, and before the trailing "#endif" token.
        body_items = [c for c in guard.children[2:]
                      if c.type not in ("#endif", "comment")]
        if not body_items:
            return None
        first = body_items[0]
        if first.type != "preproc_def":
            return None
        def_name = first.child_by_field_name("name")
        if def_name is None or text_of(def_name) != guard_name:
            return None
        return guard

    # ------------------------------------------------------------------
    def _owner(self, chain, chain_to_id, mod_id):
        # calls at namespace/class scope attribute to nearest known symbol
        return chain_to_id.get(chain, mod_id)

    def _make(self, file_id, chain, name, node, kind, parent_id, sig) -> Symbol:
        return Symbol(
            id=self.make_symbol_id(file_id, list(chain)),
            name=name, kind=kind.value, file_id=file_id,
            span=span_of(node), parent_id=parent_id, signature=sig,
        )

    def _fdeclarator(self, node: Node):
        """Descend a function_definition declarator to the function_declarator."""
        d = node.child_by_field_name("declarator")
        while d is not None and d.type in (
                "pointer_declarator", "reference_declarator"):
            d = d.child_by_field_name("declarator")
        return d

    def _function(self, node, file_id, chain, parent_id, in_class, out,
                  func_nodes, chain_to_id) -> None:
        fdecl = self._fdeclarator(node)
        if fdecl is None or fdecl.type != "function_declarator":
            return
        namenode = fdecl.child_by_field_name("declarator")
        if namenode is None:
            return
        raw = text_of(namenode)
        if "::" in raw:
            parts = [p for p in raw.split("::") if p]
            # Out-of-line definition: qualify with the enclosing namespace
            # chain so the id matches the class symbol (e.g. geo::Circle::area).
            new_chain = chain + tuple(parts)
            name = parts[-1]
            kind = SymbolKind.METHOD
            pchain = new_chain[:-1]
            pid = chain_to_id.get(pchain, self.module_id(file_id))
        else:
            name = raw
            new_chain = chain + (name,)
            kind = SymbolKind.METHOD if in_class else SymbolKind.FUNCTION
            pid = parent_id
        sym = self._make(file_id, new_chain, name, node, kind, pid,
                         self._signature(node, fdecl))
        out.symbols.append(sym)
        chain_to_id[new_chain] = sym.id
        func_nodes.append((sym.id, node))
        # nested classes/functions inside body handled by defuse skip; but we
        # still descend to find nested types via a fresh scan of the body.
        body = node.child_by_field_name("body")
        # local calls handled in defuse-independent pass: re-scan body for calls
        if body is not None:
            self._scan_body_calls(body, sym.id, out)

    def _scan_body_calls(self, body: Node, owner: str, out: ParseOutput) -> None:
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "function_definition":
                continue  # nested function: separate owner (rare in C/C++)
            if n.type == "call_expression":
                self._call(n, owner, out)
            stack.extend(n.children)

    def _signature(self, node: Node, fdecl: Node) -> str:
        params = fdecl.child_by_field_name("parameters")
        ret = node.child_by_field_name("type")
        rtext = text_of(ret) + " " if ret is not None else ""
        name = text_of(fdecl.child_by_field_name("declarator"))
        return f"{rtext}{name}{text_of(params) if params is not None else '()'}"

    def _inherits(self, cls: Node, cls_id: str, out: ParseOutput) -> None:
        for c in cls.children:
            if c.type == "base_class_clause":
                for b in c.children:
                    if b.type == "type_identifier":
                        out.inherits.append(
                            RawInherit(cls_id, text_of(b), b.start_point[0] + 1))

    def _include(self, node: Node, out: ParseOutput, conditional: bool) -> None:
        path = node.child_by_field_name("path")
        if path is None:
            return
        line = node.start_point[0] + 1
        if path.type == "system_lib_string":
            raw = text_of(path).strip()
            if raw.startswith("<") and raw.endswith(">"):
                raw = raw[1:-1]
            out.raw_imports.append(RawImport(raw, line, "include", angle=True,
                                             conditional=conditional))
        else:  # string_literal
            raw = text_of(path).strip().strip('"')
            out.raw_imports.append(RawImport(raw, line, "include", angle=False,
                                             conditional=conditional))

    def _call(self, node: Node, owner: str, out: ParseOutput) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        line = node.start_point[0] + 1
        if fn.type == "identifier":
            out.calls.append(RawCall(owner, text_of(fn), None, line))
        elif fn.type == "field_expression":
            field = fn.child_by_field_name("field")
            arg = fn.child_by_field_name("argument")
            out.calls.append(RawCall(owner, text_of(field), text_of(arg), line))
        elif fn.type == "qualified_identifier":
            raw = text_of(fn)
            parts = raw.split("::")
            recv = "::".join(parts[:-1]) if len(parts) > 1 else None
            out.calls.append(RawCall(owner, parts[-1], recv, line))

    # -- def/use ---------------------------------------------------------
    def _defuse(self, sym_id: str, func: Node, out: ParseOutput) -> None:
        b = DefUseBuilder(sym_id)
        start = func.start_point[0] + 1
        fdecl = self._fdeclarator(func)
        if fdecl is not None:
            params = fdecl.child_by_field_name("parameters")
            if params is not None:
                idx = 0
                for c in params.children:
                    if c.type == "parameter_declaration":
                        nm = self._decl_name(c.child_by_field_name("declarator"))
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
            if t in ("function_definition",) or t in _CLASS_TYPES:
                continue
            if t == "init_declarator":
                nm = self._decl_name(n.child_by_field_name("declarator"))
                if nm:
                    b.add_def(nm, n.start_point[0] + 1)
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

    def _decl_name(self, decl) -> str:
        n = decl
        while n is not None:
            if n.type == "identifier":
                return text_of(n)
            if n.type in ("pointer_declarator", "reference_declarator",
                          "array_declarator", "init_declarator",
                          "parenthesized_declarator"):
                n = n.child_by_field_name("declarator")
                continue
            # fall back to first identifier child
            nxt = None
            for c in n.children:
                if c.type == "identifier":
                    return text_of(c)
                if "declarator" in c.type:
                    nxt = c
            n = nxt
        return ""

    def _idents(self, node: Node) -> list[Node]:
        out = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                out.append(n)
            elif n.type == "field_expression":
                arg = n.child_by_field_name("argument")
                if arg is not None:
                    stack.append(arg)
            else:
                stack.extend(n.children)
        return out
