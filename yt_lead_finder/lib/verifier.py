"""Multi-stage email verifier.

Stages (in order):
  1. Syntax check      — RFC 5322-flavoured validation (dot-atom local/domain)
  2. Disposable filter — reject known throwaway mailbox domains
  3. MX lookup         — query DNS for the domain's Mail Exchange records

Verification statuses (CSV column `MX Status`):
  VERIFIED        MX records exist; mail exchangers were resolved
  INVALID_DOMAIN  bad syntax, disposable domain, unknown domain (NXDOMAIN),
                  no MX records (incl. RFC 7505 null MX)
  SKIPPED         MX lookup was not requested (--verify-mx absent)
  DNS_ERROR       the DNS lookup itself failed (timeout, no nameservers) —
                  inconclusive, treated as neither valid nor invalid
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover - dnspython is a hard dependency
    dns = None

# --- Status constants -------------------------------------------------------

VERIFIED = "VERIFIED"
INVALID_DOMAIN = "INVALID_DOMAIN"
SKIPPED = "SKIPPED"
DNS_ERROR = "DNS_ERROR"

# --- Stage 1: syntax --------------------------------------------------------

_RFC5322_EMAIL = re.compile(
    r"^(?P<local>[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*)"
    r"@"
    r"(?P<domain>[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)$"
)


def check_syntax(email: str) -> bool:
    """RFC 5322-flavoured syntax validation (length limits included)."""
    if not isinstance(email, str) or not email or len(email) > 254:
        return False
    match = _RFC5322_EMAIL.match(email)
    if not match:
        return False
    return len(match.group("local")) <= 64


def extract_domain(email: str) -> Optional[str]:
    """Return the lowercased domain part of *email*, or None."""
    if not isinstance(email, str) or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    return domain or None


# --- Stage 2: disposable ----------------------------------------------------

DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "tempmail.com", "temp-mail.org", "guerrillamail.com",
    "10minutemail.com", "yopmail.com", "trashmail.com", "throwawaymail.com",
    "getnada.com", "dispostable.com", "sharklasers.com", "maildrop.cc",
    "fakeinbox.com", "mailnesia.com", "tempinbox.com", "spamgourmet.com",
    "mytemp.email", "mohmal.com", "emailondeck.com", "moakt.com",
})


def is_disposable(email: str) -> bool:
    """True when the address uses a known throwaway mailbox domain."""
    domain = extract_domain(email)
    return bool(domain) and domain in DISPOSABLE_DOMAINS


# --- Stage 3: MX lookup -----------------------------------------------------

class MXLookupError(Exception):
    """Raised when the MX lookup cannot confirm mail exchangers.

    ``permanent`` is True for authoritative negatives (unknown domain, no MX
    records) and False for transient lookup failures (timeouts, ...).
    """

    def __init__(self, message: str, permanent: bool = True):
        super().__init__(message)
        self.permanent = permanent


_DEFAULT_RESOLVER = None


def _default_resolver():
    global _DEFAULT_RESOLVER
    if _DEFAULT_RESOLVER is None:
        _DEFAULT_RESOLVER = dns.resolver.Resolver(configure=True)
    return _DEFAULT_RESOLVER


def resolve_mx(domain: str, timeout: float = 5.0, resolver=None) -> List[str]:
    """Return sorted MX host names for *domain* (lowest preference first).

    Raises:
        MXLookupError: unknown domain, no MX records, or lookup failure.
    """
    if dns is None:  # pragma: no cover
        raise MXLookupError("dnspython is not installed")
    if not domain:
        raise MXLookupError("empty domain")

    resolver = resolver or _default_resolver()
    try:
        resolver.lifetime = timeout
    except AttributeError:  # test doubles may not implement it
        pass

    try:
        answer = resolver.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN as exc:
        raise MXLookupError(f"unknown domain: {domain}") from exc
    except dns.resolver.NoAnswer as exc:
        raise MXLookupError(f"no MX records for {domain}") from exc
    except dns.exception.Timeout as exc:
        raise MXLookupError(f"dns timeout for {domain}", permanent=False) from exc
    except dns.exception.DNSException as exc:
        raise MXLookupError(f"dns failure for {domain}: {exc}", permanent=False) from exc
    except Exception as exc:
        raise MXLookupError(f"lookup failed for {domain}: {exc}", permanent=False) from exc

    hosts = sorted({str(record.exchange).rstrip(".").lower() for record in answer})
    hosts = [host for host in hosts if host]  # RFC 7505 null MX is "."
    if not hosts:
        raise MXLookupError(f"no usable MX records for {domain}")
    return hosts


# --- Pipeline ---------------------------------------------------------------

@dataclass
class VerificationResult:
    """Outcome of running the verification pipeline for one address."""

    email: str
    status: str  # VERIFIED | INVALID_DOMAIN | SKIPPED | DNS_ERROR
    reason: str = ""
    mx_hosts: List[str] = field(default_factory=list)


def verify_email(
    email: str,
    verify_mx: bool = True,
    timeout: float = 5.0,
    resolver=None,
) -> VerificationResult:
    """Run syntax -> disposable -> MX checks for a single address."""
    if not check_syntax(email):
        return VerificationResult(email, INVALID_DOMAIN, "syntax check failed")
    if is_disposable(email):
        return VerificationResult(email, INVALID_DOMAIN, "disposable email domain")
    if not verify_mx:
        return VerificationResult(email, SKIPPED, "MX lookup not requested")

    domain = extract_domain(email)
    try:
        hosts = resolve_mx(domain, timeout=timeout, resolver=resolver)
    except MXLookupError as exc:
        status = INVALID_DOMAIN if exc.permanent else DNS_ERROR
        return VerificationResult(email, status, str(exc))
    except Exception as exc:  # unexpected resolver crash — inconclusive
        return VerificationResult(email, DNS_ERROR, str(exc))

    return VerificationResult(email, VERIFIED, "MX records resolved", mx_hosts=hosts)
