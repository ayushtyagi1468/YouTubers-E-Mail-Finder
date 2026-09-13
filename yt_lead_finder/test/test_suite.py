"""Unit tests for yt_lead_finder.

Run with:
    python -m unittest discover -s test
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dns.exception
import dns.resolver
import requests

# Allow "python -m unittest discover -s test" from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.exporter import CSV_COLUMNS, export_report, to_csv, to_json  # noqa: E402
from lib.extractor import (  # noqa: E402
    deobfuscate,
    extract_emails,
    pick_primary,
)
from lib.scraper import (  # noqa: E402
    Channel,
    clean_channel_url,
    discover_channels,
    enrich_channel,
    extract_mailto_links,
    parse_about_links,
    parse_about_text,
    parse_search_results,
    parse_video_descriptions,
    polite_fetch,
)
from lib.verifier import (  # noqa: E402
    DNS_ERROR,
    INVALID_DOMAIN,
    SKIPPED,
    VERIFIED,
    MXLookupError,
    VerificationResult,
    check_syntax,
    is_disposable,
    resolve_mx,
    verify_email,
)
from main import build_arg_parser, run  # noqa: E402


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class DeobfuscationTests(unittest.TestCase):
    def test_bracket_at_and_dot(self):
        text = "Contact me: name [at] domain [dot] com"
        self.assertEqual(deobfuscate(text), "Contact me: name@domain.com")

    def test_paren_at_and_dot(self):
        text = "mail me at name (at) domain (dot) com"
        self.assertEqual(deobfuscate(text), "mail me at name@domain.com")

    def test_spaced_at(self):
        self.assertEqual(deobfuscate("user @ example . com"), "user@example.com")

    def test_spaced_word_at(self):
        self.assertEqual(deobfuscate("billy at gmail dot com"), "billy@gmail.com")

    def test_prose_at_is_not_mangled(self):
        self.assertEqual(deobfuscate("stuck at home"), "stuck at home")

    def test_html_entities(self):
        self.assertEqual(deobfuscate("user&#64;example&#46;com"), "user@example.com")

    def test_mailto_and_percent40(self):
        self.assertEqual(deobfuscate("mailto:user@example.com"), "user@example.com")
        self.assertEqual(deobfuscate("user%40example.com"), "user@example.com")

    def test_passes_plain_emails_through(self):
        self.assertEqual(deobfuscate("user@example.com"), "user@example.com")


class ExtractionTests(unittest.TestCase):
    def test_plain_email_extraction(self):
        found = extract_emails("Ping alice@example.com or bob@example.org")
        self.assertEqual(found, ["alice@example.com", "bob@example.org"])

    def test_mixed_case_normalized_lowercase(self):
        found = extract_emails("E-Mail: John.Doe@Example.COM")
        self.assertEqual(found, ["john.doe@example.com"])

    def test_obfuscated_extraction(self):
        found = extract_emails("Business inquiries: name [at] domain [dot] com")
        self.assertEqual(found, ["name@domain.com"])

    def test_plus_addressing_preserved(self):
        found = extract_emails("hi there+yt@example.com")
        self.assertEqual(found, ["there+yt@example.com"])

    def test_dedupe(self):
        self.assertEqual(extract_emails("a@x.com a@x.com a@x.com"), ["a@x.com"])

    def test_image_asset_not_email(self):
        self.assertEqual(extract_emails('<img src="logo@2x.png">'), [])

    def test_placeholder_domain_rejected(self):
        self.assertEqual(extract_emails("use yourname@yourdomain.com template"), [])

    def test_empty_input(self):
        self.assertEqual(extract_emails(""), [])
        self.assertEqual(extract_emails(None), [])

    def test_pick_primary_prefers_custom_domain(self):
        picked = pick_primary(["hello@gmail.com", "partners@acme.io"])
        self.assertEqual(picked, "partners@acme.io")

    def test_pick_primary_deterministic(self):
        picked = pick_primary(["z@b.com", "a@b.com"])
        self.assertEqual(picked, "a@b.com")

    def test_pick_primary_empty(self):
        self.assertIsNone(pick_primary([]))


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class SyntaxTests(unittest.TestCase):
    def test_valid_addresses(self):
        for email in ("a@b.co", "first.last@sub.domain.org", "x+y@domain.io"):
            self.assertTrue(check_syntax(email))

    def test_invalid_addresses(self):
        for email in ("", "no-at-sign", "a@b", "a..b@x.com", "@x.com",
                      "user@-bad.com", "user@x..com", "a" * 65 + "@x.com"):
            self.assertFalse(check_syntax(email))


class DisposableTests(unittest.TestCase):
    def test_known_disposable_rejected(self):
        self.assertTrue(is_disposable("bob@mailinator.com"))
        self.assertTrue(is_disposable("jane@TempMail.com"))

    def test_normal_domain_passes(self):
        self.assertFalse(is_disposable("jane@gmail.com"))
        self.assertFalse(is_disposable("biz@acme.io"))


def _mx_records(hosts):
    records = []
    for host in hosts:
        record = mock.Mock()
        record.exchange = f"{host}."
        records.append(record)
    return records


def _resolver_returning(records):
    resolver = mock.Mock()
    resolver.resolve = mock.Mock(return_value=records)
    return resolver


class MXMockTests(unittest.TestCase):
    """DNS resolver is mocked — no network access in tests."""

    def test_valid_domain_gets_verified(self):
        resolver = _resolver_returning(_mx_records(["mx1.acme.io"]))
        result = verify_email("biz@acme.io", verify_mx=True, resolver=resolver)
        self.assertEqual(result.status, VERIFIED)
        self.assertEqual(result.mx_hosts, ["mx1.acme.io"])

    def test_unknown_domain_is_invalid(self):
        resolver = mock.Mock()
        resolver.resolve = mock.Mock(side_effect=dns.resolver.NXDOMAIN())
        result = verify_email("biz@nope.com", verify_mx=True, resolver=resolver)
        self.assertEqual(result.status, INVALID_DOMAIN)

    def test_no_mx_is_invalid(self):
        resolver = mock.Mock()
        resolver.resolve = mock.Mock(side_effect=dns.resolver.NoAnswer())
        result = verify_email("biz@acme.io", verify_mx=True, resolver=resolver)
        self.assertEqual(result.status, INVALID_DOMAIN)

    def test_timeout_is_dns_error(self):
        resolver = mock.Mock()
        resolver.resolve = mock.Mock(side_effect=dns.exception.Timeout())
        result = verify_email("biz@acme.io", verify_mx=True, resolver=resolver)
        self.assertEqual(result.status, DNS_ERROR)

    def test_null_mx_is_invalid(self):
        resolver = _resolver_returning(_mx_records(["."]))
        result = verify_email("biz@acme.io", verify_mx=True, resolver=resolver)
        self.assertEqual(result.status, INVALID_DOMAIN)

    def test_skipped_when_verify_mx_false(self):
        result = verify_email("biz@acme.io", verify_mx=False)
        self.assertEqual(result.status, SKIPPED)

    def test_invalid_syntax_short_circuits_before_dns(self):
        resolver = mock.Mock()
        result = verify_email("bad email@", verify_mx=True, resolver=resolver)
        self.assertEqual(result.status, INVALID_DOMAIN)
        resolver.resolve.assert_not_called()

    def test_resolve_mx_sorted_and_dot_stripped(self):
        hosts = resolve_mx("acme.io", resolver=_resolver_returning(
            _mx_records(["b.mx.io", "a.mx.io"])))
        self.assertEqual(hosts, ["a.mx.io", "b.mx.io"])

    def test_mx_lookup_error_permanence_flag(self):
        self.assertTrue(MXLookupError("x").permanent)
        self.assertFalse(MXLookupError("x", permanent=False).permanent)


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class ExporterTests(unittest.TestCase):
    def _lead(self, **overrides):
        lead = {
            "Channel Name": "Acme Reviews",
            "Channel URL": "https://www.youtube.com/@acme",
            "Extracted Email": "biz@acme.io",
            "MX Status": "VERIFIED",
            "Timestamp": "2026-09-12 10:00:00",
        }
        lead.update(overrides)
        return lead

    def test_csv_columns_exact(self):
        output = to_csv([self._lead()])
        header = output.splitlines()[0]
        self.assertEqual(header, ",".join(CSV_COLUMNS))

    def test_csv_roundtrip(self):
        output = to_csv([
            self._lead(),
            self._lead(**{"Extracted Email": "", "MX Status": ""}),
        ])
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Extracted Email"], "biz@acme.io")
        self.assertEqual(rows[1]["Extracted Email"], "")

    def test_csv_escapes_commas(self):
        output = to_csv([self._lead(**{"Channel Name": "Acme, Inc."})])
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]["Channel Name"], "Acme, Inc.")

    def test_missing_fields_default_to_empty(self):
        output = to_csv([{"Channel Name": "X"}])
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]["MX Status"], "")
        self.assertEqual(rows[0]["Extracted Email"], "")

    def test_to_json_is_array(self):
        data = json.loads(to_json([self._lead()]))
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["Channel Name"], "Acme Reviews")

    def test_atomic_write_no_tmp_leftovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "leads.csv"
            export_report([self._lead()], out)
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_atomic_write_creates_missing_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "deeper" / "leads.csv"
            export_report([self._lead()], out)
            self.assertTrue(out.exists())

    def test_overwrite_existing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "leads.csv"
            export_report([self._lead()], out)
            export_report([self._lead(**{"Channel Name": "Second"})], out)
            with open(out, encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Channel Name"], "Second")

    def test_json_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "leads.json"
            export_report([self._lead()], out)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertIsInstance(data, list)
            self.assertEqual(data[0]["Channel Name"], "Acme Reviews")

    def test_unknown_extension_falls_back_to_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "leads.txt"
            export_report([self._lead()], out)
            self.assertTrue(out.read_text(encoding="utf-8").startswith(",".join(CSV_COLUMNS)))


# ---------------------------------------------------------------------------
# Scraper (offline, fixture HTML)
# ---------------------------------------------------------------------------

SEARCH_HTML = (
    "<html><head><title>search</title></head><body>\n"
    "<script>var ytInitialData = {JSON};</script>\n"
    "</body></html>"
)

_LINK_HTML = (
    "<html><body>"
    '<a href="mailto:team@techbits.dev">Email us</a>'
    '<a href="mailto:biz [at] tech [dot] dev">Business</a>'
    '<a href="https://twitter.com/techbits">Twitter</a>'
    "</body></html>"
)

_LOCKUP = {
    "lockupViewModel": {
        "contentImage": {
            "decoratedAvatarViewModel": {
                "avatar": {"avatarViewModel": {"id": "UC1234567890abcdef"}},
            }
        },
        "metadata": {
            "lockupMetadataViewModel": {
                "title": {"content": "Tech Bits"},
                "contentId": "techbits",
            }
        },
    }
}

_VIDEO_RENDERER = {
    "videoRenderer": {
        "detailedMetadataSnippets": [
            {"snippetText": {"content": "Email me at videos@techbits.dev"}}
        ]
    }
}


def _lockup(name, handle, uid):
    """Build a lockupViewModel search-result entry with distinct identity."""
    return {
        "lockupViewModel": {
            "contentImage": {
                "decoratedAvatarViewModel": {
                    "avatar": {"avatarViewModel": {"id": uid}},
                }
            },
            "metadata": {
                "lockupMetadataViewModel": {
                    "title": {"content": name},
                    "contentId": handle,
                }
            },
        }
    }

_ABOUT_PAYLOAD = {
    "aboutChannelViewModel": {
        "description": {"content": "Business: team@techbits.dev"},
        "links": [
            {"channelExternalLinkViewModel": {
                "title": {"content": "Twitter"},
                "link": {"content": "https://twitter.com/techbits"},
            }},
            {"channelExternalLinkViewModel": {
                "title": {"content": "biz [at] techbits [dot] dev"},
                "link": {"content": "https://techbits.dev/contact"},
            }},
        ],
    }
}


def _page(payload):
    return SEARCH_HTML.replace("{JSON}", json.dumps(payload))


class ScraperTests(unittest.TestCase):
    def test_parse_search_results_lockup(self):
        html = _page({"contents": [_LOCKUP]})
        channels = parse_search_results(html, limit=5)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "Tech Bits")
        self.assertEqual(channels[0].url, "https://www.youtube.com/@techbits")
        self.assertEqual(channels[0].channel_id, "UC1234567890abcdef")

    def test_parse_search_results_limit(self):
        channels_data = [_lockup("Tech Bits", "techbits", "UC1"),
                         _lockup("Gadget Guru", "gadgetguru", "UC2"),
                         _lockup("Code Daily", "codedaily", "UC3"),
                         _lockup("Byte Sized", "bytesized", "UC4"),
                         _lockup("Signal Noise", "signalnoise", "UC5")]
        html = _page({"contents": channels_data})
        parsed = parse_search_results(html, limit=3)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0].name, "Tech Bits")
        self.assertEqual(parsed[2].name, "Code Daily")

    def test_parse_search_results_empty(self):
        self.assertEqual(parse_search_results("<html>no data</html>", limit=5), [])

    def test_parse_about_text_and_links(self):
        html = _page(_ABOUT_PAYLOAD)
        self.assertEqual(parse_about_text(html), "Business: team@techbits.dev")
        links = parse_about_links(html)
        self.assertEqual(
            links,
            ["https://twitter.com/techbits", "https://techbits.dev/contact"],
        )

    def test_parse_about_bad_json_is_tolerated(self):
        self.assertEqual(parse_about_text("<script>var ytInitialData = {bad json;"), "")

    def test_parse_about_links_empty_page(self):
        self.assertEqual(parse_about_links(""), [])

    def test_video_descriptions_extracted(self):
        html = _page({"contents": [_VIDEO_RENDERER]})
        descriptions = parse_video_descriptions(html, limit=10)
        self.assertEqual(descriptions, ["Email me at videos@techbits.dev"])

    def test_extract_mailto_links(self):
        links = extract_mailto_links(_LINK_HTML)
        self.assertEqual(
            links,
            ["team@techbits.dev", "biz [at] tech [dot] dev"],
        )

    def test_extract_mailto_links_no_mailto(self):
        self.assertEqual(extract_mailto_links("<p>nothing here</p>"), [])

    def test_enrich_channel_merges_data(self):
        responses = {
            "https://www.youtube.com/@acme/about": _page(_ABOUT_PAYLOAD),
            "https://www.youtube.com/@acme/videos": _page({"contents": [_VIDEO_RENDERER]}),
        }
        enriched = enrich_channel(
            Channel(name="Acme", url="https://www.youtube.com/@acme"),
            fetcher=responses.get,
        )
        self.assertIn("team@techbits.dev", enriched.combined_text)
        self.assertIn("https://twitter.com/techbits", enriched.combined_text)
        self.assertEqual(enriched.video_descriptions,
                         ["Email me at videos@techbits.dev"])

    def test_enrich_channel_offline_tolerated(self):
        enriched = enrich_channel(
            Channel(name="Offline", url="https://www.youtube.com/@x"),
            fetcher=lambda url: None,
        )
        self.assertEqual(enriched.video_descriptions, [])
        self.assertEqual(enriched.about_text, "")

    def test_discover_channels_requires_query(self):
        self.assertEqual(discover_channels("", 5, fetcher=lambda url: None), [])
        self.assertEqual(discover_channels("   ", 5, fetcher=lambda url: None), [])

    def test_discover_channels_offline_returns_empty(self):
        self.assertEqual(discover_channels("python", 5, fetcher=lambda url: None), [])

    def test_discover_channels_parses_results(self):
        html = _page({"contents": [_LOCKUP]})
        channels = discover_channels("tech", 5, fetcher=lambda url: html)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].name, "Tech Bits")

    def test_clean_channel_url(self):
        self.assertEqual(
            clean_channel_url("https://www.youtube.com/@a?utm_source=x"),
            "https://www.youtube.com/@a",
        )


class _FakeResponse:
    def __init__(self, status_code=200, text="<html>ok</html>"):
        self.status_code = status_code
        self.text = text


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        item = self.responses.pop(0) if self.responses else _FakeResponse(status_code=500)
        if isinstance(item, Exception):
            raise item
        return item


class PoliteFetchTests(unittest.TestCase):
    def test_success_first_try(self):
        session = _FakeSession([_FakeResponse()])
        body = polite_fetch("https://x.test", session=session)
        self.assertEqual(body, "<html>ok</html>")

    def test_retries_then_succeeds(self):
        session = _FakeSession([
            _FakeResponse(status_code=500),
            _FakeResponse(status_code=503),
            _FakeResponse(text="<html>recovered</html>"),
        ])
        with mock.patch("time.sleep"):
            body = polite_fetch("https://x.test", session=session)
        self.assertEqual(body, "<html>recovered</html>")
        self.assertEqual(len(session.calls), 3)

    def test_gives_up_after_max_attempts(self):
        session = _FakeSession([_FakeResponse(status_code=500)] * 10)
        with mock.patch("time.sleep"):
            body = polite_fetch("https://x.test", session=session)
        self.assertIsNone(body)
        self.assertEqual(len(session.calls), 3)

    def test_network_exception_retries(self):
        session = _FakeSession([requests.exceptions.ConnectionError("boom"), _FakeResponse()])
        with mock.patch("time.sleep"):
            body = polite_fetch("https://x.test", session=session)
        self.assertEqual(body, "<html>ok</html>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def test_parser_flags(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--query", "Tech", "--max-results", "3",
                                  "--out", "demo.csv", "--verify-mx"])
        self.assertEqual(args.query, "Tech")
        self.assertEqual(args.max_results, 3)
        self.assertEqual(args.out, "demo.csv")
        self.assertTrue(args.verify_mx)

    def test_parser_defaults(self):
        parser = build_arg_parser()
        args = parser.parse_args(["--query", "Tech"])
        self.assertEqual(args.max_results, 10)
        self.assertEqual(args.out, "leads.csv")
        self.assertFalse(args.verify_mx)

    def test_parser_requires_query(self):
        with self.assertRaises(SystemExit):
            build_arg_parser().parse_args([])

    def test_run_exits_1_when_no_channels(self):
        out = os.path.join(tempfile.gettempdir(), "ytl-no-leads.csv")
        with mock.patch("main.discover_channels", return_value=[]):
            code = run(build_arg_parser().parse_args(["--query", "Tech", "--out", out]))
        self.assertEqual(code, 1)

    def test_run_full_pipeline_exits_0(self):
        out = os.path.join(tempfile.gettempdir(), "ytl-cli-full.csv")
        channel = Channel(name="A", url="https://www.youtube.com/@a")
        enriched = Channel(name="A", url=channel.url, about_text="biz@acme.io")
        with mock.patch("main.discover_channels", return_value=[channel]), \
             mock.patch("main.enrich_channel", return_value=enriched), \
             mock.patch("main.verify_email",
                        return_value=VerificationResult("a@x.io", VERIFIED)), \
             mock.patch("main.export_report") as fake_export:
            code = run(build_arg_parser().parse_args(["--query", "Tech", "--out", out]))
        self.assertEqual(code, 0)
        fake_export.assert_called_once()

    def test_run_exit_2_when_no_emails_found(self):
        out = os.path.join(tempfile.gettempdir(), "ytl-cli-empty.csv")
        channel = Channel(name="A", url="https://www.youtube.com/@a")
        with mock.patch("main.discover_channels", return_value=[channel]), \
             mock.patch("main.enrich_channel",
                        return_value=Channel(name="A", url=channel.url, about_text="")), \
             mock.patch("main.export_report"):
            code = run(build_arg_parser().parse_args(["--query", "Tech", "--out", out]))
        self.assertEqual(code, 2)

    def test_run_writes_real_csv(self):
        out = os.path.join(tempfile.gettempdir(), "ytl-cli-real.csv")
        channel = Channel(name="Real", url="https://www.youtube.com/@real",
                          about_text="contact: real@acme.io")
        with mock.patch("main.discover_channels", return_value=[channel]), \
             mock.patch("main.enrich_channel", return_value=channel), \
             mock.patch("main.verify_email",
                        return_value=VerificationResult("real@acme.io", VERIFIED)):
            code = run(build_arg_parser().parse_args(
                ["--query", "Tech", "--out", out]))
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["Channel Name"], "Real")
        self.assertEqual(rows[0]["Extracted Email"], "real@acme.io")
        self.assertEqual(rows[0]["MX Status"], "VERIFIED")
        os.remove(out)


if __name__ == "__main__":
    unittest.main()
