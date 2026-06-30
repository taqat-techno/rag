"""Content-level secret detection and redaction.

File-name exclusion (``ignore.is_secret``) is necessary but insufficient: secrets
get pasted into READMEs, example settings, SQL, and YAML on every stack. This
module scans **chunk text** for high-signal secret patterns and redacts the
**value** while preserving the **key name**, so:

  * the secret value never reaches the embedding vector or the stored payload
    (index-time redaction), and never leaves a tool (serve-time redaction), and
  * "which API key does X use?" still returns the *name* (``geoapify_api_key``)
    with the value masked.

Precision over recall: provider-specific patterns plus a contextual
``<keyword> = <value>`` rule. Findings never echo the secret value (audit-safe).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A "sensitive key name" for the contextual rule. Three precise families so we
# catch real secret refs without flagging common non-secrets:
#   1. a DOTTED config path ending in .key/.secret/.token/.password
#      (e.g. ``disaster.api.geoapify.key``) — dotted form is rarely innocent,
#   2. any identifier ending in secret/token/password/credential
#      (e.g. ``app_secret``, ``jwt_token``, ``db_password``),
#   3. explicit key compounds (``api_key``, ``secret_key``, ``access_key`` ...).
# Underscored ``cache_key`` / ``primary_key`` / ``foreign_key`` are intentionally
# NOT matched (only DOTTED ``.key`` or explicit ``*_key`` secret compounds are).
_SECRET_KEY_NAME = (
    r"(?:"
    r"[A-Za-z][A-Za-z0-9]*(?:[_\-.][A-Za-z0-9]+)*\.(?:key|secret|token|password)"
    r"|[A-Za-z0-9]*(?:secret|token|password|passwd|credential)"
    r"|(?:api|access|private|secret)[_\-]?key|apikey"
    r")"
)


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern
    value_group: int  # 0 = the whole match is the secret; N = redact only group N
    severity: str = "high"        # high (provider token) | medium | low
    contextual: bool = False      # if True, only redact a quoted literal or a
    #                               high-entropy token (group 2 holds the quote)


# A literal secret value looks like a quoted string or a pure hex/base64 token —
# NOT a dotted reference (process.env.X) or an identifier (getApiKey...).
_HEX_RE = re.compile(r"\A[A-Fa-f0-9]{20,}\Z")
_B64_RE = re.compile(r"\A[A-Za-z0-9+/]{24,}={0,2}\Z")


# Provider/format rules run first (specific names); the contextual rule last.
_RULES: list[_Rule] = [
    _Rule("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), 0),
    _Rule("aws_access_key_id", re.compile(r"\bA(?:KIA|SIA|GPA|IDA|ROA|IPA|NPA|NVA)[0-9A-Z]{16}\b"), 0),
    _Rule("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b"), 0),
    _Rule("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"), 0),
    _Rule("stripe_key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"), 0),
    _Rule("private_key_block",
          re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"), 0),
    _Rule("jwt",
          re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), 0),
    # Labeled high-entropy value: a *value-presentation* label (``Default:`` /
    # ``Example:`` / ``API Key:`` with a space) followed by a hex/base64 token,
    # on a line SEPARATE from the dotted key name — e.g. the docs list
    # ``disaster.api.geoapify.key`` then ``- Default: `a7101db…` `` below, or a
    # test logs ``API Key:    a7101db…`` — which the key=value rule below cannot
    # see because key and value are not adjacent. Labels are restricted to ones
    # the ``assigned_secret`` rule does NOT already cover: presentation words and
    # the SPACE form ``api key`` (``apiKey`` / ``api_key`` / ``token:`` /
    # ``secret:`` stay with ``assigned_secret`` so the reported rule is stable).
    # The 24+ hex / 32+ base64 floor keeps benign labels (versions, ``Default:
    # True``, ``length: 6``, 8-char git SHAs) safe.
    _Rule("labeled_secret_value",
          re.compile(r"(?i)(\b(?:default|example|sample|api\s+key)\b"
                     r"\s*[:=]?\s*)(['\"\x60]?)"
                     r"([A-Fa-f0-9]{24,}|[A-Za-z0-9+/]{32,}={0,2})"),
          value_group=3, severity="medium"),
    # Contextual: a sensitive key name assigned a value. Group 1 = key + sep,
    # group 2 = optional opening quote, group 3 = the value. The value charset
    # excludes '.' so dotted env-reads (process.env.X) break at the first dot and
    # fall below the length floor. ``contextual`` => only redact when the value is
    # a quoted literal OR a high-entropy hex/base64 token (see redact()).
    _Rule("assigned_secret",
          re.compile(r"(?i)(" + _SECRET_KEY_NAME + r"\s*[:=]\s*)(['\"\x60]?)([A-Za-z0-9_\-+/=]{8,})"),
          value_group=3, severity="medium", contextual=True),
]


def _mask(rule_name: str) -> str:
    return f"***REDACTED:{rule_name}***"


def redact(text: str) -> "tuple[str, list[dict]]":
    """Redact secret values in ``text``.

    Returns ``(redacted_text, findings)`` where each finding is
    ``{"rule": <name>, "length": <int>}`` — never the secret value itself.
    The key name and surrounding context are preserved.
    """
    if not text:
        return text, []

    findings: list[dict] = []
    out = text

    for rule in _RULES:
        def _sub(m: "re.Match") -> str:
            if rule.value_group:
                value = m.group(rule.value_group)
                if value.startswith("***REDACTED:"):  # already masked
                    return m.group(0)
                if rule.contextual:
                    # Precision gate: redact only a quoted literal or a pure
                    # hex/base64 token. Identifier refs / env-reads are left alone.
                    quoted = m.group(2) in ("'", '"')
                    if not (quoted or _HEX_RE.match(value) or _B64_RE.match(value)):
                        return m.group(0)
                    severity = "medium" if quoted else "low"
                else:
                    severity = rule.severity
                findings.append({"rule": rule.name, "length": len(value), "severity": severity})
                start = m.start(rule.value_group) - m.start(0)
                end = m.end(rule.value_group) - m.start(0)
                whole = m.group(0)
                return whole[:start] + _mask(rule.name) + whole[end:]
            value = m.group(0)
            findings.append({"rule": rule.name, "length": len(value), "severity": rule.severity})
            return _mask(rule.name)

        out = rule.pattern.sub(_sub, out)

    return out, findings


def scan(text: str) -> "list[dict]":
    """Audit-only: report secret findings (rule + length) without redacting.

    Never includes the secret value — safe to surface to a user/agent so they
    can locate and rotate credentials.
    """
    return redact(text)[1]


def redact_text(text: str) -> str:
    """Convenience: return only the redacted text."""
    return redact(text)[0]
