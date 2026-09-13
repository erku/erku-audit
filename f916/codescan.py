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

# --- Attribute access: ALLOWLIST, fail-closed -----------------------------
# A denylist of dangerous attribute names cannot win this game: countless
# introspection surfaces (generator/coroutine frame attrs like `gi_frame`,
# `f_builtins`, `f_globals`, `f_locals`, `cr_frame`, `f_back`, `f_code`,
# `gi_code`, ...) are neither dunder-shaped nor on any hand-maintained
# denylist, yet chain straight to `eval`/`exec`/`__builtins__`. So instead of
# asking "is this attr dangerous", every `ast.Attribute.attr` must instead be
# in this allowlist of exactly what a pure `(target, params) -> dict`
# measurement skill legitimately needs. Anything else -- known-dangerous or
# simply unanticipated -- is rejected. Over-blocking a rare legitimate attr
# is an acceptable cost for a security-critical, fail-closed scanner.
ALLOWED_ATTR_NAMES = {
    # str methods. str.format/str.format_map stay EXCLUDED: format()'s
    # replacement-field mini-language does attribute/subscript traversal at
    # runtime from inside a string literal, invisible to these AST checks;
    # f-strings and %/+ cover formatting. .encode IS allowed -- it only makes
    # bytes (and `sha256(s.encode()).hexdigest()` is the standard idiom).
    "strip", "lstrip", "rstrip", "casefold", "lower", "upper", "startswith", "endswith",
    "split", "rsplit", "splitlines", "join", "replace", "find", "rfind", "count",
    "isdigit", "isalnum", "isalpha", "isspace", "isnumeric", "zfill", "ljust", "rjust",
    "title", "partition", "rpartition", "removeprefix", "removesuffix", "encode",
    # dict methods
    "get", "items", "keys", "values", "setdefault", "pop",
    # list/set methods
    "append", "extend", "sort", "index", "add", "update", "union", "intersection", "difference",
    # re (module fns + Match methods -- all return strings/ints/None, no exec)
    "findall", "search", "match", "fullmatch", "sub", "escape",
    "group", "groups", "groupdict", "span", "start", "end",
    # statistics
    "median", "mean", "pstdev", "stdev", "variance", "pvariance", "fmean", "mode",
    # math
    "isfinite", "isnan", "isinf", "isclose", "inf", "nan", "pi", "e", "tau",
    "floor", "ceil", "trunc", "sqrt", "log", "log2", "log10", "exp", "pow",
    "fabs", "gcd", "hypot", "copysign", "comb", "perm", "prod",
    # json (JSONDecodeError is a benign exception class)
    "loads", "dumps", "JSONDecodeError",
    # hashlib
    "sha256", "md5", "sha1", "sha512", "hexdigest", "digest",
    # datetime
    "datetime", "date", "timezone", "utcfromtimestamp", "fromtimestamp", "fromisoformat",
    "strftime", "timestamp", "year", "month", "day", "hour", "minute", "second", "utc",
    # urllib.parse (including 'parse' itself, for `urllib.parse.<fn>` access)
    "urlsplit", "urlparse", "parse_qs", "parse_qsl", "unquote", "quote", "parse",
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
    - any Attribute access whose `.attr` (at ANY position in a chain, e.g.
      both `.sys` and `.modules` in `datetime.sys.modules`) is dunder-shaped
      (__x__) OR is not in ALLOWED_ATTR_NAMES -- a fail-closed allowlist of
      exactly what a pure measurement skill needs, since a denylist cannot
      keep up with introspection surfaces like `gi_frame`/`f_builtins`/
      `f_globals`/`cr_frame` that are neither dunder nor "obviously"
      dangerous;
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
                # Fail-closed ALLOWLIST, not a denylist: every segment of
                # every chain is checked here -- ast.walk visits each nested
                # Attribute node independently, so e.g. `gen.gi_frame`
                # yields its own Attribute node for `.gi_frame`, checked
                # regardless of what `gen` is. Anything not explicitly
                # allowlisted (a known-dangerous name, an introspection attr
                # nobody thought to denylist, or simply unanticipated) is
                # rejected.
                if _is_dunder(node.attr) or node.attr not in ALLOWED_ATTR_NAMES:
                    reasons.append(f"forbidden_attribute:{node.attr}")
    except Exception as exc:  # defense in depth: never raise out of a scan
        return False, [f"scan_error:{type(exc).__name__}"]

    seen = set()
    deduped = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return (not deduped), deduped
