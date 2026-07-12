"""
tests/test_redact_corpus.py — the redaction test corpus (Phase-0 guardrail).

The vault is append-only: a secret captured once lives forever. redact.py is the
only thing standing between a leaked credential and permanent storage, and it
leans on regexes that are easy to silently break. This corpus is the handle —
two fixed lists run on every change so the patterns can't drift:

  MUST_REDACT    real credentials that have to be caught. A miss here = a key
                 in the vault forever. (false negative — unacceptable)
  MUST_NOT_TOUCH legit text that must pass through verbatim: prose, code,
                 UUIDs, hashes, paths, plain identifiers. A hit here = the
                 scrubber over-reaching. (false positive — annoying, not fatal)

Design notes that this file deliberately encodes:
  - "password" as a bare word is SAFE; only `password = <value>` is masked, and
    even then the KEY survives — only the value dies.
  - shape patterns (sk-, AKIA, ghp_, AIza, JWT, PEM, Bearer, db://) basically
    never false-positive — nothing else looks like them.
  - the ONE generic `keyword = value` pattern is the only real over-reach risk,
    so its known sharp edges get their own xfail-documented cases below.

Run: python -m pytest tests/test_redact_corpus.py -q   (stdlib only, no net)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cairn.redact import redact, scrub


# ── MUST REDACT: (description, raw_text, secret_substring_that_must_vanish) ───
# `secret` is the exact run of characters that may NOT survive in the output.
# Each case is wrapped in surrounding prose by the test so we also prove the
# scrubber finds secrets mid-sentence, not just on a bare line.
MUST_REDACT: list[tuple[str, str, str]] = [
    # ── provider / AI keys ───────────────────────────────────────────────
    ("anthropic key",      "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0", "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0"),
    ("openai key",         "sk-" + "proj1234567890ABCDEFxyz",        "sk-" + "proj1234567890ABCDEFxyz"),
    ("generic sk_ token",  "sk_" + "abcdefghij0123456789KLMN",        "sk_" + "abcdefghij0123456789KLMN"),
    # ── cloud ────────────────────────────────────────────────────────────
    ("aws access key",     "AKIA" + "IOSFODNN7EXAMPLE",               "AKIA" + "IOSFODNN7EXAMPLE"),
    ("aws sts key",        "ASIA" + "IOSFODNN7EXAMPLE",               "ASIA" + "IOSFODNN7EXAMPLE"),
    ("google api key",     "AIza" + "SyD-1234567890abcdefghIJKLMNOPqrstu", "AIza" + "SyD-1234567890abcdefghIJKLMNOPqrstu"),
    ("google oauth",       "ya29." + "a0AeXRPd-1234567890abcdef",      "ya29." + "a0AeXRPd-1234567890abcdef"),
    # ── git hosting ──────────────────────────────────────────────────────
    ("github pat",         "ghp_" + "a" * 36,                         "ghp_" + "a" * 36),
    ("github oauth",       "gho_" + "b" * 36,                         "gho_" + "b" * 36),
    ("github fine pat",    "github_pat_" + "c" * 62,                  "github_pat_" + "c" * 62),
    ("gitlab pat",         "glpat-" + "xyz1234567890ABCDEFG",          "glpat-" + "xyz1234567890ABCDEFG"),
    # ── SaaS tokens ──────────────────────────────────────────────────────
    ("slack bot token",    "xoxb-" + "1234567890-abcdefghijKL",        "xoxb-" + "1234567890-abcdefghijKL"),
    ("stripe live key",    "sk_live_" + "0123456789abcdefABCDEFgh",    "sk_live_" + "0123456789abcdefABCDEFgh"),
    ("stripe test key",    "sk_test_" + "0123456789abcdefABCDEFgh",    "sk_test_" + "0123456789abcdefABCDEFgh"),
    ("shopify token",      "shpat_" + "0123456789abcdef0123456789abcdef", "shpat_" + "0123456789abcdef0123456789abcdef"),
    # ── tokens / headers ─────────────────────────────────────────────────
    ("jwt",                "eyJ0eXAiOiJKV1QiLCJhbGc.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "eyJ0eXAiOiJKV1QiLCJhbGc.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"),
    ("bearer header",      "Bearer abcdefghij1234567890XYZ",          "abcdefghij1234567890XYZ"),
    ("basic auth header",  "Basic dXNlcjpwYXNzd29yZA==",              "dXNlcjpwYXNzd29yZA=="),
    # ── connection strings with inline password ──────────────────────────
    ("postgres url",       "postgres://admin:p4ssw0rd@db.internal:5432/app", "p4ssw0rd"),
    ("mongodb+srv url",    "mongodb+srv://u:s3cr3tPass@cluster0.mongodb.net", "s3cr3tPass"),
    ("redis url",          "redis://default:hunter2pass@cache:6379",  "hunter2pass"),
    # ── generic secret assignments (key survives, value dies) ────────────
    ("api_key assign",     'api_key = "supersecretvalue12345"',        "supersecretvalue12345"),
    ("password colon",     'password: hunter2longenough',              "hunter2longenough"),
    ("client_secret",      "client_secret=abcDEF1234567890ghi",        "abcDEF1234567890ghi"),
    ("access_token",       'access_token = "tok_abcdef123456789"',      "tok_abcdef123456789"),
    # ── generic high-entropy backstop: unknown-vendor tokens (synthetic) ──
    ("unknown-vendor token",  "vkey_Xk39fQpL2mWvZ7bNq4RtY8sHc1dEgJ0aUoI5tBw", "vkey_Xk39fQpL2mWvZ7bNq4RtY8sHc1dEgJ0aUoI5tBw"),
    ("bare high-entropy blob", "R7mK2pX9wQ4vL8nZ5tB3yF6hJ1sD0aUoIeCgN4bV2xM",  "R7mK2pX9wQ4vL8nZ5tB3yF6hJ1sD0aUoIeCgN4bV2xM"),
]

# ── PEM block (multi-line) handled separately so the corpus stays one-line ───
PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA1234567890abcdefghijklmnopqrstuvwxyz\n"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0987654321zyxwvutsrqponml\n"
    "-----END RSA PRIVATE KEY-----"
)


# ── MUST NOT TOUCH: legit text that must pass through byte-for-byte ───────────
MUST_NOT_TOUCH: list[tuple[str, str]] = [
    # ── prose mentioning secret-ish words (the classic false positive) ───
    ("prose: forgot password",    "I forgot my password again this morning."),
    ("prose: about api keys",     "We should rotate the api key policy next quarter."),
    ("prose: the secret to golf", "The secret to a good putt is a steady head."),
    ("prose: bearer of news",     "She was the bearer of good news at the meeting."),
    # ── identifiers, ids, hashes that LOOK random but aren't secrets ──────
    ("uuid",                      "user 3f29c8a1-7b4e-4d2a-9c11-0f8e6d5a4b3c logged in"),
    ("git sha",                   "fixed in commit edb63a7b8b8e4ae1ab9c0d1e2f3a4b5c6d7e8f90"),
    ("md5-looking hash",          "checksum 9e107d9d372bb6826bd81d3542a419d6 verified"),
    ("sha256 hex",                "digest a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"),
    # ── code that name-drops secrets but contains none ───────────────────
    # NOTE: `pwd = get_password()` and `api_key = YOUR_KEY_HERE` are NOT here —
    # the generic assign pattern over-reaches on both. They live in
    # TestKnownSharpEdges as documented xfails, not as clean cases.
    ("env var reference",        'token = os.environ["API_KEY"]'),
    ("dict key only",            'config = {"password": PASSWORD_ENV}'),
    # ── filesystem / urls without creds ──────────────────────────────────
    ("windows path",             r"C:\Users\casey\projects\cairn\redact.py"),
    ("plain postgres url",       "postgres://localhost:5432/cairn"),
    ("https url",                "see https://github.com/anthropics/cairn for docs"),
    # ── short tokens below the length floors (must not trip shape rules) ──
    ("short sk",                 "the sk-8 ticket is closed"),
    ("number assignment",        "timeout = 30 and retries = 5"),
    ("bearer too short",         "Bearer ok"),
    # ── long-but-not-secret runs the entropy backstop must spare (<=2 classes) ──
    ("all-caps constant",        "the MAXIMUMRETRYCOUNTLIMITSETTING flag is off"),
    ("long snake_case ident",    "call very_long_descriptive_function_name_here now"),
]


# ── tests ────────────────────────────────────────────────────────────────────

class TestMustRedact:
    @pytest.mark.parametrize("desc,raw,secret",
                             MUST_REDACT, ids=[c[0] for c in MUST_REDACT])
    def test_secret_caught_midsentence(self, desc, raw, secret):
        # embed in prose so we prove mid-string detection, not just bare lines
        text = f"context before {raw} and context after"
        clean, hits = redact(text)
        assert hits >= 1, f"{desc}: nothing redacted"
        assert secret not in clean, f"{desc}: secret survived → {clean!r}"
        assert "[REDACTED:" in clean, f"{desc}: no redaction label emitted"
        # surrounding prose must be preserved
        assert "context before" in clean and "context after" in clean

    def test_pem_private_key_block_redacted(self):
        text = f"key dump:\n{PEM_BLOCK}\nend of dump"
        clean = scrub(text)
        assert "MIIEowIBAAKCAQEA" not in clean
        assert "[REDACTED:PRIVATE_KEY]" in clean
        assert "end of dump" in clean       # content after the block survives

    def test_secret_assign_keeps_key_drops_value(self):
        # the distinguishing behavior: label the field, kill only the value
        clean, hits = redact('password = hunter2longenough')
        assert hits == 1
        assert "password" in clean              # key name survives
        assert "hunter2longenough" not in clean # value dies

    def test_anthropic_key_not_mislabeled_as_openai(self):
        # sk-ant- is MORE specific than sk- (OPENAI_KEY) and must be ordered
        # first — otherwise every Anthropic key matches sk- and mislabels as
        # OPENAI_KEY, leaving the ANTHROPIC_KEY pattern dead code. (regression
        # guard: this was the bug the import write-gate test surfaced.)
        assert scrub("sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0") == "[REDACTED:ANTHROPIC_KEY]"


class TestMustNotTouch:
    @pytest.mark.parametrize("desc,text",
                             MUST_NOT_TOUCH, ids=[c[0] for c in MUST_NOT_TOUCH])
    def test_legit_text_passes_through(self, desc, text):
        clean, hits = redact(text)
        assert hits == 0, f"{desc}: false positive → {clean!r}"
        assert clean == text, f"{desc}: text mutated → {clean!r}"


class TestKnownSharpEdges:
    """The single generic `keyword = value` pattern is the only over-reach risk.
    These pin its known limits. If a future tightening of the pattern makes one
    of these PASS (no longer over-reaches), flip the xfail to a plain assert —
    that's a guardrail improvement worth locking in, not a regression."""

    @pytest.mark.xfail(reason="generic assign pattern can't tell a literal from "
                              "a function call; over-redacts the call. Documented, "
                              "low-harm (a real call masked, not a leak).",
                       strict=True)
    def test_function_call_value_not_redacted(self):
        # ideal future behavior: a function call is not a literal secret
        clean, hits = redact("pwd = get_password() if user else None")
        assert hits == 0 and clean == "pwd = get_password() if user else None"

    @pytest.mark.xfail(reason="generic assign pattern can't tell a real value "
                              "from an obvious placeholder; over-redacts the "
                              "placeholder. Documented, low-harm (no real leak).",
                       strict=True)
    def test_placeholder_value_not_redacted(self):
        # ideal future behavior: ALL_CAPS placeholders aren't secrets
        clean, hits = redact("api_key = YOUR_KEY_HERE")
        assert hits == 0 and clean == "api_key = YOUR_KEY_HERE"


class TestHighEntropyBackstop:
    """The generic backstop catches long, random-looking tokens from vendors no
    named pattern knows — with no vendor name in the code. It must clear a
    length + charset + entropy bar, so hashes / UUIDs / constants pass through."""

    def test_unknown_token_masked_as_high_entropy(self):
        clean, hits = redact("deploy vkey_Xk39fQpL2mWvZ7bNq4RtY8sHc1dEgJ0aUoI5tBw now")
        assert hits == 1
        assert "[REDACTED:HIGH_ENTROPY]" in clean
        assert "vkey_Xk39fQpL2mWvZ7bNq4RtY8sHc1dEgJ0aUoI5tBw" not in clean

    def test_lowercase_hex_hash_untouched(self):
        h = "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"
        assert scrub(f"digest {h} ok") == f"digest {h} ok"

    def test_named_pattern_still_wins_over_backstop(self):
        assert scrub("ghp_" + "a" * 36) == "[REDACTED:GITHUB_PAT]"


class TestContract:
    """Type/shape contract — redact() must never raise on weird input, because
    it runs on EVERY captured field before write. A crash here drops a capture."""

    def test_none_passes_through(self):
        assert redact(None) == (None, 0)

    def test_nonstr_passes_through(self):
        assert redact(12345) == (12345, 0)
        assert redact(["list"]) == (["list"], 0)

    def test_empty_string(self):
        assert redact("") == ("", 0)

    def test_scrub_returns_text_only(self):
        assert isinstance(scrub("plain text"), str)

    def test_multiple_secrets_one_pass(self):
        text = f'key1 sk-{"a"*20} and key2 AKIA{"B"*16}'
        clean, hits = redact(text)
        assert hits >= 2
        assert "sk-" + "a" * 20 not in clean
        assert "AKIA" + "B" * 16 not in clean

    def test_idempotent_on_clean_text(self):
        once = scrub("the golf leaderboard updated at noon")
        twice = scrub(once)
        assert once == twice == "the golf leaderboard updated at noon"

    def test_redacted_output_has_no_residual_secret(self):
        # re-scrubbing already-redacted text must be a no-op (labels aren't secrets)
        clean = scrub(f"token sk-ant-api03-{'Z'*20} done")
        assert scrub(clean) == clean
