"""YouTube scraping engine.

Public, unauthenticated pages only (search results, channel /about, /videos).
Channel data is parsed from the ``ytInitialData`` JSON blob embedded in the
HTML, with BeautifulSoup used to locate script tags and hyperlinks.

Every public function takes an injectable ``fetcher`` callable
(url -> html string) so the module is fully testable offline. The real
fetcher wraps ``requests`` with browser-like headers, retries, and delays.

Functions never raise on network/parse problems: they log and return
partial results instead, keeping the CLI crash-free.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("yt_lead_finder.scraper")

# --- Types ------------------------------------------------------------------


@dataclass
class Channel:
    """A discovered creator channel plus everything scraped about it."""

    channel_id: str = ""
    name: str = ""
    url: str = ""
    about_text: str = ""
    links: List[str] = field(default_factory=list)
    video_descriptions: List[str] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        parts = [self.name, self.about_text, *self.links, *self.video_descriptions]
        return "\n".join(part for part in parts if part)


# --- Networking ---------------------------------------------------------------

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
)

DEFAULT_TIMEOUT = 15.0
MAX_ATTEMPTS = 3


def polite_fetch(url: str, timeout: float = DEFAULT_TIMEOUT, session: Optional[requests.Session] = None) -> Optional[str]:
    """GET *url* with browser-like headers and simple retry/backoff.

    Returns the HTML body, or None on any failure (never raises).
    """
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    session = session or requests
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response.text
            logger.warning("GET %s -> HTTP %s (attempt %d)", url, response.status_code, attempt)
        except requests.RequestException as exc:
            logger.warning("GET %s failed (attempt %d): %s", url, attempt, exc)
        if attempt < MAX_ATTEMPTS:
            time.sleep(min(2 ** attempt, 8))
    logger.error("Giving up on %s after %d attempts", url, MAX_ATTEMPTS)
    return None


# --- Parsing helpers ----------------------------------------------------------

_YT_INITIAL_DATA = re.compile(r"var ytInitialData\s*=\s*(\{.*?\});", re.DOTALL)


def parse_yt_initial_data(html: str) -> Optional[Dict]:
    """Extract the embedded ytInitialData JSON blob from a YouTube page."""
    match = _YT_INITIAL_DATA.search(html or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("ytInitialData JSON was malformed; skipping page")
        return None


def _content_value(obj, default: str = "") -> str:
    """Read a text field that YouTube renders either as {"content": ...}
    or as a plain string, depending on the page/AB test."""
    if isinstance(obj, Dict):
        value = obj.get("content", default)
        return value if isinstance(value, str) else default
    if isinstance(obj, str):
        return obj
    return default


def _find_channel_renderer(payload: Dict) -> Optional[Dict]:
    """Depth-first search for the first ``lockupViewModel``/``channelRenderer``."""
    wanted = ("lockupViewModel", "channelRenderer")
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, Dict):
            for key in wanted:
                if key in node and isinstance(node[key], Dict):
                    return node[key]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def parse_search_results(html: str, limit: int) -> List[Channel]:
    """Parse channel entries out of a YouTube search-results page."""
    payload = parse_yt_initial_data(html)
    if payload is None:
        return []

    channels: List[Channel] = []
    seen_ids = set()
    stack = [payload]
    while stack and len(channels) < limit:
        node = stack.pop()
        if isinstance(node, Dict):
            if "lockupViewModel" in node:
                channel = _parse_lockup(node["lockupViewModel"])
            elif "channelRenderer" in node:
                channel = _parse_channel_renderer(node["channelRenderer"])
            else:
                channel = None
            # Reverse so LIFO pops yield children in document order.
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            channel = None
            stack.extend(reversed(node))
        else:
            channel = None

        if channel is None:
            continue
        if channel.channel_id and channel.channel_id in seen_ids:
            continue
        seen_ids.add(channel.channel_id)
        channels.append(channel)

    return channels[:limit]


def _parse_lockup(lockup: Dict) -> Channel:
    """Extract a Channel from a ``lockupViewModel`` search-result entry."""
    content = lockup.get("contentImage", {}) or {}
    avatar = content.get("decoratedAvatarViewModel", {}) or {}
    avatar_obj = avatar.get("avatar", {}) or {}
    avatar_model = avatar_obj.get("avatarViewModel", {}) or {}
    channel_id = avatar_model.get("id", "") or ""

    metadata = lockup.get("metadata", {}) or {}
    vm = metadata.get("lockupMetadataViewModel", {}) or {}
    name = _content_value(vm.get("title"))

    url = ""
    if name:
        content_id = vm.get("contentId", "") or ""
        if content_id:
            url = f"https://www.youtube.com/@{content_id}"
        else:
            url = f"https://www.youtube.com/results?search_query={quote_plus(name)}"

    return Channel(channel_id=channel_id, name=name, url=url)


def _parse_channel_renderer(renderer: Dict) -> Channel:
    """Extract a Channel from a legacy ``channelRenderer`` entry."""
    channel_id = renderer.get("channelId") or renderer.get("channel_id") or ""
    title = (renderer.get("title") or {}).get("simpleText", "") or ""
    url = ""
    if channel_id:
        url = f"https://www.youtube.com/channel/{channel_id}"
    return Channel(channel_id=channel_id, name=title, url=url)


def parse_about_links(html: str) -> List[str]:
    """Pull external link URLs out of a channel /about page.

    Primary source is the ytInitialData blob; BeautifulSoup <a> tags are used
    as a fallback so plain-HTML pages still yield their links.
    """
    links: List[str] = []
    seen = set()

    def _add(url: str) -> None:
        if url and url not in seen:
            seen.add(url)
            links.append(url)

    payload = parse_yt_initial_data(html)
    if payload is not None:
        def _visit(node) -> None:
            if isinstance(node, Dict):
                # Canonical shape: aboutChannelViewModel -> links -> channelExternalLinkViewModel
                if "channelExternalLinkViewModel" in node:
                    vm = node["channelExternalLinkViewModel"]
                    title = _content_value(vm.get("title"))
                    link = _content_value(vm.get("link"))
                    _add(link or title)
                for value in node.values():
                    _visit(value)
            elif isinstance(node, list):
                for item in node:
                    _visit(item)

        _visit(payload)

    # BeautifulSoup fallback: ordinary hyperlinks on the page.
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if href.startswith("http") and "youtube.com" not in href:
                _add(href)
    except Exception as exc:  # malformed HTML must never crash the pipeline
        logger.debug("BeautifulSoup link fallback failed: %s", exc)

    return links


def extract_mailto_links(html: str) -> List[str]:
    """Return mailto: targets (without the scheme) found in *html*.

    Link text is often where creators publish obfuscated addresses, so the
    anchor text is returned too when the href carries no address.
    """
    if not html:
        return []
    results: List[str] = []
    seen = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - html.parser rarely raises
        return []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:"):
            target = href[7:].split("?", 1)[0].strip()
        else:
            target = ""
        if not target and "@" in anchor.get_text(strip=True):
            # Anchor text often carries a published (possibly obfuscated) address.
            target = anchor.get_text(strip=True)
        if target and target not in seen:
            seen.add(target)
            results.append(target)
    return results


def parse_about_text(html: str) -> str:
    """Extract the channel description text from a /about page."""
    payload = parse_yt_initial_data(html)
    if payload is None:
        return ""

    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, Dict):
            if "aboutChannelViewModel" in node:
                vm = node["aboutChannelViewModel"]
                return _content_value(vm.get("description"))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return ""


def parse_video_descriptions(html: str, limit: int = 10) -> List[str]:
    """Extract short descriptions from a channel /videos page listing."""
    payload = parse_yt_initial_data(html)
    if payload is None:
        return []

    descriptions: List[str] = []
    stack = [payload]
    while stack and len(descriptions) < limit:
        node = stack.pop()
        if isinstance(node, Dict):
            renderer = node.get("videoRenderer") or node.get("gridVideoRenderer")
            if isinstance(renderer, Dict):
                snippet = renderer.get("detailedMetadataSnippets") or []
                for entry in snippet:
                    if not isinstance(entry, Dict):
                        continue
                    text = _content_value(entry.get("snippetText"))
                    if text:
                        descriptions.append(text)
                        break
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return descriptions[:limit]


# --- High-level pipeline -------------------------------------------------------


def channel_about_url(channel: Channel) -> str:
    """Build the /about URL for a discovered channel."""
    base = channel.url.rstrip("/") or "https://www.youtube.com"
    return f"{base}/about"


def discover_channels(
    query: str,
    max_results: int,
    fetcher: Callable[[str], Optional[str]] = polite_fetch,
    verbose: bool = False,
) -> List[Channel]:
    """Search YouTube for *query* and return up to *max_results* channels."""
    if not query or not query.strip():
        logger.warning("Empty search query; nothing to discover")
        return []
    max_results = max(1, int(max_results))

    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgIQAg%3D%3D"
    if verbose:
        logger.info("Searching YouTube for %r", query)
    html = fetcher(search_url)
    if not html:
        logger.error("Search page could not be fetched; returning no channels")
        return []

    try:
        channels = parse_search_results(html, limit=max_results)
    except Exception as exc:  # last-resort guard: never crash discovery
        logger.error("Search parsing failed (%s); returning no channels", exc)
        return []
    if verbose:
        logger.info("Discovered %d channel(s)", len(channels))
    return channels


def enrich_channel(
    channel: Channel,
    fetcher: Callable[[str], Optional[str]] = polite_fetch,
    video_limit: int = 10,
    verbose: bool = False,
) -> Channel:
    """Fetch /about and /videos pages for *channel* and fill in its text/links.

    Returns a new Channel (pure); the input object is left untouched.
    """
    about_url = channel_about_url(channel)
    if verbose:
        logger.info("Fetching about page: %s", about_url)
    about_html = fetcher(about_url) or ""
    try:
        about_text = parse_about_text(about_html)
        links = parse_about_links(about_html)
        links.extend(extract_mailto_links(about_html))
    except Exception as exc:  # last-resort guard: keep the channel, lose nothing else
        logger.error("About-page parsing failed for %s (%s)", channel.url, exc)
        about_text, links = "", []

    videos_url = channel.url.rstrip("/") + "/videos" if channel.url else ""
    video_descriptions: List[str] = []
    if videos_url:
        if verbose:
            logger.info("Fetching videos page: %s", videos_url)
        videos_html = fetcher(videos_url) or ""
        try:
            video_descriptions = parse_video_descriptions(videos_html, limit=video_limit)
        except Exception as exc:
            logger.error("Videos-page parsing failed for %s (%s)", videos_url, exc)

    # De-duplicate links while keeping order.
    deduped_links = list(dict.fromkeys(links))

    return Channel(
        channel_id=channel.channel_id,
        name=channel.name,
        url=channel.url,
        about_text=about_text,
        links=deduped_links,
        video_descriptions=video_descriptions,
    )


def clean_channel_url(url: str) -> str:
    """Normalize a YouTube channel URL (strip tracking params)."""
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}{parsed.path}"
