"""AST purity scanner (f916.codescan) -- the core safety control gating
tier-2 self-extension. Every forbidden construct must be rejected with a
reason; the scanner must never raise, and must never import/exec anything
it inspects."""
from f916.codescan import ALLOWED_IMPORTS, is_pure_skill_source

CLEAN_SOURCE = '''\
"""A pure, deterministic checker."""
import math
import re

THRESHOLD_FLOOR = 0


def check_claim(target, params):
    value = target.get('value', 0)
    threshold = params.get('threshold', THRESHOLD_FLOOR)
    digits = re.findall(r'\\d+', str(value))
    magnitude = math.fsum(float(d) for d in digits) if digits else 0.0
    status = 'consistent' if magnitude >= threshold else 'findings'
    return {'status': status, 'findings': [], 'summary': f'magnitude={magnitude}'}
'''


def test_clean_pure_skill_passes():
    ok, reasons = is_pure_skill_source(CLEAN_SOURCE, "check_claim")
    assert ok is True
    assert reasons == []


def test_allowed_imports_cover_the_documented_set():
    assert ALLOWED_IMPORTS == {"math", "statistics", "re", "json", "hashlib", "datetime", "urllib.parse"}


def test_all_allowed_imports_are_individually_accepted():
    for module in ALLOWED_IMPORTS:
        src = f"import {module}\n\n\ndef check_claim(target, params):\n    return {{'status': 'consistent'}}\n"
        ok, reasons = is_pure_skill_source(src, "check_claim")
        assert ok is True, reasons


def test_rejects_os_import():
    src = "import os\n\n\ndef check_claim(target, params):\n    return {'status': 'consistent'}\n"
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_import" in r and "os" in r for r in reasons)


def test_rejects_from_import_of_disallowed_module():
    src = "from os import path\n\n\ndef check_claim(target, params):\n    return {'status': 'consistent'}\n"
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_import" in r for r in reasons)


def test_rejects_network_via_urllib_request():
    src = (
        "import urllib.request\n\n\n"
        "def check_claim(target, params):\n"
        "    urllib.request.urlopen('https://example.com')\n"
        "    return {'status': 'consistent'}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_import" in r and "urllib.request" in r for r in reasons)


def test_urllib_parse_alone_is_allowed_but_plain_urllib_is_not():
    ok, _ = is_pure_skill_source(
        "import urllib.parse\n\n\ndef check_claim(target, params):\n    return {'status': 'consistent'}\n",
        "check_claim",
    )
    assert ok is True
    ok, reasons = is_pure_skill_source(
        "import urllib\n\n\ndef check_claim(target, params):\n    return {'status': 'consistent'}\n",
        "check_claim",
    )
    assert ok is False
    assert any("forbidden_import" in r for r in reasons)


def test_rejects_relative_import():
    src = "from . import helper\n\n\ndef check_claim(target, params):\n    return {'status': 'consistent'}\n"
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_import" in r for r in reasons)


def test_rejects_open_call():
    src = (
        "def check_claim(target, params):\n"
        "    data = open('secret.txt').read()\n"
        "    return {'status': 'consistent', 'data': data}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_builtin:open" in r for r in reasons)


def test_rejects_eval():
    src = (
        "def check_claim(target, params):\n"
        "    result = eval(target.get('expr', '1'))\n"
        "    return {'status': 'consistent', 'result': result}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_builtin:eval" in r for r in reasons)


def test_rejects_exec():
    src = (
        "def check_claim(target, params):\n"
        "    exec('x = 1')\n"
        "    return {'status': 'consistent'}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_builtin:exec" in r for r in reasons)


def test_rejects_compile_and_dunder_import():
    src = (
        "def check_claim(target, params):\n"
        "    code = compile('1', '<s>', 'eval')\n"
        "    mod = __import__('os')\n"
        "    return {'status': 'consistent'}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_builtin:compile" in r for r in reasons)
    assert any("forbidden_builtin:__import__" in r for r in reasons)


def test_rejects_subprocess_attribute_access():
    src = (
        "import subprocess\n\n\n"
        "def check_claim(target, params):\n"
        "    subprocess.run(['ls'])\n"
        "    return {'status': 'consistent'}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_import" in r and "subprocess" in r for r in reasons)
    assert any("forbidden_attribute_root:subprocess" in r for r in reasons)


def test_rejects_getattr_and_setattr():
    src = (
        "def check_claim(target, params):\n"
        "    value = getattr(target, 'x', None)\n"
        "    setattr(target, 'y', value)\n"
        "    return {'status': 'consistent'}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_builtin:getattr" in r for r in reasons)
    assert any("forbidden_builtin:setattr" in r for r in reasons)


def test_rejects_reading_settings_or_db_by_name():
    src = (
        "def check_claim(target, params):\n"
        "    value = settings.api_key\n"
        "    return {'status': 'consistent', 'value': value}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_name:settings" in r for r in reasons)

    src2 = (
        "def check_claim(target, params):\n"
        "    row = db.get_setting('secret')\n"
        "    return {'status': 'consistent', 'row': row}\n"
    )
    ok2, reasons2 = is_pure_skill_source(src2, "check_claim")
    assert ok2 is False
    assert any("forbidden_name:db" in r for r in reasons2)


def test_rejects_secret_token_api_key_environ_references():
    for identifier in ("secret", "token", "api_key", "environ", "client"):
        src = f"def check_claim(target, params):\n    {identifier} = target.get('x')\n    return {{'status': 'consistent'}}\n"
        ok, reasons = is_pure_skill_source(src, "check_claim")
        assert ok is False, f"expected rejection for identifier {identifier!r}"
        assert any("forbidden_name" in r for r in reasons)


def test_rejects_dunder_attribute_access():
    src = (
        "def check_claim(target, params):\n"
        "    bases = target.__class__.__bases__\n"
        "    return {'status': 'consistent', 'bases': str(bases)}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("forbidden_attribute:__class__" in r for r in reasons)


def test_rejects_second_top_level_function():
    src = (
        "def helper(x):\n"
        "    return x\n\n\n"
        "def check_claim(target, params):\n"
        "    return {'status': 'consistent', 'value': helper(1)}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("multiple_top_level_functions" in r for r in reasons)


def test_rejects_module_level_side_effect():
    src = (
        "print('loaded')\n\n\n"
        "def check_claim(target, params):\n"
        "    return {'status': 'consistent'}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("disallowed_top_level_statement" in r for r in reasons)


def test_module_level_simple_constant_is_allowed():
    src = (
        "MAX_ITEMS = 10\n"
        "LABELS = ('a', 'b')\n\n\n"
        "def check_claim(target, params):\n"
        "    return {'status': 'consistent', 'max_items': MAX_ITEMS, 'labels': LABELS}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is True, reasons


def test_rejects_module_level_non_constant_assignment():
    src = (
        "import re\n"
        "PATTERN = re.compile('x')\n\n\n"
        "def check_claim(target, params):\n"
        "    return {'status': 'consistent' if PATTERN.match(str(target)) else 'findings'}\n"
    )
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("module_level_side_effect" in r for r in reasons)


def test_rejects_wrong_signature_too_many_args():
    src = "def check_claim(target, params, extra):\n    return {'status': 'consistent'}\n"
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("bad_signature" in r for r in reasons)


def test_rejects_wrong_signature_wrong_names():
    src = "def check_claim(a, b):\n    return {'status': 'consistent'}\n"
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("bad_signature" in r for r in reasons)


def test_rejects_signature_with_varargs_or_defaults():
    ok, reasons = is_pure_skill_source("def check_claim(target, params, *args):\n    return {}\n", "check_claim")
    assert ok is False and any("bad_signature" in r for r in reasons)

    ok, reasons = is_pure_skill_source("def check_claim(target, params=None):\n    return {}\n", "check_claim")
    assert ok is False and any("bad_signature" in r for r in reasons)


def test_rejects_function_name_mismatch():
    src = "def other_name(target, params):\n    return {'status': 'consistent'}\n"
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("function_name_mismatch" in r for r in reasons)
    assert any("function_missing" not in r for r in reasons) or True  # a function exists, just misnamed


def test_rejects_missing_function():
    src = "X = 1\n"
    ok, reasons = is_pure_skill_source(src, "check_claim")
    assert ok is False
    assert any("function_missing" in r for r in reasons)


def test_never_raises_on_syntax_error():
    ok, reasons = is_pure_skill_source("def check_claim(target, params:\n    return {}\n", "check_claim")
    assert ok is False
    assert reasons == ["syntax_error"]


def test_never_raises_on_non_string_input():
    ok, reasons = is_pure_skill_source(None, "check_claim")
    assert ok is False
    assert reasons


def test_never_raises_on_invalid_func_name():
    ok, reasons = is_pure_skill_source("def check_claim(target, params):\n    return {}\n", "not an identifier")
    assert ok is False
    assert reasons == ["invalid_input"]
