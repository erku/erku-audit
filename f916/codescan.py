"""AST purity scanner for LLM-authored skill proposals.

This is the core safety control for tier-2 self-extension (see
`f916/selfext.py`): it decides whether a generated `(target, params) ->
dict` function is even eligible to be handed to the isolated
`SandboxClient` for its tests to be run. It never imports, execs, or
otherwise runs the source it inspects -- only `ast.parse` and read-only AST
walking are used. A candidate is accepted only if it is pure by
construction: no I/O, no network, no secrets, no dynamic eval, and exactly
one top-level function with the exact required signature.

This module makes no promise beyond that static check; the sandbox test run
is a separate, required gate (see `f916.selfext._tier2`).
"""
from __future__ import annotations

import ast

# The only modules a generated skill may import. Exact dotted names only:
# `import urllib.parse` / `from urllib.parse import ...` are allowed, but
# plain `urllib` or `urllib.request` are not.
ALLOWED_IMPORTS = {"math", "statistics", "re", "json", "hashlib", "datetime", "urllib.parse"}

_FORBIDDEN_BUILTINS = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "getattr", "setattr", "delattr", "globals", "locals", "vars", "memoryview",
}

_FORBIDDEN_ATTR_ROOTS = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib", "builtins",
}

# Rejected wherever they appear as an `ast.Attribute.attr` name -- at ANY
# position in a chain, not just the root. This closes the re-export bypass
# where an allowlisted module attribute-chains into a dangerous one, e.g.
# `datetime.sys.modules['os'].system(...)` (datetime is allowlisted, but
# `.sys`, `.modules`, and `.system` are not). Deliberately narrow: it must
# never catch ordinary container/string methods a pure skill needs, such as
# .get/.items/.keys/.values/.append/.sort/.strip/.casefold/.startswith, nor
# legitimate module functions like re.findall, statistics.median,
# urllib.parse.urlsplit/parse_qsl, math.isfinite, hashlib.sha256, or
# datetime.datetime -- none of those names appear here.
FORBIDDEN_ATTR_NAMES = {
    "sys", "os", "subprocess", "socket", "shutil", "pathlib", "importlib", "builtins",
    "environ", "modules", "system", "popen", "spawn",
    "exec", "eval", "compile", "getattr", "setattr", "delattr", "globals", "locals", "vars", "open",
}

# Any identifier (Name or Attribute) containing one of these substrings
# (case-insensitive) is rejected outright: a pure `(target, params) -> dict`
# checker never needs to reference settings, a db/client handle, or a
# secret/token/api_key/environment variable.
_FORBIDDEN_NAME_SUBSTRINGS = ("settings", "db", "client", "secret", "token", "api_key", "environ")


def _is_dunder(name):
    return isinstance(name, str) and len(name) > 4 and name.startswith("__") and name.endswith("__")


def _contains_forbidden_substring(name):
    if not isinstance(name, str):
        return False
    lowered = name.casefold()
    return any(substring in lowered for substring in _FORBIDDEN_NAME_SUBSTRINGS)


def _attr_root(node):
    """Walk down an Attribute/Subscript chain to the leftmost Name id, or
    None if the chain does not bottom out in a bare name (e.g. a call
    result)."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_simple_constant(node):
    """A module-level value literal enough to be a harmless constant:
    a Constant, or a tuple/list/set/dict built only from such values."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_simple_constant(elt) for elt in node.elts)
    if isinstance(node, ast.Dict):
        return (all(k is None or _is_simple_constant(k) for k in node.keys)
                and all(_is_simple_constant(v) for v in node.values))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_simple_constant(node.operand)
    return False


def _signature_ok(args) -> bool:
    if args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
        return False
    if args.defaults or any(d is not None for d in args.kw_defaults):
        return False
    return [a.arg for a in args.args] == ["target", "params"]


def is_pure_skill_source(src: str, func_name: str) -> tuple:
    """Parse `src` with ast; return (ok, reasons).

    REJECT (ok False, reasons non-empty) if any:
    - import/from-import of a module not in ALLOWED_IMPORTS (including any
      relative import);
    - any Name/Call referencing a forbidden builtin: eval, exec, compile,
      __import__, open, input, getattr, setattr, globals, locals, vars,
      memoryview;
    - any Attribute access whose root name is os, sys, subprocess, socket,
      shutil, pathlib, importlib, builtins, OR whose `.attr` at ANY position
      in the chain is one of FORBIDDEN_ATTR_NAMES (this closes the
      allowlisted-module re-export bypass, e.g.
      `datetime.sys.modules['os'].system(...)`), or any Name/Attribute whose
      name is dunder-shaped (__x__);
    - any Name/Attribute referencing 'settings', 'db', 'client', 'secret',
      'token', 'api_key', or 'environ' (case-insensitive substring);
    - the module has any top-level statement other than imports, an
      optional docstring, simple literal constant assignments, and the
      single required `def <func_name>`;
    - the required top-level function `func_name` is absent, duplicated, or
      its signature is not exactly `(target, params)`.

    Pure by construction when accepted: no I/O, no network, no secrets, no
    dynamic eval. Never raises -- a SyntaxError or any other parse/walk
    failure is reported as (False, [...]).
    """
    if not isinstance(src, str) or not isinstance(func_name, str) or not func_name.isidentifier():
        return False, ["invalid_input"]

    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return False, ["syntax_error"]

    reasons = []
    try:
        found_func = False
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue  # module docstring
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                if node.value is None or not _is_simple_constant(node.value):
                    reasons.append("module_level_side_effect")
                continue
            if isinstance(node, ast.FunctionDef):
                if found_func:
                    reasons.append("multiple_top_level_functions")
                    continue
                found_func = True
                if node.name != func_name:
                    reasons.append("function_name_mismatch")
                if not _signature_ok(node.args):
                    reasons.append("bad_signature")
                continue
            reasons.append("disallowed_top_level_statement:" + type(node).__name__)

        if not found_func:
            reasons.append("function_missing")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name not in ALLOWED_IMPORTS:
                        reasons.append(f"forbidden_import:{alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level or module not in ALLOWED_IMPORTS:
                    reasons.append(f"forbidden_import:{'.' * node.level}{module}")
            elif isinstance(node, ast.Name):
                if node.id in _FORBIDDEN_BUILTINS:
                    reasons.append(f"forbidden_builtin:{node.id}")
                if _is_dunder(node.id) or _contains_forbidden_substring(node.id):
                    reasons.append(f"forbidden_name:{node.id}")
            elif isinstance(node, ast.Attribute):
                # Every segment of the chain is checked here -- ast.walk
                # visits each nested Attribute node independently, so
                # `datetime.sys.modules` yields separate Attribute nodes for
                # `.sys` and `.modules`, both checked against
                # FORBIDDEN_ATTR_NAMES regardless of the (allowlisted) root.
                if (_is_dunder(node.attr) or node.attr in FORBIDDEN_ATTR_NAMES
                        or _contains_forbidden_substring(node.attr)):
                    reasons.append(f"forbidden_attribute:{node.attr}")
                root = _attr_root(node)
                if root in _FORBIDDEN_ATTR_ROOTS:
                    reasons.append(f"forbidden_attribute_root:{root}")
            elif isinstance(node, ast.Subscript):
                # Defense in depth: a forbidden attribute/module reached via
                # subscript (e.g. `x.modules['os']`) is already rejected by
                # the Attribute check above on the same walk, but the base
                # is re-checked explicitly here in case of future refactors.
                base = node.value
                if isinstance(base, ast.Attribute) and (_is_dunder(base.attr) or base.attr in FORBIDDEN_ATTR_NAMES):
                    reasons.append(f"forbidden_subscript_base:{base.attr}")
                elif isinstance(base, ast.Name) and (base.id in _FORBIDDEN_ATTR_ROOTS or base.id in FORBIDDEN_ATTR_NAMES):
                    reasons.append(f"forbidden_subscript_base:{base.id}")
    except Exception as exc:  # defense in depth: never raise out of a scan
        return False, [f"scan_error:{type(exc).__name__}"]

    seen = set()
    deduped = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return (not deduped), deduped
