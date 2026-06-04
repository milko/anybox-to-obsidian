#!/usr/bin/env python3
"""
anybox_to_obsidian_cookies.py
============================
Converts an AnyBox JSON export into Obsidian-ready Markdown notes.

Features:
	- Parallel HTTP fetching (configurable workers)
	- Cookie support (medium.com_cookies.txt)
	- Multiple extraction backends (trafilatura, readability, BeautifulSoup)
	- Obsidian-compliant tags
	- AnyBox folder structure preservation
	- Live progress counter
	- CSV log of every processed item
	- Rejections CSV for Fallback items (with Tags column)
	- CLI flags: --input, --output, --start, --limit, --workers, --skip-existing
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

# ─── Optional dependencies ────────────────────────────────────────────────────
# These libraries improve extraction quality but the script degrades gracefully
# if they are not installed. Install with:
#   pip install trafilatura readability-lxml

try:
	import trafilatura
	import trafilatura.metadata
	HAS_TRAFILATURA = True
except ImportError:
	HAS_TRAFILATURA = False
	print("⚠️  trafilatura not installed.")

try:
	from readability import Document
	HAS_READABILITY = True
except ImportError:
	HAS_READABILITY = False
	print("⚠️  readability-lxml not installed.")


# ─── Configuration ────────────────────────────────────────────────────────────
# Edit these defaults to match your local setup.
# They can also be overridden at runtime via CLI arguments.

INPUT_JSON      = "./data/AnyBoxExport.json"   # Path to your AnyBox JSON export
OUTPUT_ROOT     = "./staging"                   # Where Markdown notes will be written
COOKIE_FILE     = "./medium.com_cookies.txt"    # Netscape or JSON cookie file (optional)
MAX_WORKERS     = 4                             # Parallel download threads
REQUEST_TIMEOUT = 20                            # Seconds before a request is abandoned

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

# ─── Thread-safety globals ────────────────────────────────────────────────────
# Each worker thread gets its own HTTP session via THREAD_LOCAL.
# LOADED_COOKIE_JAR is shared read-only across all threads after startup.

THREAD_LOCAL      = threading.local()
LOADED_COOKIE_JAR = None
EXTRACT_LOCK      = threading.Lock()   # Serialises trafilatura/readability calls
PRINT_LOCK        = threading.Lock()   # Prevents garbled console output

# ─── HTML tag classification ──────────────────────────────────────────────────
# IGNORE_TAGS: stripped entirely during custom HTML→Markdown rendering
# BLOCK_TAGS:  treated as block-level elements (newlines around them)

IGNORE_TAGS = {
	"script", "style", "noscript", "nav", "footer", "header", "aside",
	"form", "button", "input", "select", "option", "textarea",
	"svg", "canvas",
}

BLOCK_TAGS = {
	"article", "section", "main", "div", "p", "figure", "figcaption",
	"details", "summary", "h1", "h2", "h3", "h4", "h5", "h6",
	"pre", "blockquote", "ul", "ol", "li", "table",
	"thead", "tbody", "tfoot", "tr", "td", "th", "hr",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — HTTP SESSION & COOKIES
# ══════════════════════════════════════════════════════════════════════════════

def build_session():
	"""Create a requests.Session with automatic retries on transient errors."""
	s = requests.Session()
	retry = Retry(
		total=2,                                    # Retry up to 2 times
		backoff_factor=1,                           # Wait 1s, 2s between retries
		status_forcelist=[429, 500, 502, 503, 504], # Retry on these HTTP codes
	)
	s.mount("https://", HTTPAdapter(max_retries=retry))
	s.mount("http://",  HTTPAdapter(max_retries=retry))
	s.headers.update(BASE_HEADERS)
	return s


def get_session():
	"""
	Return the HTTP session for the current thread.
	Creates a new session on first call per thread and injects cookies if available.
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
	Load cookies from a Netscape .txt/.cookies file or a JSON export.
	Returns a requests.cookies.RequestsCookieJar ready to inject into sessions.

	Supported formats:
	  - Netscape format (.txt or .cookies): exported by browser extensions
	    such as "Get cookies.txt LOCALLY"
	  - JSON format (.json): exported by extensions such as "Cookie-Editor"
	    (either a list of cookie objects or {"cookies": [...]} wrapper)
	"""
	if not os.path.exists(cookie_path):
		raise FileNotFoundError(f"Cookie file not found: {cookie_path}")

	jar   = requests.cookies.RequestsCookieJar()
	lower = cookie_path.lower()

	# ── Netscape / Mozilla format ─────────────────────────────────────────
	if lower.endswith(".txt") or lower.endswith(".cookies"):
		moz = MozillaCookieJar(cookie_path)
		moz.load(ignore_discard=True, ignore_expires=True)
		for cookie in moz:
			jar.set(
				cookie.name, cookie.value,
				domain=cookie.domain, path=cookie.path, secure=cookie.secure,
			)
		return jar

	# ── JSON format ───────────────────────────────────────────────────────
	if lower.endswith(".json"):
		with open(cookie_path, "r", encoding="utf-8") as f:
			data = json.load(f)
		# Normalise to a flat list of cookie dicts
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
			name  = item.get("name")
			value = item.get("value")
			if not name or value is None:
				continue
			jar.set(
				name, value,
				domain=item.get("domain", ""),
				path=item.get("path", "/"),
				secure=bool(item.get("secure", False)),
			)
		return jar

	raise ValueError("Unsupported cookie file format. Use .txt/.cookies or .json")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def yaml_quote(value):
	"""
	Wrap a value in double quotes for safe YAML frontmatter embedding.
	Escapes backslashes, double quotes, and newlines.
	"""
	if value is None:
		value = ""
	value = str(value)
	value = value.replace("\\", "\\\\")
	value = value.replace('"',  '\\"')
	value = value.replace("\n", " ")
	return f'"{value}"'


def slugify(text):
	"""
	Convert a title into a safe filename, preserving original case.
	Strips characters that are illegal on macOS/Windows/Linux filesystems
	and collapses whitespace. Truncates to 200 characters.
	"""
	if not text:
		return "untitled"
	s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()
	s = re.sub(r"\s+", " ", s)
	return s[:200]


def parse_date(raw):
	"""
	Parse an ISO-8601 date string (as stored by AnyBox) into YYYY-MM-DD.
	Falls back to today's date if parsing fails.
	"""
	try:
		dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
		return dt.strftime("%Y-%m-%d")
	except Exception:
		return datetime.now().strftime("%Y-%m-%d")


def sanitize_html(html):
	"""
	Strip null bytes and other control characters that cause BeautifulSoup
	or trafilatura to raise ValueError during parsing.
	"""
	html = html.replace("\x00", "")
	html = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", html)
	return html


def clean_path_part(text):
	"""
	Sanitize a single folder-name component for use in os.path.join().
	Removes filesystem-illegal characters and falls back to 'Imported'.
	"""
	text = str(text).strip()
	text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
	return text or "Imported"


def normalize_folder(folder_value):
	"""
	Normalise the 'folder' field from an AnyBox item.
	AnyBox can store folders as a string, a list of path components, or None.
	Returns a single OS path string suitable for os.path.join(output_root, ...).
	"""
	if not folder_value:
		return "Imported"
	if isinstance(folder_value, str):
		return clean_path_part(folder_value)
	if isinstance(folder_value, list):
		# Each element is one level of the folder hierarchy
		parts = [clean_path_part(p) for p in folder_value if str(p).strip()]
		return os.path.join(*parts) if parts else "Imported"
	return clean_path_part(folder_value)


def extract_obsidian_tags(raw_anybox_tags):
	"""
	Convert AnyBox tag paths into Obsidian-compatible tag strings.

	AnyBox stores tags as nested lists, e.g.:
	  [["Technology", "AI"], ["Reading"]]
	We take only the leaf (last element) of each path and replace spaces
	with hyphens so Obsidian treats them as single tokens.
	Duplicates (case-insensitive) are removed.
	"""
	if not raw_anybox_tags or not isinstance(raw_anybox_tags, list):
		return []
	processed_tags = []
	seen = set()
	for tag_path in raw_anybox_tags:
		if isinstance(tag_path, list) and tag_path:
			tag_label = str(tag_path[-1]).strip()
			if tag_label:
				sanitized = tag_label.replace(" ", "-")
				if sanitized.lower() not in seen:
					seen.add(sanitized.lower())
					processed_tags.append(sanitized)
	return processed_tags


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — URL & DOMAIN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_medium_url(url):
	"""Return True if the URL belongs to medium.com or a Medium custom domain."""
	try:
		host = urlparse(url).netloc.lower()
		return host.endswith("medium.com")
	except Exception:
		return False


def normalize_author(author):
	"""
	Clean up an author string.
	Medium often returns a profile URL instead of a name; this function
	extracts a readable username from common Medium URL patterns.
	Also strips email addresses and stray punctuation from plain-text authors.
	"""
	if not author:
		return ""
	author = str(author).strip()

	# Handle URL-style authors (common on Medium)
	if author.startswith("http://") or author.startswith("https://"):
		parsed = urlparse(author)
		host   = parsed.netloc
		path   = parsed.path
		# Subdomain-style: username.medium.com
		if host.endswith(".medium.com"):
			username = host.split(".medium.com")[0]
			if username and username not in ("www", ""):
				return username
		# Path-style: medium.com/@username
		path_match = re.match(r"^/@([^/]+)", path)
		if path_match:
			return path_match.group(1)
		# Generic: last path segment that isn't a hex ID
		parts = [p for p in path.split("/") if p]
		if parts:
			candidate = parts[-1]
			if not re.fullmatch(r"[a-f0-9]{8,}", candidate):
				return candidate
		return ""

	# Plain-text author: strip emails and stray punctuation
	author = re.sub(r"\S+@\S+\.\S+", "", author)
	author = author.strip(".,;:-–—|")
	author = re.sub(r"\s+", " ", author).strip()
	return author


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def extract_lead_image(soup, base_url):
	"""
	Find the best representative image for the article.
	Priority: og:image → twitter:image → first <img> inside article/main → any <img>.
	Returns an absolute URL or empty string.
	"""
	og = soup.find("meta", property="og:image")
	if og and og.get("content"):
		return urljoin(base_url, og["content"])
	tw = soup.find("meta", attrs={"name": "twitter:image"})
	if tw and tw.get("content"):
		return urljoin(base_url, tw["content"])
	# Look inside common article containers before falling back to any image
	for selector in ["article", "main", ".post-content", ".entry-content"]:
		container = soup.select_one(selector)
		if container:
			img = container.find("img", src=True)
			if img: return urljoin(base_url, img["src"])
	img = soup.find("img", src=True)
	return urljoin(base_url, img["src"]) if img else ""


def extract_author_published(soup):
	"""
	Extract author name and publication date from HTML meta tags.
	Tries multiple common meta tag conventions in priority order.
	Returns (author_str, published_date_str) — either may be empty.
	"""
	author, published = "", ""

	# Author: try Open Graph, then standard <meta name="author">
	for attr, val in [("property", "article:author"), ("name", "author"), ("property", "og:article:author")]:
		tag = soup.find("meta", attrs={attr: val})
		if tag and tag.get("content"):
			author = tag["content"].strip()
			break

	# Published date: try article:published_time, pubdate, then schema.org itemprop
	for attr, val in [("property", "article:published_time"), ("name", "pubdate"), ("itemprop", "datePublished")]:
		tag = soup.find("meta", attrs={attr: val})
		if tag and tag.get("content"):
			raw = tag["content"].strip()
			try:
				dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
				published = dt.strftime("%Y-%m-%d")
			except Exception:
				published = raw[:10]   # Best-effort: take the date portion
			break

	return normalize_author(author), published


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — MEDIUM PAYWALL DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def looks_like_medium_preview(url, content):
	"""
	Detect whether extracted content is just a Medium paywall preview.
	Checks for known paywall marker phrases AND a low word count (< 700 words),
	since a full article would be much longer even if it mentions membership.
	Returns True if the content looks like a truncated preview.
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


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — CUSTOM HTML → MARKDOWN RENDERER
# ══════════════════════════════════════════════════════════════════════════════
# Used as a fallback when trafilatura and readability are unavailable or
# produce no output. Walks the BeautifulSoup tree and emits Markdown.

def clean_inline_text(text):
	"""Normalise whitespace in inline text nodes."""
	if not text: return ""
	text = text.replace("\xa0", " ")           # Non-breaking space → regular space
	text = re.sub(r"[ \t\r\f\v]+", " ", text)  # Collapse horizontal whitespace
	return text.strip()

def render_inlines(node):
	"""Render a single inline node (text, link, bold, italic, code) to Markdown."""
	if isinstance(node, NavigableString): return str(node)
	if not isinstance(node, Tag): return ""
	name = node.name.lower()
	if name in IGNORE_TAGS: return ""
	if name == "br": return "\n"
	if name == "code":
		return f"`{clean_inline_text(node.get_text())}`"
	if name == "a":
		href = node.get("href", "").strip()
		txt  = clean_inline_text(node.get_text())
		return f"[{txt}]({href})" if href else txt
	if name in {"strong", "b"}: return f"**{render_inlines_recursive(node)}**"
	if name in {"em", "i"}:     return f"*{render_inlines_recursive(node)}*"
	return render_inlines_recursive(node)

def render_inlines_recursive(node):
	"""Concatenate inline rendering of all children of a node."""
	return "".join(render_inlines(c) for c in node.children)

def render_block(node):
	"""
	Render a block-level HTML element to Markdown.
	Handles headings, paragraphs, code blocks, lists, and generic containers.
	"""
	if not node or not isinstance(node, Tag): return ""
	name = node.name.lower()
	if name in IGNORE_TAGS: return ""
	if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
		return f"{'#' * int(name[1])} {clean_inline_text(node.get_text())}\n\n"
	if name == "p":
		return f"{clean_inline_text(render_inlines_recursive(node))}\n\n"
	if name == "pre":
		return f"```\n{node.get_text().strip()}\n```\n\n"
	if name in {"ul", "ol"}:
		items = [f"- {clean_inline_text(li.get_text())}" for li in node.find_all("li", recursive=False)]
		return "\n".join(items) + "\n\n"
	# Generic container: recurse into children
	return "".join(render_block(c) if isinstance(c, Tag) else "" for c in node.children)

def render_container(container):
	"""Render all block children of a container element."""
	return "".join(render_block(c) if isinstance(c, Tag) else "" for c in container.children)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — FETCH & EXTRACT
# ══════════════════════════════════════════════════════════════════════════════

def fetch_and_extract(url):
	"""
	Download a URL and extract its main article content as Markdown.

	Extraction pipeline (in order of preference):
	  1. trafilatura with favor_precision=True  → best for clean articles
	  2. trafilatura with favor_precision=False → higher recall, more noise
	  3. readability-lxml                       → good for news sites
	  4. BeautifulSoup custom renderer          → last-resort fallback

	The candidate with the most text is chosen as the winner.
	If the result looks like a Medium paywall preview, it is rejected.

	Returns:
	  (content_str | None, status_str, meta_dict)
	  meta_dict keys: author, published, lead_image
	"""
	session = get_session()
	try:
		resp = session.get(url, timeout=REQUEST_TIMEOUT)
		if resp.status_code != 200:
			return None, f"HTTP {resp.status_code}", {}

		html = sanitize_html(resp.text)
		soup = BeautifulSoup(html, "html.parser")

		# Extract metadata from HTML meta tags
		lead_image        = extract_lead_image(soup, resp.url)
		author, published = extract_author_published(soup)
		meta = {"author": author, "published": published, "lead_image": lead_image}

		candidates = []   # List of (status_label, markdown_text) tuples

		# ── trafilatura (precision mode) ──────────────────────────────────
		if HAS_TRAFILATURA:
			with EXTRACT_LOCK:
				md = trafilatura.extract(html, output_format="markdown", include_tables=True, favor_precision=True)
				if md: candidates.append(("trafilatura:precision", md))

			# ── trafilatura (recall mode) ─────────────────────────────────
			with EXTRACT_LOCK:
				md = trafilatura.extract(html, output_format="markdown", include_tables=True, favor_precision=False)
				if md: candidates.append(("trafilatura:recall", md))

		# ── readability-lxml ──────────────────────────────────────────────
		if HAS_READABILITY:
			with EXTRACT_LOCK:
				doc = Document(html)
				md  = render_container(BeautifulSoup(doc.summary(), "html.parser"))
				if md: candidates.append(("readability", md))

		# ── BeautifulSoup custom renderer (last resort) ───────────────────
		main = (
			soup.find("article") or
			soup.find("main") or
			soup.find(id=re.compile(r"content|article|post", re.I))
		)
		if main:
			md = render_container(main)
			if md: candidates.append(("beautifulsoup:main_custom_md", md))

		if not candidates:
			return None, "no_content", meta

		# Pick the candidate with the most text (longest string wins)
		candidates.sort(key=lambda x: len(x[1]), reverse=True)
		best_status, best_md = candidates[0]

		# Reject Medium paywall previews even if text was extracted
		if looks_like_medium_preview(url, best_md):
			return None, "medium_paywalled_preview", meta

		return best_md, best_status, meta

	except Exception as e:
		return None, f"error:{str(e)[:80]}", {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — NOTE BUILDING & PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

def build_frontmatter(item, created_date, author, published, lead_image):
	"""
	Build the YAML frontmatter block for an Obsidian note.
	Fields populated from the AnyBox JSON item:
	  title, source (url), created (dateAdded), description, tags
	Fields populated from the fetched webpage:
	  author, published, lead_image
	Note: the 'tags' list in the frontmatter uses the AnyBox tags,
	NOT the hardcoded 'clippings' tag — that is added manually after import.
	"""
	title = item.get("title", "Untitled")
	url   = item.get("url", "")
	desc  = item.get("description", "")
	tags  = extract_obsidian_tags(item.get("tags", []))

	lines = ["---"]
	lines.append(f"title: {yaml_quote(title)}")
	lines.append(f"source: {yaml_quote(url)}")
	lines.append(f"author: {yaml_quote(author)}")
	lines.append(f"published: {yaml_quote(published)}")
	lines.append(f"created: {created_date}")
	lines.append(f"description: {yaml_quote(desc)}")
	if lead_image:
		lines.append(f"lead_image: {yaml_quote(lead_image)}")
	lines.append("tags:")
	if tags:
		for t in tags:
			lines.append(f"  - {t}")
	else:
		lines.append("  []")
	lines.append("---")
	return "\n".join(lines) + "\n\n"


def process_item(item, output_root, skip_existing):
	"""
	Process a single AnyBox item:
	  1. Resolve output folder and filename from item metadata
	  2. Skip if file already exists and --skip-existing is set
	  3. Fetch and extract article content
	  4. Write a full Markdown note on success, or a stub note on failure
	  5. Return a result tuple for CSV logging

	Return tuple: (url, result_type, details, filepath, tags_str)
	  result_type: "Success" | "Fallback" | "Skipped"
	  tags_str: comma-separated AnyBox tags (used in the rejections CSV)
	"""
	url      = item.get("url", "")
	title    = item.get("title", "Untitled")
	folder   = normalize_folder(item.get("folder"))
	created  = parse_date(item.get("dateAdded", ""))
	tags     = extract_obsidian_tags(item.get("tags", []))
	tags_str = ", ".join(tags) if tags else ""   # For the CSV log

	filename   = f"{slugify(title)}.md"
	target_dir = os.path.join(output_root, folder)
	os.makedirs(target_dir, exist_ok=True)
	filepath   = os.path.join(target_dir, filename)

	# Items with no URL cannot be fetched — write nothing, log as Fallback
	if not url:
		return (url, "Fallback", "no_url", filepath, tags_str)

	# Skip already-processed notes when --skip-existing is active
	if skip_existing and os.path.exists(filepath):
		return (url, "Skipped", "file_exists", filepath, tags_str)

	content, status, meta = fetch_and_extract(url)
	fm = build_frontmatter(
		item, created,
		meta.get("author", ""),
		meta.get("published", ""),
		meta.get("lead_image", ""),
	)

	if content:
		# Full article extracted — write complete note
		with open(filepath, "w", encoding="utf-8") as f:
			f.write(fm + content)
		return (url, "Success", status, filepath, tags_str)
	else:
		# Extraction failed — write a stub note with a link to the original
		stub = (
			fm +
			f"> Full article could not be extracted. Status: `{status}`\n\n"
			f"[Open original article]({url})\n"
		)
		with open(filepath, "w", encoding="utf-8") as f:
			f.write(stub)
		return (url, "Fallback", status, filepath, tags_str)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
	global LOADED_COOKIE_JAR

	# ── CLI arguments ─────────────────────────────────────────────────────
	# --start and --limit allow chunked processing of large exports.
	# Example: process items 200–399 with 8 workers, skipping existing notes:
	#   python anybox_to_obsidian_cookies.py --start 200 --limit 200 --workers 8 --skip-existing
	parser = argparse.ArgumentParser(description="Convert AnyBox JSON export to Obsidian Markdown notes.")
	parser.add_argument("--input",         default=INPUT_JSON,   help="Path to AnyBox JSON export")
	parser.add_argument("--output",        default=OUTPUT_ROOT,  help="Output root directory")
	parser.add_argument("--start",         type=int, default=0,  help="Start index (0-based)")
	parser.add_argument("--limit",         type=int, default=0,  help="Max items to process (0 = all)")
	parser.add_argument("--workers",       type=int, default=MAX_WORKERS, help="Parallel download threads")
	parser.add_argument("--skip-existing", action="store_true",  help="Skip items whose .md file already exists")
	args = parser.parse_args()

	# ── Load cookies ──────────────────────────────────────────────────────
	# Cookies are optional. If the file is missing the script runs without them.
	if os.path.exists(COOKIE_FILE):
		try:
			LOADED_COOKIE_JAR = load_cookie_jar(COOKIE_FILE)
			print(f"🔐 Loaded cookies from {COOKIE_FILE}")
		except Exception as e:
			print(f"⚠️  Cookie error: {e}")

	# ── Load and slice the AnyBox export ──────────────────────────────────
	with open(args.input, "r", encoding="utf-8") as f:
		data = json.load(f)

	end_index = (args.start + args.limit) if args.limit > 0 else len(data)
	items     = data[args.start:end_index]

	# ── Set up log files ──────────────────────────────────────────────────
	# Each run gets a timestamped log so previous runs are never overwritten.
	timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
	log_file    = os.path.join("logs", f"anybox_import_log_{timestamp}.csv")
	reject_file = os.path.join("logs", f"anybox_rejections_{timestamp}.csv")
	os.makedirs("logs", exist_ok=True)

	print(f"🚀 Processing items {args.start}–{args.start + len(items)} of {len(data)}")
	print(f"   Workers : {args.workers}")
	print(f"   Output  : {args.output}")
	print(f"   Log     : {log_file}")
	print(f"   Rejects : {reject_file}")
	print()

	# ── Process items in parallel ─────────────────────────────────────────
	results = []
	counts  = {"Success": 0, "Fallback": 0, "Skipped": 0, "Error": 0}
	total   = len(items)

	with ThreadPoolExecutor(max_workers=args.workers) as executor:
		futures = {
			executor.submit(process_item, it, args.output, args.skip_existing): it
			for it in items
		}
		done = 0
		for future in as_completed(futures):
			row = future.result()
			results.append(row)
			result_type = row[1]
			if result_type in counts:
				counts[result_type] += 1
			else:
				counts["Error"] += 1
			done += 1
			# Update progress line every 10 items (and on the last item)
			if done % 10 == 0 or done == total:
				with PRINT_LOCK:
					print(
						f"\r  [{done:4d}/{total}]  "
						f"✅ {counts['Success']}  "
						f"📄 {counts['Fallback']}  "
						f"⏭️  {counts['Skipped']}  "
						f"❌ {counts['Error']}",
						end="", flush=True
					)

	print()

	# ── Write full CSV log ────────────────────────────────────────────────
	# Contains every processed item regardless of outcome.
	with open(log_file, "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		writer.writerow(["URL", "Result", "Details", "Filepath", "Tags"])
		writer.writerows(results)

	# ── Write rejections CSV ──────────────────────────────────────────────
	# Contains only Fallback items. The Tags column lets you quickly copy
	# the correct tags when manually clipping the article in Obsidian.
	rejections = [r for r in results if r[1] == "Fallback"]
	with open(reject_file, "w", newline="", encoding="utf-8") as f:
		writer = csv.writer(f)
		writer.writerow(["URL", "Result", "Details", "Filepath", "Tags"])
		writer.writerows(rejections)

	# ── Summary ───────────────────────────────────────────────────────────
	print()
	print("─" * 50)
	print("✨ Done!")
	print(f"   ✅ Success  : {counts['Success']}")
	print(f"   📄 Fallback : {counts['Fallback']}")
	print(f"   ⏭️  Skipped  : {counts['Skipped']}")
	print(f"   ❌ Error    : {counts['Error']}")
	print(f"   📋 Log      : {log_file}")
	print(f"   ❗ Rejects  : {reject_file}")


if __name__ == "__main__":
	main()