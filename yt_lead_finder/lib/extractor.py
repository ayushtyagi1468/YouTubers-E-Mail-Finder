"""Email extraction engine.

Responsibilities:
  * de-obfuscate common anti-spam spellings ("name [at] domain [dot] com")
  * extract email addresses with a strict regex
  * normalize to lowercase, dedupe, and pick the most promising address
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional
from urllib.parse import unquote

# Strict-ish email regex: local part + domain with an alphabetic TLD >= 2 chars.
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Local parts that are almost always prose rather than the start of an
# obfuscated address ("you can reach me at any time", "get in touch at ...").
# They are still allowed when the domain is a known mailbox provider, because
# "hit me up at gmail dot com" really is an email.
STOPWORD_LOCALS = frozenset({
    "a", "all", "any", "anytime", "arrive", "arrives", "at", "available",
    "back", "call", "come", "contact", "find", "first", "good", "here", "hi",
    "him", "home", "it", "last", "least", "live", "look", "looking", "me",
    "once", "online", "out", "reach", "say", "talk", "that", "them", "this",
    "touch", "up", "us", "working", "write", "you",
})

# Domains that are real mailbox providers — enough signal to allow stopword
# local parts ("me at gmail dot com" -> me@gmail.com).
KNOWN_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com", "yandex.com", "zoho.com",
})

# Placeholder domains used in templates and docs, never real contacts.
# (RFC 2606 documentation domains like example.com are allowed through here —
# extraction only gathers candidates; the verifier judges deliverability.)
EXAMPLE_DOMAINS = frozenset({
    "yourdomain.com", "email.com", "test.com", "sender.com",
    "recipient.com", "yoursite.com", "website.com", "mysite.com",
    "yourcompany.com", "company.com",
})

# Asset filenames such as "logo@2x.png" are not contact addresses.
IMAGE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "avif",
})

# "john [at] acme (dot) com" and bracket variants.
_BRACKET_AT = re.compile(r"[\[\(\{]\s*(?:at|@)\s*[\]\)\}]", re.IGNORECASE)
_BRACKET_DOT = re.compile(r"[\[\(\{]\s*(?:dot|\.)\s*[\]\)\}]", re.IGNORECASE)

# Spaced word form: "billy at gmail dot com". The domain side must contain at
# least one dot (or "dot" word) so plain prose like "stuck at home" is ignored.
_WORD_AT = re.compile(
    r"\b(?P<local>[A-Za-z0-9._%+-]+)\s+at\s+"
    r"(?P<domain>[A-Za-z0-9-]+(?:\s*(?:\.|\bdot\b)\s*[A-Za-z0-9-]+)+)",
    re.IGNORECASE,
)

# Leftover "dot" words sitting between word characters: "gmail dot com".
_DOT_WORD = re.compile(
    r"(?<=[A-Za-z0-9])\s*[\[\(\{]?\bdot\b[\]\)\}]?\s*(?=[A-Za-z0-9])",
    re.IGNORECASE,
)


def deobfuscate(text: str) -> str:
    """Collapse anti-spam spellings into plain "user@domain.tld" text."""
    cleaned = unquote(str(text))
    cleaned = re.sub(r"mailto:", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("&#64;", "@").replace("&#46;", ".").replace("%40", "@")
    cleaned = _BRACKET_AT.sub("@", cleaned)
    cleaned = _BRACKET_DOT.sub(".", cleaned)

    def _word_at(match: "re.Match[str]") -> str:
        local = match.group("local")
        domain = re.sub(
            r"\s*(?:\.|\bdot\b)\s*", ".", match.group("domain"), flags=re.IGNORECASE
        )
        if local.lower() in STOPWORD_LOCALS and domain.lower() not in KNOWN_MAIL_DOMAINS:
            return match.group(0)
        return f"{local}@{match.group('domain')}"

    cleaned = _WORD_AT.sub(_word_at, cleaned)
    cleaned = _DOT_WORD.sub(".", cleaned)
    cleaned = re.sub(r"\s*@\s*", "@", cleaned)                       # "user @ domain"
    cleaned = re.sub(r"(?<=[A-Za-z0-9])\s*\.\s*(?=[A-Za-z0-9])", ".", cleaned)
    return cleaned


def is_plausible_email(email: str) -> bool:
    """Cheap junk filter for regex hits that are not real contact addresses."""
    domain = email.rsplit("@", 1)[-1]
    tld = domain.rsplit(".", 1)[-1]
    if tld in IMAGE_EXTENSIONS:
        return False  # asset names like logo@2x.png
    if domain in EXAMPLE_DOMAINS:
        return False  # placeholder addresses from templates
    return True


def extract_emails(text: str) -> List[str]:
    """Extract unique lowercase email candidates from *text* (order kept)."""
    if not text:
        return []
    cleaned = deobfuscate(text)

    emails: List[str] = []
    seen = set()
    for match in EMAIL_REGEX.finditer(cleaned):
        email = match.group(0).strip(".").lower()
        if email in seen or not is_plausible_email(email):
            continue
        seen.add(email)
        emails.append(email)
    return emails


FREE_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "yandex.com", "zoho.com",
})


def pick_primary(emails: Iterable[str]) -> Optional[str]:
    """Choose the most promising address when a page advertises several.

    Custom business domains outrank free mailbox providers; ties break on the
    shortest address, then alphabetically, so the result is deterministic.
    """
    candidates = [email for email in dict.fromkeys(emails) if email]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda email: (
            email.rsplit("@", 1)[-1] in FREE_PROVIDERS,  # False sorts first
            len(email),
            email,
        ),
    )

