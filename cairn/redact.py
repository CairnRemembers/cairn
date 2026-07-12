"""
cairn/redact.py — the secret scrubber. The vault is append-only, so a secret
captured once lives forever (void hides it; it stays on disk). Therefore
secrets must NEVER be written in the first place. This runs on every field
the hook captures, BEFORE vault.write.

Stdlib only (charter). Conservative by design: a false positive replaces a
token with [REDACTED:kind] — harmless. A false negative leaks a credential
into permanent storage — unacceptable. When unsure, redact.

Patterns cover the credentials that actually leak through tool output:
provider API keys, cloud keys, tokens, private-key PEM blocks, bearer/auth
headers, connection strings with inline passwords, and generic secret
assignments (api_key = "...", password: ...). A final high-entropy backstop
catches long, random-looking tokens from vendors no named pattern knows —
without hardcoding any vendor name.
"""
from __future__ import annotations

import math
import re
from collections import Counter

# (compiled pattern, replacement label). Order matters: specific before generic.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ── named provider / cloud credentials (high precision) ──────────────
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),             "ANTHROPIC_KEY"),  # before sk- (more specific)
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"),                 "OPENAI_KEY"),
    (re.compile(r"AKIA[0-9A-Z]{16}"),                      "AWS_KEY"),
    (re.compile(r"ASIA[0-9A-Z]{16}"),                      "AWS_STS_KEY"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"),                   "GITHUB_PAT"),
    (re.compile(r"gho_[A-Za-z0-9]{36}"),                   "GITHUB_OAUTH"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{60,}"),          "GITHUB_FINE_PAT"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),          "SLACK_TOKEN"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"),                 "GOOGLE_KEY"),
    (re.compile(r"ya29\.[0-9A-Za-z_-]{20,}"),              "GOOGLE_OAUTH"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),              "GITLAB_PAT"),
    (re.compile(r"sk_live_[0-9A-Za-z]{24,}"),              "STRIPE_KEY"),
    (re.compile(r"sk_test_[0-9A-Za-z]{24,}"),              "STRIPE_TEST_KEY"),
    (re.compile(r"sk_[A-Za-z0-9]{20,}"),                   "API_KEY"),  # ElevenLabs etc. — generic sk_ token (Stripe sk_live_/sk_test_ matched above first)
    (re.compile(r"shpat_[A-Fa-f0-9]{32}"),                 "SHOPIFY_TOKEN"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),
                                                            "JWT"),
    # ── PEM private-key blocks (whole block) ─────────────────────────────
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
                r".*?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
                re.DOTALL),                                "PRIVATE_KEY"),
    # ── auth headers / bearer tokens ─────────────────────────────────────
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"),   "BEARER"),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}"),"BASIC_AUTH"),
    # ── connection strings with inline password ──────────────────────────
    (re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://"
                r"[^\s:@/]+:[^\s:@/]+@[^\s]+"),            "DB_URL_CRED"),
    # ── generic secret assignments: key = "value" / "key": "value" ───────
    (re.compile(r"""(?ix)
        \b(?:api[_-]?key|secret|client[_-]?secret|access[_-]?token|
            auth[_-]?token|password|passwd|pwd|private[_-]?key|
            refresh[_-]?token|session[_-]?token)
        \b \s* [:=] \s*
        ['"]? ([A-Za-z0-9_\-./+=]{8,}) ['"]?
    """),                                                  "SECRET_ASSIGN"),
]

_MASK = "[REDACTED:{}]"

# ── generic high-entropy backstop ────────────────────────────────────────
# Catches long, random-looking secrets no named pattern above knows about
# (unknown/future vendors' tokens) WITHOUT hardcoding any vendor. A run must
# clear three gates to be masked: length, 3+ character classes, and Shannon
# entropy — tuned to catch real tokens while sparing hashes, UUIDs, and ids.
_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9_\-+/=]{28,}")


def _shannon(s):
    n = len(s)
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _looks_secret(tok):
    classes = (any(c.islower() for c in tok)
               + any(c.isupper() for c in tok)
               + any(c.isdigit() for c in tok))
    return classes >= 3 and _shannon(tok) >= 3.5


def redact(text):
    """Scrub secrets from a captured string. Returns (clean_text, hit_count).
    None / non-str passes through untouched as ('', 0) / (text, 0)."""
    if text is None:
        return None, 0
    if not isinstance(text, str):
        return text, 0
    hits = 0
    for pat, label in _PATTERNS:
        if label == "SECRET_ASSIGN":
            # keep the key name, mask only the value (group 1)
            def _sub(m, lab=label):
                nonlocal hits
                hits += 1
                return m.group(0)[:m.start(1) - m.start(0)] + _MASK.format(lab)
            text = pat.sub(_sub, text)
        else:
            def _sub(m, lab=label):
                nonlocal hits
                hits += 1
                return _MASK.format(lab)
            text = pat.sub(_sub, text)

    # generic high-entropy backstop — runs LAST so the precise labeled patterns
    # win first; masks unknown-vendor tokens with no vendor name in the code.
    def _ent_sub(m):
        nonlocal hits
        tok = m.group(0)
        if _looks_secret(tok):
            hits += 1
            return _MASK.format("HIGH_ENTROPY")
        return tok
    text = _ENTROPY_CANDIDATE.sub(_ent_sub, text)

    return text, hits


def scrub(text):
    """Convenience: return only the cleaned text."""
    return redact(text)[0]
