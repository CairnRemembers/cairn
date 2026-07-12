"""
tests/test_codex_rewire.py — the Codex notify re-wire fix.

A Codex auto-update rewrites our install into
  [computer-use.exe, "turn-ended", "--previous-notify", "<stale cairn json>"]
where the buried cairn command is NOT executed. So:
  - _codex_is_installed must report that as NOT installed (else `install` no-ops),
  - install must strip the --previous-notify tail and re-wrap cairn as PRIMARY
    over the clean underlying command.
"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.__main__ import (_codex_is_installed, _underlying_notify,
                            _build_hook_notify, _read_notify_array,
                            _rewrite_notify_line, _toml_encode_str, _find_notify_span)

CU = "C:/x/codex-computer-use.exe"
PY = "C:/py/python.exe"
BURIED = '["' + PY + '","-X","utf8","-m","cairn","codex-hook","--chain","' + CU + '","--"]'


def test_installed_true_when_cairn_is_primary():
    n = [PY, "-X", "utf8", "-m", "cairn", "codex-hook", "--chain", CU, "turn-ended", "--"]
    assert _codex_is_installed(n) is True


def test_installed_false_when_cairn_buried_in_previous_notify():
    n = [CU, "turn-ended", "--previous-notify", BURIED]     # the broken shape
    assert _codex_is_installed(n) is False


def test_installed_false_on_plain_computer_use_and_empty():
    assert _codex_is_installed([CU, "turn-ended"]) is False
    assert _codex_is_installed([]) is False
    assert _codex_is_installed(None) is False


def test_underlying_strips_previous_notify_tail():
    assert _underlying_notify([CU, "turn-ended", "--previous-notify", BURIED]) == [CU, "turn-ended"]


def test_underlying_passes_clean_through():
    assert _underlying_notify([CU, "turn-ended"]) == [CU, "turn-ended"]
    assert _underlying_notify([]) == []
    assert _underlying_notify(None) == []


def test_reinstall_on_broken_config_recovers_primary():
    broken = [CU, "turn-ended", "--previous-notify", BURIED]
    assert _codex_is_installed(broken) is False            # install will proceed (no no-op)
    new = _build_hook_notify(PY, _underlying_notify(broken))
    assert new[:6] == [PY, "-X", "utf8", "-m", "cairn", "codex-hook"]   # cairn PRIMARY
    assert "--previous-notify" not in new                  # stale nest dropped
    assert CU in new and "turn-ended" in new               # computer-use preserved as chain
    assert _codex_is_installed(new) is True                # and now detected as installed


def test_full_rewrite_on_real_broken_config_shape():
    # Faithful reproduction of THIS machine's Codex-rewritten notify: computer-use
    # is PRIMARY and the cairn hook is buried in a --previous-notify JSON STRING
    # whose value CONTAINS a ] (the array-close inside the string). The old
    # non-greedy regex stopped at that inner ] and wrote MALFORMED TOML.
    CU = r"C:\Tools\OpenAI\Codex\bin\codex-computer-use.exe"
    PY = r"C:\Python314\python.exe"
    buried = json.dumps([PY, "-X", "utf8", "-m", "cairn", "codex-hook",
                         "--chain", CU, "turn-ended", "--"])
    broken_elems = [CU, "turn-ended", "--previous-notify", buried]
    config_text = ("notify = [" + ", ".join(_toml_encode_str(x) for x in broken_elems) + "]\n"
                   "\n[history]\nmax = 1000\n")

    # sanity: the broken config is itself valid TOML (Codex wrote it), and the span
    # finder locates the TRUE closing ] — not the one inside the buried string
    assert tomllib.loads(config_text)["notify"][0] == CU
    span = _find_notify_span(config_text)
    assert span is not None and config_text[span[1] - 1] == "]"
    assert config_text[span[1]:].lstrip().startswith("[history]")

    # 1. read current notify  2. strip underlying  3. build cairn-primary
    notify = _read_notify_array(config_text)
    assert _codex_is_installed(notify) is False and "--previous-notify" in notify
    chain = _underlying_notify(notify)
    assert chain == [CU, "turn-ended"]
    new_array = _build_hook_notify(PY, chain)

    # 4. full text rewrite  5. RESULT MUST PARSE as valid TOML (the bug did not)
    new_text, replaced = _rewrite_notify_line(config_text, new_array)
    assert replaced is True
    conf = tomllib.loads(new_text)                          # would raise before the fix
    assert conf["notify"][:6] == [PY, "-X", "utf8", "-m", "cairn", "codex-hook"]
    assert "--previous-notify" not in conf["notify"]
    assert CU in conf["notify"] and "turn-ended" in conf["notify"]
    assert conf["history"]["max"] == 1000                  # rest of the config untouched
    assert _codex_is_installed(conf["notify"]) is True
