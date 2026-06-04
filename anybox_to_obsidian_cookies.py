#!/usr/bin/env python3
"""
anybox_to_obsidian_cookies.py
============================
Converts an AnyBox JSON export into Obsidian-ready Markdown notes.

Features:
	- Parallel HTTP fetching
	- Cookie support (medium.com_cookies.txt)
	- Multiple extraction backends (trafilatura, readability, BeautifulSoup)
	- Obsidian-compliant tags: extracts last element of AnyBox tag paths, replaces spaces with hyphens, and deduplicates.
	- AnyBox folder structure preservation.
	- Consistent tab indentation for IDE compatibility.
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

try:
	import trafilatura
	import trafilatura.metadata
	HAS_TRAFILATURA = True
except ImportError:
	HAS_TRAFILATURA = False
	print("⚠️  trafilatura not installed. Falling back to readability/BeautifulSoup only.")

try:
	from readability import Document
	HAS_READABILITY = True
except ImportError:
	HAS_READABILITY = False
	print("⚠️  readability-lxml not installed. Falling back to BeautifulSoup only.")


# ─── Configuration ────────────────────────────────────────────────────────────

INPUT_JSON      = "./data/AnyBoxExport.json"
OUTPUT_ROOT     = "./staging"
COOKIE_FILE     = "./medium.com_cookies.txt"
MAX_WORKERS     = 4
REQUEST_TIMEOUT = 20

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

THREAD_LOCAL      = threading.local()
LOADED_COOKIE_JAR = None
EXTRACT_LOCK      = threading.Lock()

# ─── HTML tag classification ──────────────────────────────────────────────────

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
	s = requests.Session()
	retry = Retry(
		total=2,
		backoff_factor=1,
		status_forcelist=[429, 500, 502, 503, 504],
	)
	s.mount("https://", HTTPAdapter(max_retries=retry))
	s.mount("http://",  HTTPAdapter(max_retries=retry))
	s.headers.update(BASE_HEADERS)
	return s


def get_session():
	global LOADED_COOKIE_JAR
	session = getattr(THREAD_LOCAL, "session", None)
	if session is None:
		session = build_session()
		if LOADED_COOKIE_JAR is not None:
			session.cookies.update(LOADED_COOKIE_JAR)
		THREAD_LOCAL.session = session
	return session


def load_cookie_jar(cookie_path):
	if not os.path.exists(cookie_path):
		raise FileNotFoundError(f"Cookie file not found: {cookie_path}")

	jar   = requests.cookies.RequestsCookieJar()
	lower = cookie_path.lower()

	if lower.endswith(".txt") or lower.endswith(".cookies"):
		moz = MozillaCookieJar(cookie_path)
		moz.load(ignore_discard=True, ignore_expires=True)
		for cookie in moz:
			jar.set(
				cookie.name, cookie.value,
				domain=cookie.domain, path=cookie.path, secure=cookie.secure,
			)
		return jar

	if lower.endswith(".json"):
		with open(cookie_path, "r", encoding="utf-8") as f:
			data = json.load(f)
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
	if value is None:
		value = ""
	value = str(value)
	value = value.replace("\\", "\\\\")
	value = value.replace('"',  '\\"')
	value = value.replace("\n", " ")
	return f'"{value}"'


def slugify(text):
	if not text:
		return "untitled"
	s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()
	s = re.sub(r"\s+", " ", s)
	return s[:200]


def parse_date(raw):
	try:
		dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
		return dt.strftime("%Y-%m-%d")
	except Exception:
		return datetime.now().strftime("%Y-%m-%d")


def sanitize_html(html):
	html = html.replace("\x00", "")
	html = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", html)
	return html


def clean_path_part(text):
	text = str(text).strip()
	text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
	return text or "Imported"


def normalize_folder(folder_value):
	if not folder_value:
		return "Imported"
	if isinstance(folder_value, str):
		return clean_path_part(folder_value)
	if isinstance(folder_value, list):
		parts = [clean_path_part(p) for p in folder_value if str(p).strip()]
		return os.path.join(*parts) if parts else "Imported"
	return clean_path_part(folder_value)


def extract_obsidian_tags(raw_anybox_tags):
	"""
	AnyBox tags are arrays of arrays (paths). 
	This takes the last element of each path, replaces spaces with hyphens,
	and deduplicates (case-insensitive dedupe, keeping original case).
	"""
	if not raw_anybox_tags or not isinstance(raw_anybox_tags, list):
		return []
	
	processed_tags = []
	seen = set()
	
	for tag_path in raw_anybox_tags:
		if isinstance(tag_path, list) and tag_path:
			# Only take the last element of the path
			tag_label = str(tag_path[-1]).strip()
			if tag_label:
				# Replace spaces with hyphens for Obsidian compatibility
				sanitized = tag_label.replace(" ", "-")
				# Deduplicate based on lowercase but store formatted string
				if sanitized.lower() not in seen:
					seen.add(sanitized.lower())
					processed_tags.append(sanitized)
					
	return processed_tags


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — URL & DOMAIN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_medium_url(url):
	try:
		host = urlparse(url).netloc.lower()
		return host.endswith("medium.com")
	except Exception:
		return False


def normalize_author(author):
	if not author:
		return ""
	author = str(author).strip()
	if author.startswith("http://") or author.startswith("https://"):
		parsed = urlparse(author)
		host   = parsed.netloc
		path   = parsed.path
		if host.endswith(".medium.com"):
			username = host.split(".medium.com")[0]
			if username and username not in ("www", ""):
				return username
		path_match = re.match(r"^/@([^/]+)", path)
		if path_match:
			return path_match.group(1)
		parts = [p for p in path.split("/") if p]
		if parts:
			candidate = parts[-1]
			if not re.fullmatch(r"[a-f0-9]{8,}", candidate):
				return candidate
		return ""
	author = re.sub(r"\S+@\S+\.\S+", "", author)
	author = author.strip(".,;:-–—|")
	author = re.sub(r"\s+", " ", author).strip()
	return author


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def extract_lead_image(soup, base_url):
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
			if img: return urljoin(base_url, img["src"])
	img = soup.find("img", src=True)
	return urljoin(base_url, img["src"]) if img else ""


def extract_author_published(soup):
	author, published = "", ""
	for attr, val in [("property", "article:author"), ("name", "author"), ("property", "og:article:author")]:
		tag = soup.find("meta", attrs={attr: val})
		if tag and tag.get("content"):
			author = tag["content"].strip()
			break
	for attr, val in [("property", "article:published_time"), ("name", "pubdate"), ("itemprop", "datePublished")]:
		tag = soup.find("meta", attrs={attr: val})
		if tag and tag.get("content"):
			raw = tag["content"].strip()
			try:
				dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
				published = dt.strftime("%Y-%m-%d")
			except Exception:
				published = raw[:10]
			break
	return normalize_author(author), published


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — MEDIUM PAYWALL DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def looks_like_medium_preview(url, content):
	if not is_medium_url(url) or not content:
		return False
	text = re.sub(r"\s+", " ", content[:12000]).lower()
	markers = ["continue reading with membership", "sign up to read this story", "member-only story"]
	return any(m in text for m in markers) and len(re.findall(r"\w+", text)) < 700


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — CUSTOM HTML → MARKDOWN RENDERER
# ══════════════════════════════════════════════════════════════════════════════

def clean_inline_text(text):
	if not text: return ""
	text = text.replace("\xa0", " ")
	text = re.sub(r"[ \t\r\f\v]+", " ", text)
	return text.strip()

def render_inlines(node):
	if isinstance(node, NavigableString): return str(node)
	if not isinstance(node, Tag): return ""
	name = node.name.lower()
	if name in IGNORE_TAGS: return ""
	if name == "br": return "\n"
	if name == "code": 
		return f"`{clean_inline_text(node.get_text())}`"
	if name == "a":
		href = node.get("href", "").strip()
		txt = clean_inline_text(node.get_text())
		return f"[{txt}]({href})" if href else txt
	if name in {"strong", "b"}: return f"**{render_inlines_recursive(node)}**"
	if name in {"em", "i"}: return f"*{render_inlines_recursive(node)}*"
	return render_inlines_recursive(node)

def render_inlines_recursive(node):
	return "".join(render_inlines(c) for c in node.children)

def render_block(node):
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
	return "".join(render_block(c) if isinstance(c, Tag) else "" for c in node.children)

def render_container(container):
	return "".join(render_block(c) if isinstance(c, Tag) else "" for c in container.children)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — FETCH & EXTRACT
# ══════════════════════════════════════════════════════════════════════════════

def fetch_and_extract(url):
	session = get_session()
	try:
		resp = session.get(url, timeout=REQUEST_TIMEOUT)
		if resp.status_code != 200:
			return None, f"HTTP {resp.status_code}", {}
		html = sanitize_html(resp.text)
		soup = BeautifulSoup(html, "html.parser")
		lead_image = extract_lead_image(soup, resp.url)
		author, pub = extract_author_published(soup)
		meta = {"author": author, "published": pub, "lead_image": lead_image}

		candidates = []
		if HAS_TRAFILATURA:
			with EXTRACT_LOCK:
				md = trafilatura.extract(html, output_format="markdown", include_tables=True, favor_precision=True)
				if md: candidates.append(("trafilatura:precision", md))
		
		if HAS_READABILITY:
			with EXTRACT_LOCK:
				doc = Document(html)
				md = render_container(BeautifulSoup(doc.summary(), "html.parser"))
				if md: candidates.append(("readability", md))

		if not candidates:
			return None, "no_content", meta

		# Simple scoring: choose longest for now, detect preview
		candidates.sort(key=lambda x: len(x[1]), reverse=True)
		best_status, best_md = candidates[0]
		
		if looks_like_medium_preview(url, best_md):
			return None, "medium_paywalled_preview", meta
			
		return best_md, best_status, meta

	except Exception as e:
		return None, f"error:{str(e)[:50]}", {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — NOTE BUILDING & PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

def build_frontmatter(item, created_date, author, published, lead_image):
	title = item.get("title", "Untitled")
	url = item.get("url", "")
	desc = item.get("description", "")
	# Extract and process tags from AnyBox JSON
	tags = extract_obsidian_tags(item.get("tags", []))

	lines = ["---"]
	lines.append(f"title: {yaml_quote(title)}")
	lines.append(f"source: {yaml_quote(url)}")
	lines.append(f"author: {yaml_quote(author)}")
	lines.append(f"published: {yaml_quote(published)}")
	lines.append(f"created: {created_date}")
	lines.append(f"description: {yaml_quote(desc)}")
	if lead_image: lines.append(f"lead_image: {yaml_quote(lead_image)}")
	lines.append("tags:")
	if tags:
		for t in tags: lines.append(f"  - {t}")
	else:
		lines.append("  []")
	lines.append("---")
	return "\n".join(lines) + "\n\n"

def process_item(item, output_root, skip_existing):
	url = item.get("url", "")
	title = item.get("title", "Untitled")
	folder = normalize_folder(item.get("folder"))
	created = parse_date(item.get("dateAdded", ""))
	
	filename = f"{slugify(title)}.md"
	target_dir = os.path.join(output_root, folder)
	os.makedirs(target_dir, exist_ok=True)
	filepath = os.path.join(target_dir, filename)

	if skip_existing and os.path.exists(filepath):
		return {"url": url, "result": "Skipped", "details": "exists", "folder": folder}

	if not url:
		return {"url": url, "result": "Skipped", "details": "no_url", "folder": folder}

	content, status, meta = fetch_and_extract(url)
	fm = build_frontmatter(item, created, meta.get("author"), meta.get("published"), meta.get("lead_image"))

	if content:
		with open(filepath, "w", encoding="utf-8") as f:
			f.write(fm + content)
		return {"url": url, "result": "Success", "details": status, "folder": folder}
	else:
		return {"url": url, "result": "Fallback", "details": status, "folder": folder}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
	global LOADED_COOKIE_JAR
	parser = argparse.ArgumentParser()
	parser.add_argument("--start", type=int, default=0)
	parser.add_argument("--limit", type=int, default=0)
	parser.add_argument("--workers", type=int, default=MAX_WORKERS)
	parser.add_argument("--skip-existing", action="store_true")
	args = parser.parse_args()

	if os.path.exists(COOKIE_FILE):
		try:
			LOADED_COOKIE_JAR = load_cookie_jar(COOKIE_FILE)
			print(f"🔐 Loaded cookies from {COOKIE_FILE}")
		except Exception as e:
			print(f"⚠️  Cookie error: {e}")

	with open(INPUT_JSON, "r", encoding="utf-8") as f:
		data = json.load(f)

	end = (args.start + args.limit) if args.limit > 0 else len(data)
	items = data[args.start:end]
	
	results = []
	with ThreadPoolExecutor(max_workers=args.workers) as executor:
		futures = [executor.submit(process_item, it, OUTPUT_ROOT, args.skip_existing) for it in items]
		done = 0
		for f in as_completed(futures):
			results.append(f.result())
			done += 1
			if done % 10 == 0: print(f"Proc: {done}/{len(items)}")

	# Summary
	counts = {"Success": 0, "Fallback": 0, "Skipped": 0}
	for r in results: counts[r["result"]] = counts.get(r["result"], 0) + 1
	print(f"\n✨ Done! Success: {counts['Success']}, Fallback: {counts['Fallback']}, Skip: {counts['Skipped']}")

if __name__ == "__main__":
	main()