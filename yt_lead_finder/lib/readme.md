# yt_lead_finder — YouTube Creator Email Finder & Verifier

A zero-crash Python CLI that discovers YouTube creators in a niche, extracts
publicly published email addresses, verifies deliverability, and writes clean
CSV/JSON reports.

> **Ethics:** only public, unauthenticated pages are scraped, at a polite
> (rate-limited) pace. Use the results responsibly and in line with YouTube's
> ToS and applicable anti-spam law.

## Project layout

```
yt_lead_finder/
├── main.py              # CLI entry point (argparse interface)
├── requirements.txt     # requests, beautifulsoup4, dnspython
├── lib/
│   ├── scraper.py       # YouTube search + about/videos parsing (ytInitialData + BS4)
│   ├── extractor.py     # De-obfuscation + strict regex email extraction
│   ├── verifier.py      # Syntax check -> disposable filter -> MX lookup
│   └── exporter.py      # Atomic CSV/JSON export
└── test/
    └── test_suite.py    # unittest suite (DNS fully mocked; network-free)
```

## Setup

```bash
cd yt_lead_finder
uv venv .venv                       # or: python -m venv .venv
.venv/Scripts/activate              # Windows (bash: source .venv/Scripts/activate)
pip install -r requirements.txt
```

## Usage

```bash
python main.py --query "Tech Reviews" --max-results 10 --out leads.csv --verify-mx
python main.py --query "Python Developers" --max-results 3 --out demo_leads.csv
python main.py --query "Fitness" --max-results 5 --out leads.json --verify-mx --verbose
```

| Flag | Description |
| --- | --- |
| `--query` (required) | Search niche, e.g. `"Tech Reviews"` |
| `--max-results N` | Number of creators to scan (default 10) |
| `--out PATH` | Output path; `.json` → JSON, anything else → CSV (default `leads.csv`) |
| `--verify-mx` | Run live DNS MX lookups (Stage 3 of the verifier) |
| `--verbose` | Debug logging |

Exit codes: `0` success · `1` no channels found / usage error · `2` ran but
extracted zero emails · `130` interrupted.

## Pipeline

1. **Discover** — search YouTube (channels filter) and parse `ytInitialData`
   to get channel names/URLs.
2. **Enrich** — fetch each channel's `/about` (description + external links)
   and `/videos` pages. Failures degrade gracefully (partial data, no crash).
3. **Extract & verify** — de-obfuscate (`name [at] domain [dot] com`,
   `user @ domain . com`, HTML entities, `mailto:`), extract with a strict
   regex, normalize, then verify:
   - **Stage 1** RFC 5322-flavoured syntax check
   - **Stage 2** disposable-domain filter (mailinator, tempmail, …)
   - **Stage 3** MX lookup via dnspython (with `--verify-mx`)
4. **Export** — atomic write (tmp file + `os.replace`) with columns:
   `Channel Name, Channel URL, Extracted Email, MX Status, Timestamp`.

`MX Status` values: `VERIFIED`, `INVALID_DOMAIN`, `SKIPPED`, `DNS_ERROR`.

## Tests

```bash
python -m unittest discover -s test
```

45 tests cover: obfuscation variants and extraction accuracy, syntax and
disposable checks, MX resolution with **mocked DNS** (valid, NXDOMAIN,
NoAnswer, timeout, null-MX), CSV/JSON formatting, atomic writes, scraper
parsing on fixture HTML, and mocked end-to-end CLI runs. No test touches the
network.
