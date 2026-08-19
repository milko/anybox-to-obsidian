#!/usr/bin/env python3
"""
anybox_to_obsidian_cookies.py
=============================

Convert an AnyBox JSON export into Obsidian-ready Markdown notes.

Main features
-------------
	- Parallel HTTP fetching (configurable workers)
	- Cookie support for paywalled sites (e.g. Medium)
	- Multiple extraction backends:
		1. trafilatura (precision mode)
		2. trafilatura (recall mode)
		3. readability-lxml
		4. BeautifulSoup crude fallback renderer
	- Preserves AnyBox folder structure under OUTPUT_ROOT
	- Writes Markdown files with Obsidian-compatible frontmatter
	- Creates a full CSV log of every processed item
	- Creates a separate rejection CSV for fallback/failed items
	- Each progress line is printed on a new line so you can scroll
	  back and compare rejection counts across chunks
	- Supports chunked imports via --start and --limit

Frontmatter behavior
--------------------
	Tags are intentionally omitted from generated notes.
	An LLM will be used to assign tags after import.

Rejection logic
---------------
	Items are marked as Fallback (and written to the rejection CSV) when:
	  - No URL is present
	  - The HTTP response is not 200
	  - The source is a direct PDF (pdf_source_requires_pdf_extraction)
	  - Medium paywall preview is detected (medium_paywalled_preview)
	  - Only an academic abstract/summary was extracted (summary_only_extraction)
	  - No content could be extracted at all (no_content)
	  - An exception occurred during fetching or extraction

Dependencies
------------
	pip install requests beautifulsoup4 trafilatura readability-lxml

Usage
-----
	python anybox_to_obsidian_cookies.py
	python anybox_to_obsidian_cookies.py --start 0 --limit 100 --workers 4
	python anybox_to_obsidian_cookies.py --start 100 --limit 100 --skip-existing
"""

import os
import json
import re
import csv
import argparse
import threading
import requests

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from http.cookiejar import MozillaCookieJar

from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ──────────────────────────────────────────────────────────────────────────────
# Optional dependencies
# The script degrades gracefully if these are not installed,
# but extraction quality will be lower.
# ──────────────────────────────────────────────────────────────────────────────

try:
	import trafilatura
	HAS_TRAFILATURA = True
except ImportError:
	HAS_TRAFILATURA = False
	print("⚠️  trafilatura not installed — pip install trafilatura")

try:
	from readability import Document
	HAS_READABILITY = True
except ImportError:
	HAS_READABILITY = False
	print("⚠️  readability-lxml not installed — pip install readability-lxml")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# Edit these defaults. Most can also be overridden via CLI arguments.
# ══════════════════════════════════════════════════════════════════════════════

# Path to your AnyBox JSON export file
INPUT_JSON = "./data/AnyBoxExport.json"

# Root folder where Markdown notes will be written
OUTPUT_ROOT = "./staging"

# Path to your cookie file (Netscape .txt or JSON export)
# If the file does not exist the script runs without cookies.
COOKIE_FILE = "./medium.com_cookies.txt"

# Number of parallel download workers
# 4 is safe; increase to 8 if you see few errors
MAX_WORKERS = 4

# HTTP request timeout in seconds
REQUEST_TIMEOUT = 20

# Browser-like headers to reduce bot detection
BASE_HEADERS = {
	"User-Agent": (
		"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
		"AppleWebKit/537.36 (KHTML, like Gecko) "
		"Chrome/124.0.0.0 Safari/537.36"
	),
	"Accept-Language": "en-US,en;q=0.9,it;q=0.8",
	"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
	"Cache-Control": "no-cache",
	"Pragma": "no-cache",
}

# HTML tags whose content is always discarded during crude extraction
IGNORE_TAGS = {
	"script", "style", "noscript", "nav", "footer", "header", "aside",
	"form", "button", "input", "select", "option", "textarea",
	"svg", "canvas",
}

# Academic domains where summary-only extraction is likely
ACADEMIC_DOMAINS = (
	"mdpi.com",
	"sciencedirect.com",
	"springer.com",
	"nature.com",
	"wiley.com",
	"tandfonline.com",
	"frontiersin.org",
	"researchgate.net",
	"doi.org",
)


# ══════════════════════════════════════════════════════════════════════════════
# THREAD-LOCAL GLOBALS
# Each worker thread gets its own requests.Session so sessions are not shared.
# ══════════════════════════════════════════════════════════════════════════════

THREAD_LOCAL = threading.local()   # per-thread storage
LOADED_COOKIE_JAR = None           # populated once at startup if cookie file exists
EXTRACT_LOCK = threading.Lock()    # serialises trafilatura/readability calls
PRINT_LOCK = threading.Lock()      # prevents garbled terminal output


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — HTTP SESSION AND COOKIE LOADING
# ══════════════════════════════════════════════════════════════════════════════

def build_session():
	"""
	Create a requests.Session with:
	  - browser-like headers
	  - automatic retries on server errors and rate-limit responses
	"""
	s = requests.Session()
	retry = Retry(
		total=2,
		backoff_factor=1,
		status_forcelist=[429, 500, 502, 503, 504],
	)
	s.mount("https://", HTTPAdapter(max_retries=retry))
	s.mount("http://", HTTPAdapter(max_retries=retry))
	s.headers.update(BASE_HEADERS)
	return s


def get_session():
	"""
	Return the requests.Session for the current thread.
	Creates one on first call and injects cookies if available.
	"""
	global LOADED_COOKIE_JAR

	session = getattr(THREAD_LOCAL, "session", None)
	if session is None:
		session = build_session()
		if LOADED_COOKIE_JAR is not None:
			session.cookies.update(LOADED_COOKIE_JAR)
		THREAD_LOCAL.session = session
	return session


def load_cookie_jar(cookie_path):
	"""
	Load cookies from a file into a RequestsCookieJar.

	Supported formats:
	  - Netscape / Mozilla cookies.txt  (extension .txt or .cookies)
	  - JSON cookie export              (extension .json)

	JSON format can be either:
	  - A list of cookie objects
	  - A dict with a "cookies" key containing a list

	Each cookie object must have at least "name" and "value".
	Optional keys: "domain", "path", "secure".
	"""
	if not os.path.exists(cookie_path):
		raise FileNotFoundError(f"Cookie file not found: {cookie_path}")

	jar = requests.cookies.RequestsCookieJar()
	lower = cookie_path.lower()

	# ── Netscape / Mozilla format ──────────────────────────────────────────
	if lower.endswith(".txt") or lower.endswith(".cookies"):
		moz = MozillaCookieJar(cookie_path)
		moz.load(ignore_discard=True, ignore_expires=True)
		for cookie in moz:
			jar.set(
				cookie.name,
				cookie.value,
				domain=cookie.domain,
				path=cookie.path,
				secure=cookie.secure,
			)
		return jar

	# ── JSON format ────────────────────────────────────────────────────────
	if lower.endswith(".json"):
		with open(cookie_path, "r", encoding="utf-8") as f:
			data = json.load(f)

		# Normalise to a flat list
		if isinstance(data, dict):
			if "cookies" in data and isinstance(data["cookies"], list):
				data = data["cookies"]
			else:
				data = [data]

		if not isinstance(data, list):
			raise ValueError("Unsupported JSON cookie format")

		for item in data:
			if not isinstance(item, dict):
				continue
			name = item.get("name")
			value = item.get("value")
			if not name or value is None:
				continue
			jar.set(
				name,
				value,
				domain=item.get("domain", ""),
				path=item.get("path", "/"),
				secure=bool(item.get("secure", False)),
			)
		return jar

	raise ValueError("Unsupported cookie file format. Use .txt/.cookies or .json")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SMALL UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def yaml_quote(value):
	"""
	Wrap a value in double quotes for safe YAML frontmatter output.
	Escapes backslashes, double quotes, and newlines.
	"""
	if value is None:
		value = ""
	value = str(value)
	value = value.replace("\\", "\\\\")
	value = value.replace('"', '\\"')
	value = value.replace("\n", " ")
	return f'"{value}"'


def slugify(text):
	"""
	Convert a title into a filesystem-safe filename.
	Preserves original case (no .lower()).
	Strips characters that are illegal on macOS / Windows.
	Truncates to 200 characters.
	"""
	if not text:
		return "untitled"
	s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()
	s = re.sub(r"\s+", " ", s)
	return s[:200]


def parse_date(raw):
	"""
	Convert an ISO 8601 date string to YYYY-MM-DD.
	Falls back to today's date if parsing fails.
	"""
	try:
		dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
		return dt.strftime("%Y-%m-%d")
	except Exception:
		return datetime.now().strftime("%Y-%m-%d")


def sanitize_html(html):
	"""
	Strip null bytes and other control characters that can break
	HTML parsers or cause ValueError in BeautifulSoup.
	"""
	html = html.replace("\x00", "")
	html = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", html)
	return html


def clean_path_part(text):
	"""
	Sanitize a single folder-name component.
	Removes filesystem-illegal characters and falls back to "Imported".
	"""
	text = str(text).strip()
	text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
	return text or "Imported"


def normalize_folder(folder_value):
	"""
	AnyBox stores folder as one of:
	  - None / missing
	  - a plain string  e.g. "Tutorials"
	  - a list          e.g. ["Software", "Tutorials"]

	This function normalises all three cases into a valid relative path
	that can be passed to os.path.join().

	Examples:
	  None                     -> "Imported"
	  "Tutorials"              -> "Tutorials"
	  ["Software","Tutorials"] -> "Software/Tutorials"
	"""
	if not folder_value:
		return "Imported"

	if isinstance(folder_value, str):
		return clean_path_part(folder_value)

	if isinstance(folder_value, list):
		parts = [clean_path_part(p) for p in folder_value if str(p).strip()]
		return os.path.join(*parts) if parts else "Imported"

	# Unexpected type — convert to string and sanitize
	return clean_path_part(folder_value)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — URL AND DOMAIN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_medium_url(url):
	"""
	Return True if the URL belongs to medium.com or a Medium custom domain.
	"""
	try:
		host = urlparse(url).netloc.lower()
		return host.endswith("medium.com")
	except Exception:
		return False


def is_probable_pdf_url(url):
	"""
	Return True if the URL path appears to point directly to a PDF file.
	Used as a secondary check alongside the HTTP Content-Type header.
	"""
	try:
		return urlparse(url).path.lower().endswith(".pdf")
	except Exception:
		return False


def normalize_author(author):
	"""
	Clean up author strings.

	Medium (when accessed without cookies) sometimes returns the author's
	profile URL instead of their display name. This function tries to
	extract a readable name from the URL, or returns an empty string
	if nothing useful can be found.
	"""
	if not author:
		return ""

	author = str(author).strip()

	# If it looks like a URL, try to extract a username
	if author.startswith("http://") or author.startswith("https://"):
		parsed = urlparse(author)
		host = parsed.netloc
		path = parsed.path

		# Medium subdomain: username.medium.com
		if host.endswith(".medium.com"):
			username = host.split(".medium.com")[0]
			if username and username not in ("www", ""):
				return username

		# Medium path: /@username
		path_match = re.match(r"^/@([^/]+)", path)
		if path_match:
			return path_match.group(1)

		# Last path segment (skip hex IDs)
		parts = [p for p in path.split("/") if p]
		if parts:
			candidate = parts[-1]
			if not re.fullmatch(r"[a-f0-9]{8,}", candidate):
				return candidate

		return ""

	# Plain text cleanup
	author = re.sub(r"\S+@\S+\.\S+", "", author)   # remove email addresses
	author = author.strip(".,;:-–—|")
	author = re.sub(r"\s+", " ", author).strip()
	return author


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — METADATA EXTRACTION FROM HTML
# ══════════════════════════════════════════════════════════════════════════════

def extract_lead_image(soup, base_url):
	"""
	Find the best representative image for the article.

	Priority order:
	  1. og:image meta tag
	  2. twitter:image meta tag
	  3. First <img> inside article / main / known content containers
	  4. First <img> anywhere on the page

	Returns an absolute URL string, or empty string if none found.
	"""
	og = soup.find("meta", property="og:image")
	if og and og.get("content"):
		return urljoin(base_url, og["content"])

	tw = soup.find("meta", attrs={"name": "twitter:image"})
	if tw and tw.get("content"):
		return urljoin(base_url, tw["content"])

	for selector in ["article", "main", ".post-content", ".entry-content"]:
		container = soup.select_one(selector)
		if container:
			img = container.find("img", src=True)
			if img:
				return urljoin(base_url, img["src"])

	img = soup.find("img", src=True)
	return urljoin(base_url, img["src"]) if img else ""


def extract_author_published(soup):
	"""
	Extract author name and publication date from common meta tags.

	Returns: (author_str, published_str)
	  - author_str   : plain name or empty string
	  - published_str: YYYY-MM-DD or empty string
	"""
	author = ""
	published = ""

	# Try common author meta tags in priority order
	for attr, val in [
		("property", "article:author"),
		("name", "author"),
		("property", "og:article:author"),
	]:
		tag = soup.find("meta", attrs={attr: val})
		if tag and tag.get("content"):
			author = tag["content"].strip()
			break

	# Try common publication date meta tags in priority order
	for attr, val in [
		("property", "article:published_time"),
		("name", "pubdate"),
		("itemprop", "datePublished"),
	]:
		tag = soup.find("meta", attrs={attr: val})
		if tag and tag.get("content"):
			raw = tag["content"].strip()
			try:
				dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
				published = dt.strftime("%Y-%m-%d")
			except Exception:
				published = raw[:10]   # best-effort: take first 10 chars
			break

	return normalize_author(author), published


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — CONTENT QUALITY CHECKS
# These functions detect cases where extraction "succeeded" technically
# but the result is not a full article worth keeping.
# ══════════════════════════════════════════════════════════════════════════════

def looks_like_medium_preview(url, content):
	"""
	Detect whether extracted content is just a Medium paywall teaser.

	Without valid session cookies, Medium returns a short preview followed
	by a sign-up prompt. This function catches that case so the item is
	logged as "medium_paywalled_preview" (Fallback) rather than "Success".

	Heuristic: content contains a known paywall marker AND is very short
	(fewer than 700 words).
	"""
	if not is_medium_url(url) or not content:
		return False

	text = re.sub(r"\s+", " ", content[:12000]).lower()
	markers = [
		"continue reading with membership",
		"sign up to read this story",
		"member-only story",
	]

	return any(m in text for m in markers) and len(re.findall(r"\w+", text)) < 700


def looks_like_summary_only(content, url=""):
	"""
	Heuristic detector for academic abstract/summary-only extractions.

	Some academic publisher pages (MDPI, Springer, ScienceDirect, etc.)
	show only the abstract and metadata in their HTML, with the full paper
	body locked behind a PDF download or a paywall. When the extractor
	finds "some content" on these pages it marks the item as Success, but
	the note only contains the abstract.

	This function catches that case by checking:
	  1. The URL belongs to a known academic domain, OR the text contains
	     typical academic signals (abstract, keywords, DOI, citation).
	  2. The extracted text is short (< 1200 words).
	  3. The text contains the word "abstract".
	  4. Fewer than 2 body-section markers are present (introduction,
	     methods, results, discussion, conclusion, etc.).

	The check is intentionally conservative to avoid false positives on
	legitimate short articles.
	"""
	if not content:
		return False

	text = re.sub(r"\s+", " ", content).lower()
	word_count = len(re.findall(r"\w+", text))
	host = urlparse(url).netloc.lower() if url else ""

	# Signal 1: academic domain or academic vocabulary
	academic_signal = (
		any(domain in host for domain in ACADEMIC_DOMAINS)
		or " abstract " in f" {text} "
		or " keywords " in f" {text} "
		or " doi " in f" {text} "
		or " citation " in f" {text} "
	)

	if not academic_signal:
		return False

	# Signal 2: body section markers that would appear in a full paper
	body_markers = [
		"introduction",
		"materials and methods",
		"methods",
		"methodology",
		"results",
		"discussion",
		"conclusion",
	]
	body_hits = sum(1 for marker in body_markers if marker in text)

	# Reject if: short AND has abstract signal AND missing body sections
	return (
		word_count < 1200
		and "abstract" in text
		and body_hits < 2
	)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — CRUDE HTML → MARKDOWN FALLBACK RENDERER
# This is used when trafilatura and readability both fail or are not installed.
# It is intentionally simple and handles only the most common HTML structures.
# ══════════════════════════════════════════════════════════════════════════════

def clean_inline_text(text):
	"""Normalise whitespace in a plain-text string."""
	if not text:
		return ""
	text = text.replace("\xa0", " ")                    # non-breaking space
	text = re.sub(r"[ \t\r\f\v]+", " ", text)
	return text.strip()


def render_inlines(node):
	"""
	Render a single HTML node as inline Markdown.
	Handles: text nodes, <br>, <code>, <a>, <strong>, <b>, <em>, <i>.
	"""
	if isinstance(node, NavigableString):
		return str(node)

	if not isinstance(node, Tag):
		return ""

	name = node.name.lower()

	if name in IGNORE_TAGS:
		return ""

	if name == "br":
		return "\n"

	if name == "code":
		return f"`{clean_inline_text(node.get_text())}`"

	if name == "a":
		href = node.get("href", "").strip()
		txt = clean_inline_text(node.get_text())
		return f"[{txt}]({href})" if href else txt

	if name in {"strong", "b"}:
		return f"**{render_inlines_recursive(node)}**"

	if name in {"em", "i"}:
		return f"*{render_inlines_recursive(node)}*"

	return render_inlines_recursive(node)


def render_inlines_recursive(node):
	"""Render all inline children of a node and concatenate the results."""
	return "".join(render_inlines(c) for c in node.children)


def render_block(node):
	"""
	Render a block-level HTML node as Markdown.
	Handles: headings, paragraphs, pre/code, lists, blockquotes,
	and generic containers (div, section, article, etc.).
	"""
	if not node or not isinstance(node, Tag):
		return ""

	name = node.name.lower()

	if name in IGNORE_TAGS:
		return ""

	if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
		level = int(name[1])
		return f"{'#' * level} {clean_inline_text(node.get_text())}\n\n"

	if name == "p":
		return f"{clean_inline_text(render_inlines_recursive(node))}\n\n"

	if name == "pre":
		return f"```\n{node.get_text().strip()}\n```\n\n"

	if name in {"ul", "ol"}:
		items = []
		for li in node.find_all("li", recursive=False):
			items.append(f"- {clean_inline_text(li.get_text())}")
		return "\n".join(items) + "\n\n"

	if name == "blockquote":
		lines = clean_inline_text(node.get_text()).splitlines()
		return "\n".join(f"> {line}" for line in lines if line.strip()) + "\n\n"

	# Generic container: recurse into children
	return "".join(
		render_block(c) if isinstance(c, Tag) else ""
		for c in node.children
	)


def render_container(container):
	"""Render all top-level children of a container node."""
	return "".join(
		render_block(c) if isinstance(c, Tag) else ""
		for c in container.children
	)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — FETCH AND EXTRACT ARTICLE CONTENT
# ══════════════════════════════════════════════════════════════════════════════

def fetch_and_extract(url):
	"""
	Download a page and attempt to extract its main article content.

	Extraction strategy (tried in order, longest result wins):
	  1. trafilatura — precision mode  (fewest false positives)
	  2. trafilatura — recall mode     (more content, may include noise)
	  3. readability-lxml              (Mozilla Readability port)
	  4. BeautifulSoup crude renderer  (last resort)

	Rejection logic (in order):
	  - HTTP status != 200            -> "HTTP <code>"
	  - Content-Type is PDF           -> "pdf_source_requires_pdf_extraction"
	  - URL path ends in .pdf         -> "pdf_source_requires_pdf_extraction"
	  - No content extracted          -> "no_content"
	  - Medium paywall preview        -> "medium_paywalled_preview"
	  - Academic summary/abstract only-> "summary_only_extraction"

	Returns:
	  (content, status, meta)
	  - content : Markdown string, or None if extraction failed/rejected
	  - status  : short label describing what happened
	  - meta    : dict with keys author, published, lead_image
	"""
	session = get_session()

	try:
		resp = session.get(url, timeout=REQUEST_TIMEOUT)

		if resp.status_code != 200:
			return None, f"HTTP {resp.status_code}", {}

		content_type = resp.headers.get("Content-Type", "").lower()
		final_url = resp.url

		# Reject direct PDF sources — the script cannot extract PDF body text.
		# These are logged as Fallback so you can handle them manually.
		if (
			"application/pdf" in content_type
			or is_probable_pdf_url(url)
			or is_probable_pdf_url(final_url)
		):
			return None, "pdf_source_requires_pdf_extraction", {}

		html = sanitize_html(resp.text)
		soup = BeautifulSoup(html, "html.parser")

		# Extract metadata from the page regardless of content extraction result
		lead_image = extract_lead_image(soup, final_url)
		author, published = extract_author_published(soup)

		meta = {
			"author": author,
			"published": published,
			"lead_image": lead_image,
		}

		candidates = []

		# ── 1. trafilatura precision ───────────────────────────────────────
		if HAS_TRAFILATURA:
			with EXTRACT_LOCK:
				md = trafilatura.extract(
					html,
					output_format="markdown",
					include_tables=True,
					favor_precision=True,
				)
			if md:
				candidates.append(("trafilatura:precision", md))

			# ── 2. trafilatura recall ──────────────────────────────────────
			with EXTRACT_LOCK:
				md = trafilatura.extract(
					html,
					output_format="markdown",
					include_tables=True,
					favor_precision=False,
				)
			if md:
				candidates.append(("trafilatura:recall", md))

		# ── 3. readability-lxml ────────────────────────────────────────────
		if HAS_READABILITY:
			with EXTRACT_LOCK:
				doc = Document(html)
				summary_html = doc.summary()
			md = render_container(BeautifulSoup(summary_html, "html.parser"))
			if md:
				candidates.append(("readability", md))

		# ── 4. BeautifulSoup crude fallback ───────────────────────────────
		main = (
			soup.find("article")
			or soup.find("main")
			or soup.find(id=re.compile(r"content|article|post", re.I))
		)
		if main:
			md = render_container(main)
			if md:
				candidates.append(("beautifulsoup:main_custom_md", md))

		if not candidates:
			return None, "no_content", meta

		# Pick the candidate with the most content
		candidates.sort(key=lambda x: len(x[1]), reverse=True)
		best_status, best_md = candidates[0]

		# Reject Medium paywall previews
		if looks_like_medium_preview(final_url, best_md):
			return None, "medium_paywalled_preview", meta

		# Reject academic abstract/summary-only extractions
		if looks_like_summary_only(best_md, final_url):
			return None, "summary_only_extraction", meta

		return best_md, best_status, meta

	except Exception as e:
		return None, f"error:{str(e)[:80]}", {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — FRONTMATTER AND FILE WRITING
# ══════════════════════════════════════════════════════════════════════════════

def build_frontmatter(item, created_date, author, published, lead_image):
	"""
	Build the YAML frontmatter block for an Obsidian note.

	Fields populated:
	  - title       : from AnyBox item
	  - source      : URL from AnyBox item
	  - author      : extracted from the webpage (empty if not found)
	  - published   : extracted from the webpage (empty if not found)
	  - created     : from AnyBox dateAdded, formatted as YYYY-MM-DD
	  - description : from AnyBox item
	  - lead_image  : best image found on the page (omitted if none)

	Tags are intentionally omitted — an LLM will assign them after import.
	All string values are double-quoted for safe YAML output.
	"""
	title = item.get("title", "Untitled")
	url = item.get("url", "")
	desc = item.get("description", "")

	lines = ["---"]
	lines.append(f"title: {yaml_quote(title)}")
	lines.append(f"source: {yaml_quote(url)}")
	lines.append(f"author: {yaml_quote(author)}")
	lines.append(f"published: {yaml_quote(published)}")
	lines.append(f"created: {created_date}")
	lines.append(f"description: {yaml_quote(desc)}")

	if lead_image:
		lines.append(f"lead_image: {yaml_quote(lead_image)}")

	lines.append("---")

	return "\n".join(lines) + "\n\n"


def process_item(item, output_root, skip_existing):
	"""
	Process one AnyBox item end-to-end:
	  1. Resolve output path from folder + title
	  2. Optionally skip if file already exists
	  3. Fetch and extract article content
	  4. Write Markdown note (or stub note on failure)
	  5. Return a result tuple for CSV logging

	Returns:
	  (url, result, details, filepath)

	  - url      : original URL
	  - result   : "Success" | "Fallback" | "Skipped" | "Error"
	  - details  : extraction method or error description
	  - filepath : path of the written file
	"""
	url = item.get("url", "")
	title = item.get("title", "Untitled")
	folder = normalize_folder(item.get("folder"))
	created = parse_date(item.get("dateAdded", ""))

	filename = f"{slugify(title)}.md"
	target_dir = os.path.join(output_root, folder)
	os.makedirs(target_dir, exist_ok=True)
	filepath = os.path.join(target_dir, filename)

	# No URL — write nothing, log as Fallback
	if not url:
		return (url, "Fallback", "no_url", filepath)

	# Skip if file already exists and --skip-existing was passed
	if skip_existing and os.path.exists(filepath):
		return (url, "Skipped", "file_exists", filepath)

	# Fetch and extract
	content, status, meta = fetch_and_extract(url)

	fm = build_frontmatter(
		item,
		created,
		meta.get("author", ""),
		meta.get("published", ""),
		meta.get("lead_image", ""),
	)

	if content:
		# Full article extracted — write complete note
		with open(filepath, "w", encoding="utf-8") as f:
			f.write(fm + content)
		return (url, "Success", status, filepath)

	# Extraction failed — write a stub note with a link to the original
	stub = (
		fm
		+ f"> Full article could not be extracted. Status: `{status}`\n\n"
		+ f"[Open original article]({url})\n"
	)
	with open(filepath, "w", encoding="utf-8") as f:
		f.write(stub)

	return (url, "Fallback", status, filepath)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
	global LOADED_COOKIE_JAR

	# ── CLI arguments ──────────────────────────────────────────────────────
	parser = argparse.ArgumentParser(
		description="Convert AnyBox JSON export into Obsidian-ready Markdown notes."
	)
	parser.add_argument(
		"--input", default=INPUT_JSON,
		help=f"Path to AnyBox JSON export (default: {INPUT_JSON})"
	)
	parser.add_argument(
		"--output", default=OUTPUT_ROOT,
		help=f"Output root folder (default: {OUTPUT_ROOT})"
	)
	parser.add_argument(
		"--start", type=int, default=0,
		help="0-based index of first item to process (default: 0)"
	)
	parser.add_argument(
		"--limit", type=int, default=0,
		help="Number of items to process; 0 means all (default: 0)"
	)
	parser.add_argument(
		"--workers", type=int, default=MAX_WORKERS,
		help=f"Parallel download workers (default: {MAX_WORKERS})"
	)
	parser.add_argument(
		"--skip-existing", action="store_true",
		help="Skip items whose output file already exists"
	)
	args = parser.parse_args()

	# ── Load cookies ───────────────────────────────────────────────────────
	if os.path.exists(COOKIE_FILE):
		try:
			LOADED_COOKIE_JAR = load_cookie_jar(COOKIE_FILE)
			print(f"🔐 Loaded cookies from {COOKIE_FILE}")
		except Exception as e:
			print(f"⚠️  Cookie error: {e}")
	else:
		print(f"ℹ️  No cookie file found at {COOKIE_FILE} — running without cookies")

	# ── Load AnyBox export ─────────────────────────────────────────────────
	with open(args.input, "r", encoding="utf-8") as f:
		data = json.load(f)

	total_items = len(data)
	end_index = (args.start + args.limit) if args.limit > 0 else total_items
	items = data[args.start:end_index]

	# ── Prepare log output folder ──────────────────────────────────────────
	timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	os.makedirs("logs", exist_ok=True)

	log_file    = os.path.join("logs", f"anybox_import_log_{timestamp}.csv")
	reject_file = os.path.join("logs", f"anybox_rejections_{timestamp}.csv")

	print(f"\n🚀 Processing items {args.start}–{args.start + len(items) - 1} of {total_items - 1}")
	print(f"   Workers       : {args.workers}")
	print(f"   Output folder : {args.output}")
	print(f"   Full log      : {log_file}")
	print(f"   Rejection log : {reject_file}")
	print()

	# ── Run parallel processing ────────────────────────────────────────────
	results = []
	counts = {"Success": 0, "Fallback": 0, "Skipped": 0, "Error": 0}
	total = len(items)

	with ThreadPoolExecutor(max_workers=args.workers) as executor:
		futures = {
			executor.submit(process_item, item, args.output, args.skip_existing): item
			for item in items
		}

		done = 0
		for future in as_completed(futures):
			try:
				row = future.result()
			except Exception as e:
				row = ("", "Error", f"worker_exception:{str(e)[:80]}", "")

			results.append(row)

			result_type = row[1]
			counts[result_type] = counts.get(result_type, 0) + 1

			done += 1

			# Print a new line every 10 items and at the very end.
			# Each line is kept so you can scroll back and compare
			# rejection counts across chunks.
			if done % 10 == 0 or done == total:
				with PRINT_LOCK:
					print(
						f"  [{done:4d}/{total}]  "
						f"✅ {counts['Success']}  "
						f"📄 {counts['Fallback']}  "
						f"⏭️  {counts['Skipped']}  "
						f"❌ {counts['Error']}"
					)

	# ── Write full CSV log ─────────────────────────────────────────────────
	# lineterminator="\n" prevents csv.writer from adding \r on some platforms
	with open(log_file, "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f, lineterminator="\n")
		writer.writerow(["URL", "Result", "Details", "Filepath"])
		writer.writerows(results)

	# ── Write rejection CSV log ────────────────────────────────────────────
	# Only Fallback items are written here.
	with open(reject_file, "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f, lineterminator="\n")
		writer.writerow(["URL", "Result", "Details", "Filepath"])
		for row in results:
			if row[1] == "Fallback":
				writer.writerow(row)

	# ── Final summary ──────────────────────────────────────────────────────
	print()
	print("─" * 50)
	print("✨ Done!")
	print(f"   ✅ Success  : {counts['Success']}")
	print(f"   📄 Fallback : {counts['Fallback']}")
	print(f"   ⏭️  Skipped  : {counts['Skipped']}")
	print(f"   ❌ Error    : {counts['Error']}")
	print(f"   📋 Full log : {log_file}")
	print(f"   ❗ Rejects  : {reject_file}")


if __name__ == "__main__":
	main()