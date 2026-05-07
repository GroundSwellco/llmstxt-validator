import re
import os
import time
import asyncio
import base64
import subprocess
from xml.etree import ElementTree as ET
import httpx
from charset_normalizer import from_bytes
from urllib.parse import urlparse
from typing import Optional
from dataclasses import dataclass, asdict

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from anthropic import AsyncAnthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    import stripe
    _STRIPE_AVAILABLE = True
except ImportError:
    _STRIPE_AVAILABLE = False


def _ensure_stripe():
    if not _STRIPE_AVAILABLE:
        raise HTTPException(status_code=503, detail="Stripe SDK is not installed on the server.")
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="STRIPE_SECRET_KEY is not configured on the server.")
    stripe.api_key = key


GENERATION_PRICES = {
    "llms-ctx.txt": {"label": "llms-ctx.txt generation", "amount_cents": 200},
    "llms-full.txt": {"label": "llms-full.txt generation", "amount_cents": 499},
}

app = FastAPI(title="LLMs.txt Validator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


class ValidateRequest(BaseModel):
    content: Optional[str] = None
    url: Optional[str] = None
    file_base64: Optional[str] = None
    file_type: str = "llms.txt"  # llms.txt, llms-ctx.txt, llms-full.txt


class DetectRequest(BaseModel):
    url: str


class GenerateRequest(BaseModel):
    url: str


LLMS_FILES = ["llms.txt", "llms-ctx.txt", "llms-full.txt"]

_GENERATE_RATE_LIMITS: dict[str, list[float]] = {}
_GENERATE_MAX_PER_HOUR = 5
_GENERATE_MAX_URLS = 25
_GENERATE_FETCH_TIMEOUT = 3.0
_GENERATE_FETCH_CONCURRENCY = 10
_anthropic_client_singleton = None


def _get_anthropic():
    global _anthropic_client_singleton
    if not _ANTHROPIC_AVAILABLE:
        raise HTTPException(status_code=503, detail="Anthropic SDK is not installed on the server.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured on the server.")
    if _anthropic_client_singleton is None:
        _anthropic_client_singleton = AsyncAnthropic()
    return _anthropic_client_singleton


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    cutoff = now - 3600
    times = [t for t in _GENERATE_RATE_LIMITS.get(ip, []) if t > cutoff]
    if len(times) >= _GENERATE_MAX_PER_HOUR:
        _GENERATE_RATE_LIMITS[ip] = times
        return False
    times.append(now)
    _GENERATE_RATE_LIMITS[ip] = times
    return True


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta\s+[^>]*?name=["\']description["\'][^>]*?content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_META_OG_DESC_RE = re.compile(
    r'<meta\s+[^>]*?property=["\']og:description["\'][^>]*?content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_HREF_RE = re.compile(r'<a\s+[^>]*?href=["\']([^"\'#]+)["\']', re.IGNORECASE)


def _extract_meta(html: str, pattern: re.Pattern) -> Optional[str]:
    m = pattern.search(html)
    if not m:
        return None
    text = re.sub(r"\s+", " ", m.group(1)).strip()
    return text or None


async def _fetch_sitemap_urls(client: httpx.AsyncClient, base_url: str) -> list[str]:
    """Fetch /sitemap.xml. Follows up to 4 nested sitemap files."""
    urls: list[str] = []
    sitemap_queue = [f"{base_url}/sitemap.xml"]
    seen_sitemaps: set[str] = set()
    while sitemap_queue and len(seen_sitemaps) < 4:
        sm_url = sitemap_queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        try:
            r = await client.get(sm_url, timeout=4.0)
            if r.status_code != 200:
                continue
            text = r.text
        except Exception:
            continue
        try:
            root = ET.fromstring(text.encode("utf-8"))
            for el in root.iter():
                if el.tag.endswith("loc") and el.text:
                    u = el.text.strip()
                    if not u:
                        continue
                    if u.lower().endswith(".xml") and len(seen_sitemaps) + len(sitemap_queue) < 4:
                        sitemap_queue.append(u)
                    else:
                        urls.append(u)
        except ET.ParseError:
            for m in re.finditer(r"<loc[^>]*>(.*?)</loc>", text, re.IGNORECASE | re.DOTALL):
                u = m.group(1).strip()
                if u:
                    urls.append(u)
        if len(urls) >= _GENERATE_MAX_URLS * 4:
            break
    return urls


async def _scrape_homepage_links(
    client: httpx.AsyncClient, base_url: str
) -> tuple[list[str], Optional[str], Optional[str]]:
    """Fall back when there's no sitemap. Returns (urls, homepage_title, homepage_desc)."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        r = await client.get(base_url, timeout=5.0)
        if r.status_code != 200:
            return [], None, None
        html = r.text
    except Exception:
        return [], None, None
    title = _extract_meta(html, _TITLE_RE)
    desc = _extract_meta(html, _META_DESC_RE) or _extract_meta(html, _META_OG_DESC_RE)
    seen: set[str] = set()
    urls: list[str] = []
    for m in _HREF_RE.finditer(html):
        href = m.group(1).strip()
        if href.startswith("/"):
            href = origin + href
        if not href.startswith(("http://", "https://")):
            continue
        ph = urlparse(href)
        if ph.netloc != parsed.netloc:
            continue
        clean = f"{ph.scheme}://{ph.netloc}{ph.path or '/'}"
        if clean in seen:
            continue
        seen.add(clean)
        urls.append(clean)
    return urls, title, desc


async def _fetch_page_metadata(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    diag: dict,
) -> Optional[dict]:
    """Fetch a page, fall back to curl when httpx is blocked, extract title+description."""
    async with sem:
        html: Optional[str] = None
        try:
            r = await client.get(url, timeout=_GENERATE_FETCH_TIMEOUT)
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "").lower()
                if ctype and "html" not in ctype:
                    diag["non_html"] += 1
                    return None
                html = r.text[:200_000]
                diag["httpx_ok"] += 1
            elif r.status_code in (403, 429, 503):
                # Likely bot defense — try curl with its trusted TLS fingerprint
                diag[f"httpx_{r.status_code}"] = diag.get(f"httpx_{r.status_code}", 0) + 1
                raw, ct = await asyncio.to_thread(_fetch_with_curl, url)
                if raw and (not ct or "html" in ct.lower()):
                    html = raw.decode("utf-8", errors="replace")[:200_000]
                    diag["curl_ok"] += 1
            else:
                diag["non_200"] += 1
                return None
        except Exception:
            diag["error"] += 1
            return None

        if not html:
            return None

        title = _extract_meta(html, _TITLE_RE)
        desc = _extract_meta(html, _META_DESC_RE) or _extract_meta(html, _META_OG_DESC_RE)
        if not title and not desc:
            diag["no_meta"] += 1
            return None
        return {"url": url, "title": title or url, "description": desc or ""}


_CTX_SYSTEM_PROMPT = """You are an expert at writing llms-ctx.txt files per the specification at https://llmstxt.org/.

llms-ctx.txt is the contextual variant of llms.txt — same structure but richer:
- An H1 with the project or site name
- A blockquote with a one-sentence summary
- A 3–5 sentence intro paragraph explaining what the site/project does and how the docs are organized
- H2 sections grouping related links
- Each link as: - [Title](URL): a 1–2 sentence description (longer than llms.txt — explain what the page covers and why someone would read it)

Output ONLY the raw file content — no code fences, no preamble, no commentary."""


_FULL_SYSTEM_PROMPT = """You are an expert at writing llms-full.txt files per the specification at https://llmstxt.org/.

llms-full.txt is the full-context variant — comprehensive, encyclopedic coverage of what the site documents:
- An H1 with the project or site name
- A blockquote with a one-sentence summary
- A 5–10 sentence intro that establishes context (what the project is, what problems it solves, who it's for, how the docs are organized)
- H2 sections grouping related links — each section opens with a 2–4 sentence paragraph describing what that section covers in depth
- Each link as: - [Title](URL): a 2–3 sentence description covering the key concepts and takeaways from that page

Aim for thorough, developer-grade documentation context. Output ONLY the raw file content — no code fences, no preamble, no commentary."""


_GENERATE_SYSTEM_PROMPT = """You are an expert at writing llms.txt files per the specification at https://llmstxt.org/.

The llms.txt format:
- An H1 with the project or site name (required)
- A blockquote with a one-sentence summary (recommended)
- An optional intro paragraph or two giving more context
- H2 sections grouping related links (e.g., Documentation, Examples, API Reference, Blog, Optional)
- Each link as: - [Title](URL): short description

Guidelines:
- Pick H2 sections that match how someone would actually navigate the site
- Skip noise (privacy policies, terms, sign-in, cart) unless the site is essentially that content
- Use clean, concrete page titles — rewrite long ones, drop boilerplate suffixes
- Keep descriptions short (5–15 words) and focused on what an LLM would learn from the page
- The intro paragraph (if any) should be 1–3 sentences with no marketing fluff
- Output ONLY the raw llms.txt content — no code fences, no preamble, no commentary"""


@dataclass
class ValidationError:
    line: int
    message: str
    severity: str  # error, warning


@dataclass
class ValidationResult:
    is_valid: bool
    file_type: str
    errors: list
    warnings: list
    stats: dict
    structure: dict


def estimate_tokens(text: str) -> int:
    """Estimate token count. Roughly 4 characters = 1 token for English."""
    # More accurate estimation based on common tokenizer behavior
    # Words + punctuation + whitespace patterns
    words = len(re.findall(r'\b\w+\b', text))
    punctuation = len(re.findall(r'[^\w\s]', text))
    # Approximate: 1 word ≈ 1.3 tokens on average
    return int(words * 1.3 + punctuation * 0.5)


def get_file_size(text: str) -> dict:
    """Get file size in bytes, KB, and MB."""
    size_bytes = len(text.encode('utf-8'))
    size_kb = size_bytes / 1024
    size_mb = size_kb / 1024
    return {
        "bytes": size_bytes,
        "kb": round(size_kb, 2),
        "mb": round(size_mb, 4),
        "formatted": f"{size_kb:.2f} KB" if size_kb < 1024 else f"{size_mb:.2f} MB"
    }


def detect_encoding(raw_bytes: bytes, content_type: str = None) -> dict:
    """Detect encoding from raw bytes and optional Content-Type header."""
    info = {
        "detected": None,
        "declared": None,
        "has_bom": False,
        "is_utf8": True,
        "recommendation": None,
    }

    # Check for BOM (Byte Order Mark)
    if raw_bytes.startswith(b'\xef\xbb\xbf'):
        info["has_bom"] = True
    elif raw_bytes.startswith(b'\xff\xfe') or raw_bytes.startswith(b'\xfe\xff'):
        info["has_bom"] = True

    # Extract charset from Content-Type header
    if content_type:
        for part in content_type.split(';'):
            part = part.strip().lower()
            if part.startswith('charset='):
                info["declared"] = part.split('=', 1)[1].strip().strip('"')
                break

    # Detect encoding from bytes using charset_normalizer
    result = from_bytes(raw_bytes).best()
    if result:
        # Normalize display name (e.g. utf_8 -> utf-8)
        info["detected"] = result.encoding.replace("_", "-")

    detected = (info["detected"] or "").lower().replace("-", "").replace("_", "")
    info["is_utf8"] = detected in ("utf8", "ascii")

    # Build recommendation
    if not info["is_utf8"]:
        info["recommendation"] = f"File appears to be encoded as {info['detected']}. UTF-8 is strongly recommended for llms.txt files to ensure compatibility with all LLM consumers."
    elif info["has_bom"]:
        info["recommendation"] = "File contains a BOM (Byte Order Mark). While valid, BOM can cause parsing issues with some LLM consumers. Consider saving as UTF-8 without BOM."
    elif info["declared"] and info["declared"].lower().replace("-", "") not in ("utf8", "ascii") and info["is_utf8"]:
        info["recommendation"] = f"Server declares charset={info['declared']} but content is actually UTF-8. Consider updating the server's Content-Type header to charset=utf-8."

    return info


def validate_llmstxt(content: str, file_type: str = "llms.txt") -> ValidationResult:
    """Validate llms.txt content against the specification."""

    errors = []
    warnings = []
    structure = {
        "h1_title": None,
        "blockquote": None,
        "h2_sections": [],
        "links": [],
        "has_optional_section": False
    }

    lines = content.split('\n')
    current_section = None
    h1_found = False
    blockquote_found = False
    h2_count = 0
    link_pattern = re.compile(r'^-\s*\[([^\]]+)\]\(([^)]+)\)(.*)$')
    url_pattern = re.compile(r'https?://[^\s\)]+')

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Check for H1 (required)
        if stripped.startswith('# ') and not stripped.startswith('## '):
            if h1_found:
                warnings.append(ValidationError(i, "Multiple H1 headers found. Only one is recommended.", "warning"))
            else:
                h1_found = True
                structure["h1_title"] = stripped[2:].strip()

        # Check for blockquote
        elif stripped.startswith('>'):
            if not blockquote_found:
                blockquote_found = True
                structure["blockquote"] = stripped[1:].strip()
            # Multiple blockquotes are OK for multi-line summaries

        # Check for H2 sections
        elif stripped.startswith('## '):
            h2_count += 1
            section_name = stripped[3:].strip()
            current_section = section_name
            structure["h2_sections"].append({
                "name": section_name,
                "line": i,
                "links": []
            })
            if section_name.lower() == "optional":
                structure["has_optional_section"] = True

        # Check for links
        elif stripped.startswith('- ['):
            match = link_pattern.match(stripped)
            if match:
                title = match.group(1)
                url = match.group(2)
                description = match.group(3).strip()
                if description.startswith(':'):
                    description = description[1:].strip()

                link_info = {
                    "title": title,
                    "url": url,
                    "description": description,
                    "line": i
                }
                structure["links"].append(link_info)

                if structure["h2_sections"]:
                    structure["h2_sections"][-1]["links"].append(link_info)

                # Validate URL format
                if not url_pattern.match(url) and not url.startswith('/'):
                    warnings.append(ValidationError(i, f"URL may be malformed: {url}", "warning"))
            else:
                errors.append(ValidationError(i, f"Invalid link format. Expected: - [Title](URL): description", "error"))

        # Check for malformed headers
        elif stripped.startswith('#') and not stripped.startswith('# ') and not stripped.startswith('## '):
            if stripped.startswith('###'):
                warnings.append(ValidationError(i, "H3+ headers are not part of the spec. Consider using H2.", "warning"))

    # Required elements check
    if not h1_found:
        errors.append(ValidationError(0, "Missing required H1 header (# Title)", "error"))

    if not blockquote_found:
        warnings.append(ValidationError(0, "Missing blockquote summary (> Description). Recommended.", "warning"))

    if h2_count == 0:
        warnings.append(ValidationError(0, "No H2 sections found. Consider adding sections for organization.", "warning"))

    # File size check
    size_info = get_file_size(content)
    if file_type == "llms.txt" and size_info["kb"] > 500:
        errors.append(ValidationError(0, f"File size ({size_info['formatted']}) exceeds 500KB limit for llms.txt", "error"))

    # Check for duplicate URLs
    urls = [link["url"] for link in structure["links"]]
    seen_urls = set()
    for i, url in enumerate(urls):
        if url in seen_urls:
            line = structure["links"][i]["line"]
            warnings.append(ValidationError(line, f"Duplicate URL found: {url}", "warning"))
        seen_urls.add(url)

    # Stats
    stats = {
        "characters": len(content),
        "lines": len(lines),
        "words": len(content.split()),
        "tokens_estimate": estimate_tokens(content),
        "size": size_info,
        "h1_count": 1 if h1_found else 0,
        "h2_count": h2_count,
        "link_count": len(structure["links"]),
        "has_blockquote": blockquote_found
    }

    is_valid = len(errors) == 0

    return ValidationResult(
        is_valid=is_valid,
        file_type=file_type,
        errors=[asdict(e) for e in errors],
        warnings=[asdict(w) for w in warnings],
        stats=stats,
        structure={
            "h1_title": structure["h1_title"],
            "blockquote": structure["blockquote"],
            "h2_sections": [{"name": s["name"], "link_count": len(s["links"])} for s in structure["h2_sections"]],
            "total_links": len(structure["links"]),
            "has_optional_section": structure["has_optional_section"]
        }
    )


def _fetch_with_curl(url: str) -> tuple:
    """Fallback fetch using curl subprocess. Returns (raw_bytes, content_type)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "10", "-f",
             "-w", "\n__CT__:%{content_type}", url],
            capture_output=True, timeout=15
        )
        if result.returncode == 0 and result.stdout:
            # Extract content_type from the appended marker
            raw = result.stdout
            ct_marker = b"\n__CT__:"
            ct_idx = raw.rfind(ct_marker)
            content_type = None
            if ct_idx != -1:
                content_type = raw[ct_idx + len(ct_marker):].decode("ascii", errors="ignore")
                raw = raw[:ct_idx]
            return raw, content_type
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return b"", None


async def fetch_llmstxt(url: str, file_type: str = "llms.txt") -> tuple:
    """Fetch llms.txt from a URL. Returns (content_str, encoding_info)."""

    # Parse and construct the llms.txt URL
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Determine which file to fetch
    file_path = f"/{file_type}"
    full_url = base_url + file_path

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        try:
            response = await client.get(full_url)
            response.raise_for_status()
            raw_bytes = response.content
            content_type = response.headers.get("content-type", "")
            enc_info = detect_encoding(raw_bytes, content_type)
            return response.text, enc_info
        except httpx.HTTPStatusError as e:
            # Some sites block Python HTTP clients via TLS fingerprinting.
            # Fall back to curl which has a trusted TLS fingerprint.
            if e.response.status_code == 403:
                raw_bytes, content_type = _fetch_with_curl(full_url)
                if raw_bytes:
                    enc_info = detect_encoding(raw_bytes, content_type)
                    return raw_bytes.decode("utf-8", errors="replace"), enc_info
            raise HTTPException(status_code=404, detail=f"Could not fetch {file_type} from {base_url}. Status: {e.response.status_code}")
        except httpx.RequestError as e:
            # Also try curl for connection errors (some WAFs reset connections)
            raw_bytes, content_type = _fetch_with_curl(full_url)
            if raw_bytes:
                enc_info = detect_encoding(raw_bytes, content_type)
                return raw_bytes.decode("utf-8", errors="replace"), enc_info
            raise HTTPException(status_code=400, detail=f"Error fetching URL: {str(e)}")


# HTML Template
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LLMs.txt Validator — Validate Your LLM-Ready Files</title>
    <meta name="description" content="Validate your llms.txt, llms-ctx.txt, and llms-full.txt files against the official specification. Catch errors, verify structure, and ensure your site is AI-ready.">
    <meta name="keywords" content="llms.txt, llms.txt validator, llms-full.txt, llms-ctx.txt, llmstxt, AI-ready web, LLM validation, llmstxt.org">
    <meta name="author" content="LLMs.txt Validator">
    <meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large">
    <meta name="theme-color" content="#2C3B4C">
    <link rel="canonical" href="https://llmvalidator.io/">
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='20' fill='%23F16365'/%3E%3Cpath d='M28 52 L44 68 L74 34' stroke='white' stroke-width='10' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
    <link rel="sitemap" type="application/xml" href="/sitemap.xml">
    <link rel="alternate" type="text/plain" title="llms.txt" href="/llms.txt">

    <!-- Open Graph -->
    <meta property="og:title" content="LLMs.txt Validator — Validate Your LLM-Ready Files">
    <meta property="og:description" content="Validate your llms.txt, llms-ctx.txt, and llms-full.txt files against the official spec. Free, instant, no sign-up.">
    <meta property="og:type" content="website">
    <meta property="og:url" content="https://llmvalidator.io/">
    <meta property="og:site_name" content="LLMs.txt Validator">
    <meta property="og:locale" content="en_US">
    <meta property="og:image" content="https://llmvalidator.io/og-image.png">
    <meta property="og:image:width" content="1200">
    <meta property="og:image:height" content="630">
    <meta property="og:image:alt" content="LLMs.txt Validator — Validate Your LLM-Ready Files">

    <!-- Twitter -->
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="LLMs.txt Validator — Validate Your LLM-Ready Files">
    <meta name="twitter:description" content="Validate your llms.txt files against the official spec. Free, instant, no sign-up.">
    <meta name="twitter:image" content="https://llmvalidator.io/og-image.png">
    <meta name="twitter:image:alt" content="LLMs.txt Validator — Validate Your LLM-Ready Files">

    <!-- JSON-LD: WebSite -->
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "WebSite",
      "name": "LLMs.txt Validator",
      "url": "https://llmvalidator.io/",
      "description": "Validate your llms.txt, llms-ctx.txt, and llms-full.txt files against the official specification.",
      "inLanguage": "en"
    }
    </script>

    <!-- JSON-LD: SoftwareApplication -->
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "SoftwareApplication",
      "name": "LLMs.txt Validator",
      "url": "https://llmvalidator.io/",
      "applicationCategory": "DeveloperApplication",
      "operatingSystem": "Any (Web)",
      "description": "Web-based validator for llms.txt, llms-ctx.txt, and llms-full.txt files. Checks structure, links, encoding, file size, and spec compliance.",
      "offers": {
        "@type": "Offer",
        "price": "0",
        "priceCurrency": "USD"
      },
      "featureList": [
        "Validate llms.txt, llms-ctx.txt, llms-full.txt",
        "Fetch from URL, paste content, or upload file",
        "Character count, file size, token estimation",
        "Line-level error and warning reporting",
        "Structure visualization (H1, blockquote, H2, links)",
        "Encoding detection (UTF-8, BOM, Content-Type)",
        "Inline editing and UTF-8 download"
      ]
    }
    </script>

    <!-- JSON-LD: FAQPage -->
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "FAQPage",
      "mainEntity": [
        {
          "@type": "Question",
          "name": "What is the llms.txt specification?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "The llms.txt specification is a proposed standard for providing LLM-friendly content on websites. It defines a simple markdown format that helps AI agents and language models understand what a site offers and where to find key resources. Learn more at llmstxt.org."
          }
        },
        {
          "@type": "Question",
          "name": "What file types can I validate?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "You can validate three file types: llms.txt (concise site overview, max 500KB), llms-ctx.txt (additional context for AI), and llms-full.txt (comprehensive documentation with no size limit)."
          }
        },
        {
          "@type": "Question",
          "name": "What does the validator check?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "The validator checks for required H1 headers, recommended blockquote summaries, proper H2 section structure, valid link formatting, URL correctness, duplicate links, file size limits, and character encoding. Each issue is reported with a specific line number so you can find and fix it quickly."
          }
        },
        {
          "@type": "Question",
          "name": "How are tokens estimated?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "Token estimates use an approximation of ~1.3 tokens per word plus ~0.5 per punctuation mark. This is a rough guide — actual tokenization varies by model. It helps you gauge whether your content fits within typical LLM context windows."
          }
        },
        {
          "@type": "Question",
          "name": "Is my content stored or shared?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "No. All validation happens in a single request. Content you paste or upload is processed server-side for validation and returned to your browser. Nothing is stored, logged, or shared with third parties."
          }
        },
        {
          "@type": "Question",
          "name": "Where can I learn more about the spec?",
          "acceptedAnswer": {
            "@type": "Answer",
            "text": "Visit llmstxt.org for the full specification, examples, and community resources. The spec is open and community-driven."
          }
        }
      ]
    }
    </script>
    <style>
        @font-face {
            font-family: 'NHaasGroteskTXPro';
            src: url('/static/Fonts/NHaasGroteskTXPro-65Md.ttf') format('truetype');
            font-weight: 500;
            font-style: normal;
            font-display: swap;
        }
        @font-face {
            font-family: 'NeuzeitGro';
            src: url('/static/Fonts/NeuzeitGro-Reg.ttf') format('truetype');
            font-weight: 400;
            font-style: normal;
            font-display: swap;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html { scroll-behavior: smooth; }
        body {
            font-family: 'NeuzeitGro', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a2330 0%, #2C3B4C 100%);
            background-attachment: fixed;
            min-height: 100vh;
            color: #e2e8f0;
        }
        h1, h2, h3, h4, h5, h6, .logo, .nav-logo, .stat-value {
            font-family: 'NHaasGroteskTXPro', 'Helvetica Neue', Helvetica, Arial, sans-serif;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 40px 20px; }

        header { text-align: center; margin-bottom: 40px; }
        .logo { font-size: 2.5rem; font-weight: 700; }
        .logo span { color: #7FBBE6; }
        .subtitle { color: #64748b; margin-top: 8px; }

        .input-section {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 30px;
        }

        .tabs { display: flex; gap: 8px; margin-bottom: 20px; }
        .tab {
            padding: 10px 20px;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            color: #94a3b8;
            cursor: pointer;
            transition: all 0.2s;
        }
        .tab:hover { background: rgba(255,255,255,0.1); }
        .tab.active { background: #7FBBE6; color: #fff; border-color: #7FBBE6; }

        .input-group { margin-bottom: 20px; }
        .input-group label { display: block; margin-bottom: 8px; color: #94a3b8; font-size: 0.9rem; }

        .url-input-wrapper { display: flex; gap: 10px; }
        .url-input {
            flex: 1;
            padding: 12px 16px;
            background: rgba(0,0,0,0.3);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            color: #fff;
            font-size: 1rem;
        }
        .url-input:focus { outline: none; border-color: #7FBBE6; }

        textarea {
            width: 100%;
            min-height: 200px;
            padding: 16px;
            background: rgba(0,0,0,0.3);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 8px;
            color: #fff;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.9rem;
            resize: vertical;
        }
        textarea:focus { outline: none; border-color: #7FBBE6; }
        textarea::placeholder { color: #475569; }

        .btn {
            padding: 12px 24px;
            background: #F16365;
            border: none;
            border-radius: 8px;
            color: #fff;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn:hover { background: #D94E50; transform: translateY(-1px); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
        .btn-secondary { background: rgba(255,255,255,0.1); }
        .btn-secondary:hover { background: rgba(255,255,255,0.2); }

        .file-type-selector { display: flex; gap: 8px; margin-bottom: 20px; }
        .file-type-btn {
            padding: 8px 16px;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 6px;
            color: #94a3b8;
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.2s;
        }
        .file-type-btn:hover { background: rgba(255,255,255,0.1); }
        .file-type-btn.active { background: rgba(127,187,230,0.2); color: #7FBBE6; border-color: #7FBBE6; }

        .results { display: none; }
        .results.show { display: block; }

        .result-header {
            display: flex;
            align-items: center;
            gap: 16px;
            padding: 20px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            margin-bottom: 20px;
        }

        .status-badge {
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 0.9rem;
        }
        .status-valid { background: rgba(127,187,230,0.2); color: #7FBBE6; }
        .status-invalid { background: rgba(239,68,68,0.2); color: #F16365; }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }

        .stat-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
            text-align: center;
        }
        .stat-value { font-size: 1.8rem; font-weight: 700; color: #7FBBE6; }
        .stat-label { font-size: 0.85rem; color: #64748b; margin-top: 4px; }

        .issues-section {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .issues-title { font-size: 1.1rem; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }
        .issues-title .count {
            background: rgba(255,255,255,0.1);
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.8rem;
        }

        .issue-item {
            display: flex;
            gap: 12px;
            padding: 12px;
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
            margin-bottom: 8px;
            align-items: flex-start;
        }
        .issue-item:last-child { margin-bottom: 0; }
        .issue-line {
            background: rgba(255,255,255,0.1);
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.8rem;
            color: #94a3b8;
            white-space: nowrap;
        }
        .issue-message { flex: 1; }
        .issue-error { border-left: 3px solid #F16365; }
        .issue-warning { border-left: 3px solid #FBD779; }

        .structure-section {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
        }
        .structure-title { font-size: 1.1rem; margin-bottom: 16px; }
        .structure-item {
            display: flex;
            justify-content: space-between;
            padding: 12px;
            background: rgba(0,0,0,0.2);
            border-radius: 8px;
            margin-bottom: 8px;
        }
        .structure-item:last-child { margin-bottom: 0; }
        .structure-label { color: #94a3b8; }
        .structure-value { color: #7FBBE6; font-weight: 500; }

        .encoding-intro {
            color: #94a3b8;
            font-size: 0.85rem;
            line-height: 1.5;
            margin-bottom: 16px;
        }
        .encoding-intro strong { color: #7FBBE6; }

        .info-icon {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 16px;
            height: 16px;
            border-radius: 50%;
            background: rgba(56,189,248,0.15);
            color: #7FBBE6;
            font-size: 0.65rem;
            font-weight: 700;
            font-style: normal;
            cursor: help;
            margin-left: 6px;
            position: relative;
            flex-shrink: 0;
        }
        .info-icon .tooltip {
            display: none;
            position: absolute;
            bottom: calc(100% + 8px);
            left: 50%;
            transform: translateX(-50%);
            background: #1e293b;
            border: 1px solid rgba(255,255,255,0.15);
            color: #e2e8f0;
            font-size: 0.78rem;
            font-weight: 400;
            padding: 8px 12px;
            border-radius: 8px;
            width: 260px;
            line-height: 1.4;
            z-index: 10;
            text-align: left;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            pointer-events: none;
        }
        .info-icon .tooltip::after {
            content: '';
            position: absolute;
            top: 100%;
            left: 50%;
            transform: translateX(-50%);
            border: 6px solid transparent;
            border-top-color: #1e293b;
        }
        .info-icon:hover .tooltip { display: block; }

        .upload-area {
            border: 2px dashed rgba(255,255,255,0.15);
            border-radius: 12px;
            padding: 40px 20px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s;
        }
        .upload-area:hover, .upload-area.dragover {
            border-color: #7FBBE6;
            background: rgba(127,187,230,0.05);
        }
        .upload-placeholder p { margin-top: 8px; color: #94a3b8; }
        .upload-file-info {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 16px;
            font-size: 1rem;
        }
        .upload-file-info span:first-child { color: #7FBBE6; font-weight: 500; }

        .loading { display: none; text-align: center; padding: 40px; }
        .loading.show { display: block; }

        .detector-sub {
            color: #94a3b8;
            max-width: 720px;
            margin: 0 auto 28px;
            text-align: center;
            line-height: 1.6;
        }
        .detector-sub code {
            background: rgba(255,255,255,0.06);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.9em;
            color: #e2e8f0;
        }
        .detector-summary {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            text-align: center;
            font-size: 1.05rem;
            color: #e2e8f0;
        }
        .detector-summary code {
            background: rgba(255,255,255,0.06);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.9em;
        }
        .detector-summary .summary-count { color: #7FBBE6; font-weight: 700; }
        .detector-summary .summary-count.none { color: #F16365; }
        .detector-summary .summary-note {
            margin-top: 8px;
            color: #94a3b8;
            font-size: 0.95rem;
        }
        .detector-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }
        .detector-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
        }
        .detector-card.found { border-color: rgba(127,187,230,0.4); }
        .detector-card.missing { border-color: rgba(239,68,68,0.25); }
        .detector-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
            gap: 12px;
        }
        .detector-card-name {
            font-family: 'JetBrains Mono', ui-monospace, monospace;
            font-weight: 600;
            color: #e2e8f0;
            font-size: 1rem;
        }
        .detector-card-meta {
            color: #94a3b8;
            font-size: 0.9rem;
            line-height: 1.6;
        }
        .detector-card-meta .meta-row { color: #64748b; }
        .detector-card-meta a {
            color: #7FBBE6;
            text-decoration: none;
            word-break: break-all;
        }
        .detector-card-meta a:hover { text-decoration: underline; }
        .detector-card-actions { margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }

        .generator-output {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 12px;
            margin-bottom: 16px;
        }
        .generator-output textarea {
            width: 100%;
            background: transparent;
            border: none;
            color: #e2e8f0;
            font-family: 'Monaco', 'Menlo', 'Consolas', monospace;
            font-size: 0.9rem;
            line-height: 1.55;
            resize: vertical;
            min-height: 320px;
            outline: none;
        }
        .generator-actions { display: flex; gap: 12px; flex-wrap: wrap; }
        .spinner {
            width: 40px;
            height: 40px;
            border: 3px solid rgba(255,255,255,0.1);
            border-left-color: #7FBBE6;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 16px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        footer { text-align: center; margin-top: 60px; color: #475569; font-size: 0.9rem; }
        footer a { color: #7FBBE6; text-decoration: none; }

        .example-link { color: #7FBBE6; cursor: pointer; font-size: 0.85rem; }
        .example-link:hover { text-decoration: underline; }

        /* Content Preview Panel */
        .content-preview-section {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .content-preview-title {
            font-size: 1.1rem;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .preview-actions { display: flex; gap: 8px; }
        .content-editor {
            width: 100%;
            min-height: 350px;
            padding: 16px;
            background: rgba(0,0,0,0.4);
            border: 1px solid rgba(127,187,230,0.3);
            border-radius: 8px;
            color: #e2e8f0;
            font-family: 'Monaco', 'Menlo', 'Consolas', monospace;
            font-size: 0.85rem;
            line-height: 1.6;
            resize: vertical;
            tab-size: 4;
        }
        .content-editor:focus { outline: none; border-color: #7FBBE6; }
        .content-preview-wrapper {
            background: rgba(0,0,0,0.4);
            border-radius: 8px;
            max-height: 400px;
            overflow: auto;
            font-family: 'Monaco', 'Menlo', 'Consolas', monospace;
            font-size: 0.85rem;
        }
        .content-line {
            display: flex;
            min-height: 1.6em;
            line-height: 1.6em;
        }
        .line-number {
            min-width: 50px;
            padding: 0 12px;
            text-align: right;
            color: #475569;
            background: rgba(0,0,0,0.2);
            user-select: none;
            border-right: 1px solid rgba(255,255,255,0.1);
        }
        .line-content {
            flex: 1;
            padding: 0 12px;
            white-space: pre;
            overflow-x: auto;
        }
        .line-error {
            background: rgba(239, 68, 68, 0.15);
            border-left: 3px solid #F16365;
        }
        .line-error .line-number {
            background: rgba(239, 68, 68, 0.2);
            color: #F16365;
        }
        .line-warning {
            background: rgba(251, 191, 36, 0.15);
            border-left: 3px solid #FBD779;
        }
        .line-warning .line-number {
            background: rgba(251, 191, 36, 0.2);
            color: #FBD779;
        }
        .issue-item {
            cursor: pointer;
            transition: background 0.2s;
        }
        .issue-item:hover {
            background: rgba(255,255,255,0.05);
        }
        .toggle-preview-btn {
            font-size: 0.85rem;
            padding: 6px 12px;
        }
        @keyframes flash {
            0% { background: rgba(16, 185, 129, 0.4); }
            100% { background: transparent; }
        }
        .line-error { animation: none !important; }
        .line-warning { animation: none !important; }

        /* ===== NAVBAR ===== */
        .navbar {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 100;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 16px 40px;
            transition: background 0.3s, box-shadow 0.3s;
        }
        .navbar.scrolled {
            background: rgba(15,15,26,0.92);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            box-shadow: 0 1px 0 rgba(255,255,255,0.06);
        }
        .nav-logo {
            font-size: 1.3rem;
            font-weight: 700;
            color: #e2e8f0;
            text-decoration: none;
        }
        .nav-logo span { color: #7FBBE6; }
        .nav-links { display: flex; align-items: center; gap: 32px; }
        .nav-links a {
            color: #94a3b8;
            text-decoration: none;
            font-size: 0.9rem;
            transition: color 0.2s;
        }
        .nav-links a:hover { color: #e2e8f0; }
        .nav-cta {
            padding: 8px 20px !important;
            background: #F16365 !important;
            color: #fff !important;
            border-radius: 8px !important;
            font-weight: 500 !important;
            font-size: 0.9rem !important;
            transition: background 0.2s !important;
        }
        .nav-cta:hover { background: #D94E50 !important; }

        /* ===== HERO ===== */
        .hero {
            text-align: center;
            padding: 140px 20px 80px;
            position: relative;
            overflow: hidden;
        }
        .hero::before {
            content: '';
            position: absolute;
            top: 30%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 600px;
            height: 600px;
            background: radial-gradient(circle, rgba(127,187,230,0.08) 0%, transparent 70%);
            pointer-events: none;
        }
        .hero-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 16px;
            background: rgba(127,187,230,0.1);
            border: 1px solid rgba(127,187,230,0.25);
            border-radius: 50px;
            color: #7FBBE6;
            font-size: 0.85rem;
            font-weight: 500;
            margin-bottom: 24px;
        }
        .hero h1 {
            font-size: 3.5rem;
            font-weight: 800;
            line-height: 1.15;
            letter-spacing: -0.02em;
            color: #f1f5f9;
            max-width: 700px;
            margin: 0 auto 20px;
        }
        .hero h1 span { color: #7FBBE6; }
        .hero-sub {
            font-size: 1.15rem;
            color: #94a3b8;
            max-width: 560px;
            margin: 0 auto 36px;
            line-height: 1.7;
        }
        .hero-cta {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 14px 32px;
            background: #F16365;
            color: #fff;
            border: none;
            border-radius: 10px;
            font-size: 1.05rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            text-decoration: none;
        }
        .hero-cta:hover { background: #D94E50; transform: translateY(-2px); box-shadow: 0 8px 30px rgba(241,99,101,0.25); }

        /* ===== SECTION UTILITIES ===== */
        .section { padding: 80px 20px; max-width: 1200px; margin: 0 auto; }
        .section-divider {
            height: 1px;
            max-width: 1200px;
            margin: 0 auto;
            background: linear-gradient(90deg, transparent 0%, rgba(127,187,230,0.25) 50%, transparent 100%);
        }
        .section-label {
            text-transform: uppercase;
            letter-spacing: 0.1em;
            font-size: 0.8rem;
            font-weight: 600;
            color: #7FBBE6;
            margin-bottom: 12px;
        }
        .section-heading {
            font-size: 2.2rem;
            font-weight: 700;
            color: #f1f5f9;
            margin-bottom: 16px;
            letter-spacing: -0.01em;
        }
        .section-desc {
            font-size: 1.05rem;
            color: #94a3b8;
            max-width: 600px;
            line-height: 1.7;
            margin-bottom: 48px;
        }
        .section-center { text-align: center; }
        .section-center .section-desc { margin-left: auto; margin-right: auto; }

        /* ===== TOOL SECTION LABEL ===== */
        .tool-label {
            text-align: center;
            margin-bottom: 24px;
            padding-top: 20px;
        }
        .tool-label span {
            display: inline-block;
            padding: 6px 16px;
            background: rgba(127,187,230,0.1);
            border: 1px solid rgba(127,187,230,0.2);
            border-radius: 50px;
            color: #7FBBE6;
            font-size: 0.85rem;
            font-weight: 500;
        }

        /* ===== ABOUT SECTION ===== */
        .about-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 48px;
            align-items: center;
        }
        .about-text { line-height: 1.8; color: #cbd5e1; font-size: 1.02rem; }
        .about-text p { margin-bottom: 16px; }
        .about-text a { color: #7FBBE6; text-decoration: none; }
        .about-text a:hover { text-decoration: underline; }
        .about-code {
            background: rgba(0,0,0,0.4);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            padding: 24px;
            font-family: 'Monaco', 'Menlo', 'Consolas', monospace;
            font-size: 0.88rem;
            line-height: 1.8;
            overflow-x: auto;
        }
        .about-code .code-comment { color: #475569; }
        .about-code .code-h1 { color: #7FBBE6; font-weight: 600; }
        .about-code .code-h2 { color: #7FBBE6; }
        .about-code .code-quote { color: #a78bfa; }
        .about-code .code-link { color: #FBD779; }
        .about-code .code-url { color: #64748b; }

        /* ===== BENEFITS GRID ===== */
        .benefits-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 24px;
        }
        .benefit-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 32px 28px;
            transition: all 0.3s;
        }
        .benefit-card:hover {
            border-color: rgba(127,187,230,0.3);
            transform: translateY(-3px);
            box-shadow: 0 12px 40px rgba(0,0,0,0.2);
        }
        .benefit-icon {
            width: 48px;
            height: 48px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 12px;
            font-size: 1.4rem;
            margin-bottom: 20px;
        }
        .benefit-icon-green { background: rgba(127,187,230,0.15); }
        .benefit-icon-blue { background: rgba(56,189,248,0.15); }
        .benefit-icon-purple { background: rgba(167,139,250,0.15); }
        .benefit-card h3 {
            font-size: 1.1rem;
            color: #f1f5f9;
            margin-bottom: 10px;
            font-weight: 600;
        }
        .benefit-card p {
            color: #94a3b8;
            font-size: 0.92rem;
            line-height: 1.6;
        }

        /* ===== FEATURES GRID ===== */
        .features-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
        }
        .feature-card {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 12px;
            padding: 28px 24px;
            transition: all 0.3s;
        }
        .feature-card:hover {
            border-color: rgba(127,187,230,0.25);
            background: rgba(255,255,255,0.04);
        }
        .feature-icon { font-size: 1.5rem; margin-bottom: 14px; }
        .feature-card h3 {
            font-size: 1rem;
            color: #e2e8f0;
            margin-bottom: 8px;
            font-weight: 600;
        }
        .feature-card p {
            color: #64748b;
            font-size: 0.88rem;
            line-height: 1.5;
        }

        /* ===== HOW IT WORKS ===== */
        .steps-row {
            display: flex;
            gap: 24px;
            align-items: flex-start;
            justify-content: center;
        }
        .step-item {
            flex: 1;
            max-width: 300px;
            text-align: center;
            position: relative;
        }
        .step-number {
            width: 56px;
            height: 56px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 20px;
            border-radius: 50%;
            background: rgba(127,187,230,0.15);
            border: 2px solid rgba(127,187,230,0.4);
            color: #7FBBE6;
            font-size: 1.3rem;
            font-weight: 700;
        }
        .step-item h3 {
            font-size: 1.05rem;
            color: #e2e8f0;
            margin-bottom: 8px;
            font-weight: 600;
        }
        .step-item p { color: #94a3b8; font-size: 0.9rem; line-height: 1.6; }
        .step-connector {
            flex: 0 0 60px;
            display: flex;
            align-items: center;
            justify-content: center;
            padding-top: 20px;
            color: rgba(127,187,230,0.4);
            font-size: 1.5rem;
        }

        /* ===== FAQ ===== */
        .faq-list { max-width: 720px; margin: 0 auto; }
        .faq-item {
            border-bottom: 1px solid rgba(255,255,255,0.06);
        }
        .faq-item summary {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 20px 0;
            cursor: pointer;
            font-size: 1.02rem;
            color: #e2e8f0;
            font-weight: 500;
            list-style: none;
        }
        .faq-item summary::-webkit-details-marker { display: none; }
        .faq-item summary::after {
            content: '+';
            font-size: 1.4rem;
            color: #64748b;
            transition: transform 0.2s;
            flex-shrink: 0;
            margin-left: 16px;
        }
        .faq-item[open] summary::after { content: '-'; }
        .faq-item .faq-answer {
            padding: 0 0 20px;
            color: #94a3b8;
            font-size: 0.95rem;
            line-height: 1.7;
        }
        .faq-answer a { color: #7FBBE6; text-decoration: none; }
        .faq-answer a:hover { text-decoration: underline; }

        /* ===== CTA BAND ===== */
        .cta-band {
            text-align: center;
            padding: 80px 20px;
            position: relative;
        }
        .cta-band::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 500px;
            height: 300px;
            background: radial-gradient(circle, rgba(127,187,230,0.06) 0%, transparent 70%);
            pointer-events: none;
        }
        .cta-band h2 {
            font-size: 2rem;
            color: #f1f5f9;
            margin-bottom: 16px;
            font-weight: 700;
        }
        .cta-band p {
            color: #94a3b8;
            margin-bottom: 32px;
            font-size: 1.05rem;
        }

        /* ===== FOOTER ===== */
        .footer-full {
            text-align: center;
            padding: 40px 20px;
            border-top: 1px solid rgba(255,255,255,0.06);
            color: #475569;
            font-size: 0.85rem;
        }
        .footer-links { display: flex; justify-content: center; gap: 32px; margin-bottom: 16px; }
        .footer-links a { color: #64748b; text-decoration: none; font-size: 0.88rem; }
        .footer-links a:hover { color: #7FBBE6; }

        /* ===== SCROLL REVEAL ===== */
        .reveal {
            opacity: 0;
            transform: translateY(30px);
            transition: opacity 0.6s ease, transform 0.6s ease;
        }
        .reveal.visible {
            opacity: 1;
            transform: translateY(0);
        }

        /* ===== RESPONSIVE ===== */
        @media (max-width: 1024px) {
            .hero h1 { font-size: 2.8rem; }
            .features-grid { grid-template-columns: repeat(2, 1fr); }
            .section { padding: 60px 20px; }
        }
        @media (max-width: 768px) {
            .navbar { padding: 14px 20px; }
            .nav-links { gap: 16px; }
            .nav-links .nav-link-text { display: none; }
            .hero { padding: 120px 20px 60px; }
            .hero h1 { font-size: 2rem; }
            .hero-sub { font-size: 1rem; }
            .about-grid { grid-template-columns: 1fr; gap: 32px; }
            .benefits-grid { grid-template-columns: 1fr; }
            .features-grid { grid-template-columns: 1fr; }
            .steps-row { flex-direction: column; align-items: center; }
            .step-connector { transform: rotate(90deg); padding-top: 0; }
            .section-heading { font-size: 1.7rem; }
        }
    </style>
</head>
<body>
    <!-- Navbar -->
    <nav class="navbar" id="navbar">
        <a href="#" class="nav-logo">LLMs<span>.txt</span> Validator</a>
        <div class="nav-links">
            <a href="#detector" class="nav-link-text">Detector</a>
            <a href="#validator" class="nav-link-text">Validator</a>
            <a href="#about" class="nav-link-text">About</a>
            <a href="#features" class="nav-link-text">Features</a>
            <a href="#faq" class="nav-link-text">FAQ</a>
            <a href="#detector" class="nav-cta">Detect Now</a>
        </div>
    </nav>

    <!-- Hero -->
    <section class="hero">
        <div class="hero-badge">Open Standard &middot; Free Tool</div>
        <h1>Validate Your <span>LLMs.txt</span> Files Instantly</h1>
        <p class="hero-sub">Check compliance with the llms.txt specification. Catch errors, verify structure, and ensure your site is ready for AI agents and language models.</p>
        <a href="#detector" class="hero-cta">Detect llms.txt &#8595;</a>
    </section>

    <div class="section-divider"></div>

    <!-- Detector Tool -->
    <section id="detector">
    <div class="container">
        <div class="tool-label"><span>Free Detector</span></div>
        <div class="section-heading">Does this site have llms.txt?</div>
        <p class="detector-sub">Enter a URL and we'll check the root for <code>llms.txt</code>, <code>llms-ctx.txt</code>, and <code>llms-full.txt</code>.</p>

        <div class="input-section">
            <div class="input-group">
                <label>Website URL</label>
                <div class="url-input-wrapper">
                    <input type="text" class="url-input" id="detectUrlField" placeholder="https://example.com">
                    <button class="btn" id="detectBtn">Detect</button>
                </div>
                <p style="margin-top: 8px;"><span class="example-link" onclick="document.getElementById('detectUrlField').value='https://anthropic.com'">Try: anthropic.com</span></p>
            </div>
        </div>

        <div class="loading" id="detectLoading">
            <div class="spinner"></div>
            <p>Checking the site...</p>
        </div>

        <div class="detector-results" id="detectorResults" style="display:none;">
            <div class="detector-summary" id="detectorSummary"></div>
            <div class="detector-grid" id="detectorGrid"></div>
        </div>

        <div class="loading" id="generateLoading">
            <div class="spinner"></div>
            <p>Reading the site and generating llms.txt with Claude Haiku... this can take 10–20 seconds.</p>
        </div>

        <div class="generator-results" id="generatorResults" style="display:none;">
            <div class="detector-summary" id="generatorSummary"></div>
            <div class="generator-output">
                <textarea id="generatedContent" class="content-editor" spellcheck="false" rows="20"></textarea>
            </div>
            <div class="generator-actions">
                <button class="btn" id="copyGeneratedBtn">Copy</button>
                <button class="btn" id="downloadGeneratedBtn">Download llms.txt</button>
                <button class="btn btn-secondary" id="validateGeneratedBtn">Validate this</button>
            </div>
        </div>
    </div>
    </section>

    <div class="section-divider"></div>

    <!-- Validator Tool -->
    <section id="validator">
    <div class="container">
        <div class="tool-label"><span>Try It Now</span></div>

        <div class="input-section">
            <div class="tabs">
                <div class="tab active" data-tab="url">Fetch from URL</div>
                <div class="tab" data-tab="paste">Paste Content</div>
                <div class="tab" data-tab="upload">Upload File</div>
            </div>

            <div class="file-type-selector">
                <button class="file-type-btn active" data-type="llms.txt">llms.txt</button>
                <button class="file-type-btn" data-type="llms-ctx.txt">llms-ctx.txt</button>
                <button class="file-type-btn" data-type="llms-full.txt">llms-full.txt</button>
            </div>

            <div id="urlInput" class="input-group">
                <label>Enter website URL</label>
                <div class="url-input-wrapper">
                    <input type="text" class="url-input" id="urlField" placeholder="https://example.com">
                    <button class="btn" id="fetchBtn">Validate</button>
                </div>
                <p style="margin-top: 8px;"><span class="example-link" onclick="document.getElementById('urlField').value='https://anthropic.com'">Try: anthropic.com</span></p>
            </div>

            <div id="pasteInput" class="input-group" style="display:none;">
                <label>Paste your llms.txt content</label>
                <textarea id="contentField" placeholder="# My Project

> A brief description of my project for LLMs.

## Documentation
- [Getting Started](/docs/start): Quick start guide
- [API Reference](/docs/api): Full API documentation

## Optional
- [Examples](/examples): Code examples"></textarea>
                <button class="btn" id="validateBtn" style="margin-top: 12px;">Validate</button>
            </div>

            <div id="uploadInput" class="input-group" style="display:none;">
                <label>Upload a .txt or .md file</label>
                <div class="upload-area" id="uploadArea">
                    <input type="file" id="fileField" accept=".txt,.md,text/plain,text/markdown" style="display:none;">
                    <div class="upload-placeholder" id="uploadPlaceholder">
                        <span style="font-size: 2rem;">&#128196;</span>
                        <p>Drop a file here or click to browse</p>
                        <p style="font-size: 0.8rem; color: #475569;">Supports .txt and .md files</p>
                    </div>
                    <div class="upload-file-info" id="uploadFileInfo" style="display:none;">
                        <span id="uploadFileName"></span>
                        <span class="example-link" id="uploadClear">Remove</span>
                    </div>
                </div>
                <button class="btn" id="uploadBtn" style="margin-top: 12px;">Validate</button>
            </div>
        </div>

        <div class="loading" id="loading">
            <div class="spinner"></div>
            <p>Validating...</p>
        </div>

        <div class="results" id="results">
            <div class="result-header">
                <span class="status-badge" id="statusBadge">Valid</span>
                <span id="fileTypeLabel">llms.txt</span>
                <span style="color: #64748b;" id="titleLabel"></span>
            </div>

            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value" id="statChars">0</div>
                    <div class="stat-label">Characters</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="statTokens">0</div>
                    <div class="stat-label">Est. Tokens</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="statSize">0 KB</div>
                    <div class="stat-label">File Size</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="statLinks">0</div>
                    <div class="stat-label">Links</div>
                </div>
                <div class="stat-card" id="statEncodingCard">
                    <div class="stat-value" id="statEncoding" style="font-size: 1.2rem;">-</div>
                    <div class="stat-label">Encoding</div>
                </div>
            </div>

            <div class="issues-section" id="encodingSection" style="display:none;">
                <div class="issues-title" style="color: #7FBBE6;">
                    Encoding Details
                </div>
                <p class="encoding-intro">Encoding determines how characters are stored as bytes. For llms.txt files, <strong>UTF-8</strong> is the recommended standard — it supports all languages and special characters while being universally compatible with LLM consumers.</p>
                <div id="encodingDetails"></div>
            </div>

            <div class="content-preview-section" id="contentPreviewSection">
                <div class="content-preview-title">
                    <span>Content Preview</span>
                    <div class="preview-actions">
                        <button class="btn btn-secondary toggle-preview-btn" id="editBtn">Edit</button>
                        <button class="btn btn-secondary toggle-preview-btn" id="togglePreviewBtn">Hide</button>
                        <button class="btn toggle-preview-btn" id="downloadBtn" style="background: #3b82f6;">&#8681; Download UTF-8</button>
                    </div>
                </div>
                <div class="content-preview-wrapper" id="contentPreviewWrapper">
                    <div id="contentPreview"></div>
                </div>
                <textarea id="contentEditor" class="content-editor" style="display:none;"></textarea>
            </div>

            <div class="issues-section" id="errorsSection" style="display:none;">
                <div class="issues-title" style="color: #F16365;">
                    Errors <span class="count" id="errorCount">0</span>
                </div>
                <div id="errorsList"></div>
            </div>

            <div class="issues-section" id="warningsSection" style="display:none;">
                <div class="issues-title" style="color: #FBD779;">
                    Warnings <span class="count" id="warningCount">0</span>
                </div>
                <div id="warningsList"></div>
            </div>

            <div class="structure-section">
                <div class="structure-title">Structure</div>
                <div id="structureList"></div>
            </div>
        </div>

    </div>
    </section>

    <div class="section-divider"></div>

    <!-- What is llms.txt -->
    <section id="about" class="section reveal">
        <div class="section-label">About the Standard</div>
        <div class="section-heading">What is llms.txt?</div>
        <div class="about-grid">
            <div class="about-text">
                <p>The <a href="https://llmstxt.org/" target="_blank">llms.txt specification</a> is a proposed standard that helps websites provide structured, LLM-friendly content. Think of it as a robots.txt for AI — a simple markdown file that tells language models what your site is about and where to find key resources.</p>
                <p>The format supports three file types: <strong>llms.txt</strong> for a concise overview, <strong>llms-ctx.txt</strong> for additional context, and <strong>llms-full.txt</strong> for comprehensive documentation. Each follows a clean markdown structure with headers, descriptions, and categorized links.</p>
                <p>As AI agents become more prevalent, having a well-structured llms.txt file ensures your content is discoverable, accessible, and correctly interpreted by language models.</p>
            </div>
            <div class="about-code">
                <div class="code-comment"># A valid llms.txt example</div>
                <br>
                <div class="code-h1"># My Project</div>
                <br>
                <div class="code-quote">&gt; A brief description of the project<br>&gt; for language models to understand.</div>
                <br>
                <div class="code-h2">## Documentation</div>
                <div class="code-link">- [<span style="color:#FBD779;">Getting Started</span>](<span class="code-url">https://example.com/start</span>): Quick start guide</div>
                <div class="code-link">- [<span style="color:#FBD779;">API Reference</span>](<span class="code-url">https://example.com/api</span>): Full API docs</div>
                <br>
                <div class="code-h2">## Optional</div>
                <div class="code-link">- [<span style="color:#FBD779;">Examples</span>](<span class="code-url">https://example.com/examples</span>): Code samples</div>
            </div>
        </div>
    </section>

    <div class="section-divider"></div>

    <!-- Why Validate -->
    <section class="section section-center reveal">
        <div class="section-label">Why It Matters</div>
        <div class="section-heading">Why Validate Your llms.txt?</div>
        <div class="section-desc">A malformed llms.txt means AI agents may misinterpret or ignore your content entirely. Validation ensures your file is spec-compliant and machine-ready.</div>
        <div class="benefits-grid">
            <div class="benefit-card">
                <div class="benefit-icon benefit-icon-green">&#10003;</div>
                <h3>Catch Errors Early</h3>
                <p>Detect missing headers, malformed links, encoding issues, and structural problems before they reach production. Line-by-line feedback shows you exactly what to fix.</p>
            </div>
            <div class="benefit-card">
                <div class="benefit-icon benefit-icon-blue">&#9881;</div>
                <h3>Ensure Spec Compliance</h3>
                <p>Validate against the official llms.txt specification. Check required fields, recommended sections, link formats, and file size limits automatically.</p>
            </div>
            <div class="benefit-card">
                <div class="benefit-icon benefit-icon-purple">&#9733;</div>
                <h3>Improve AI Discoverability</h3>
                <p>A valid, well-structured llms.txt makes your site more accessible to AI crawlers and language models, ensuring your content is properly indexed and understood.</p>
            </div>
        </div>
    </section>

    <div class="section-divider"></div>

    <!-- Features -->
    <section id="features" class="section section-center reveal">
        <div class="section-label">Capabilities</div>
        <div class="section-heading">Everything You Need</div>
        <div class="section-desc">A comprehensive validation toolkit built specifically for the llms.txt ecosystem.</div>
        <div class="features-grid">
            <div class="feature-card">
                <div class="feature-icon">&#127760;</div>
                <h3>URL Fetching</h3>
                <p>Validate any live site by entering its URL. Handles redirects, encoding detection, and TLS-protected sites.</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#128196;</div>
                <h3>Multi-Format Support</h3>
                <p>Validate llms.txt, llms-ctx.txt, and llms-full.txt files with format-specific rules and size limits.</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#128300;</div>
                <h3>Encoding Detection</h3>
                <p>Automatic character encoding analysis with BOM detection, UTF-8 validation, and server header comparison.</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#128202;</div>
                <h3>Structure Analysis</h3>
                <p>Visual breakdown of your file structure: H1 titles, blockquotes, H2 sections, and link inventory.</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#129518;</div>
                <h3>Token Estimation</h3>
                <p>Approximate LLM token counts so you can gauge context window usage before feeding content to a model.</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#9998;</div>
                <h3>Inline Editing</h3>
                <p>Fix issues directly in the browser, then download the corrected file as clean UTF-8 text.</p>
            </div>
        </div>
    </section>

    <div class="section-divider"></div>

    <!-- How It Works -->
    <section class="section section-center reveal">
        <div class="section-label">Simple Process</div>
        <div class="section-heading">How It Works</div>
        <div class="section-desc">Three steps to a validated llms.txt file.</div>
        <div class="steps-row">
            <div class="step-item">
                <div class="step-number">1</div>
                <h3>Provide Your File</h3>
                <p>Enter a URL, paste content directly, or upload a .txt/.md file from your computer.</p>
            </div>
            <div class="step-connector">&#8594;</div>
            <div class="step-item">
                <div class="step-number">2</div>
                <h3>Instant Validation</h3>
                <p>We check structure, links, encoding, and compliance against the llms.txt specification.</p>
            </div>
            <div class="step-connector">&#8594;</div>
            <div class="step-item">
                <div class="step-number">3</div>
                <h3>Review &amp; Fix</h3>
                <p>Get line-by-line feedback, fix issues inline, and download the corrected file.</p>
            </div>
        </div>
    </section>

    <div class="section-divider"></div>

    <!-- FAQ -->
    <section id="faq" class="section section-center reveal">
        <div class="section-label">Common Questions</div>
        <div class="section-heading">FAQ</div>
        <div class="section-desc">Everything you need to know about llms.txt and this validator.</div>
        <div class="faq-list">
            <details class="faq-item">
                <summary>What is the llms.txt specification?</summary>
                <div class="faq-answer">The llms.txt specification is a proposed standard for providing LLM-friendly content on websites. It defines a simple markdown format that helps AI agents and language models understand what a site offers and where to find key resources. Learn more at <a href="https://llmstxt.org/" target="_blank">llmstxt.org</a>.</div>
            </details>
            <details class="faq-item">
                <summary>What file types can I validate?</summary>
                <div class="faq-answer">You can validate three file types: <strong>llms.txt</strong> (concise site overview, max 500KB), <strong>llms-ctx.txt</strong> (additional context for AI), and <strong>llms-full.txt</strong> (comprehensive documentation with no size limit).</div>
            </details>
            <details class="faq-item">
                <summary>What does the validator check?</summary>
                <div class="faq-answer">The validator checks for required H1 headers, recommended blockquote summaries, proper H2 section structure, valid link formatting, URL correctness, duplicate links, file size limits, and character encoding. Each issue is reported with a specific line number so you can find and fix it quickly.</div>
            </details>
            <details class="faq-item">
                <summary>How are tokens estimated?</summary>
                <div class="faq-answer">Token estimates use an approximation of ~1.3 tokens per word plus ~0.5 per punctuation mark. This is a rough guide — actual tokenization varies by model. It helps you gauge whether your content fits within typical LLM context windows.</div>
            </details>
            <details class="faq-item">
                <summary>Is my content stored or shared?</summary>
                <div class="faq-answer">No. All validation happens in a single request. Content you paste or upload is processed server-side for validation and returned to your browser. Nothing is stored, logged, or shared with third parties.</div>
            </details>
            <details class="faq-item">
                <summary>Where can I learn more about the spec?</summary>
                <div class="faq-answer">Visit <a href="https://llmstxt.org/" target="_blank">llmstxt.org</a> for the full specification, examples, and community resources. The spec is open and community-driven.</div>
            </details>
        </div>
    </section>

    <div class="section-divider"></div>

    <!-- CTA Band -->
    <section class="cta-band reveal">
        <h2>Ready to Validate?</h2>
        <p>Check your llms.txt file in seconds. Free, instant, no sign-up required.</p>
        <a href="#validator" class="hero-cta">Go to Validator &#8593;</a>
    </section>

    <!-- Footer -->
    <footer class="footer-full">
        <div class="footer-links">
            <a href="https://llmstxt.org/" target="_blank">llms.txt Specification</a>
            <a href="#validator">Validator</a>
            <a href="#about">About</a>
            <a href="#faq">FAQ</a>
            <a href="/sitemap">Sitemap</a>
            <a href="/llms.txt">llms.txt</a>
        </div>
        <p>A free tool for the llms.txt ecosystem. Built for the AI-ready web.</p>
    </footer>

    <script>
        let currentTab = 'url';
        let currentFileType = 'llms.txt';
        let currentContent = '';
        let errorLines = new Set();
        let warningLines = new Set();
        let uploadedFileBase64 = null;

        // Tab switching
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                currentTab = tab.dataset.tab;

                document.getElementById('urlInput').style.display = currentTab === 'url' ? 'block' : 'none';
                document.getElementById('pasteInput').style.display = currentTab === 'paste' ? 'block' : 'none';
                document.getElementById('uploadInput').style.display = currentTab === 'upload' ? 'block' : 'none';
            });
        });

        // File type switching
        document.querySelectorAll('.file-type-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.file-type-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentFileType = btn.dataset.type;
            });
        });

        // Fetch button
        document.getElementById('fetchBtn').addEventListener('click', async () => {
            const url = document.getElementById('urlField').value.trim();
            if (!url) return alert('Please enter a URL');

            await validate({ url, file_type: currentFileType });
        });

        // Detector
        const detectBtn = document.getElementById('detectBtn');
        const detectUrlField = document.getElementById('detectUrlField');
        detectBtn.addEventListener('click', () => runDetect());
        detectUrlField.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') runDetect();
        });

        async function runDetect() {
            const url = detectUrlField.value.trim();
            if (!url) return alert('Please enter a URL');

            const loading = document.getElementById('detectLoading');
            const resultsEl = document.getElementById('detectorResults');
            loading.classList.add('show');
            resultsEl.style.display = 'none';

            try {
                const response = await fetch('/detect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url }),
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Detection failed');
                renderDetection(data);
            } catch (err) {
                alert('Detection error: ' + err.message);
            } finally {
                loading.classList.remove('show');
            }
        }

        function formatBytes(bytes) {
            if (bytes == null) return '—';
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
        }

        function escapeHtml(s) {
            return String(s).replace(/[&<>"']/g, (c) => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[c]));
        }

        function renderDetection(data) {
            const { url, results, summary } = data;
            const summaryEl = document.getElementById('detectorSummary');
            const gridEl = document.getElementById('detectorGrid');

            const countCls = summary.found_count === 0 ? 'summary-count none' : 'summary-count';
            let note = '';
            if (summary.found_count === 0) {
                note = '<div class="summary-note">No llms.txt files found at this site &mdash; it is not yet AI-ready.</div>';
            } else if (summary.found_count < summary.total) {
                note = '<div class="summary-note">Some files are missing. Validate what you have and consider adding the rest.</div>';
            } else {
                note = '<div class="summary-note">Fully configured. Validate each file to confirm it follows the spec.</div>';
            }

            summaryEl.innerHTML = `
                <div><span class="${countCls}">${summary.found_count} / ${summary.total}</span> LLM files detected at <code>${escapeHtml(url)}</code></div>
                ${note}
            `;

            gridEl.innerHTML = results.map(r => {
                const cls = r.found ? 'found' : 'missing';
                const badgeCls = r.found ? 'status-valid' : 'status-invalid';
                const badgeText = r.found ? 'Found' : 'Missing';
                const fileUrl = escapeHtml(r.url);
                const meta = r.found
                    ? `<div>HTTP ${r.status} &middot; ${formatBytes(r.size)}${r.content_type ? ' &middot; ' + escapeHtml(r.content_type) : ''}</div>
                       <div style="margin-top:4px;"><a href="${fileUrl}" target="_blank" rel="noopener">${fileUrl}</a></div>`
                    : `<div>${r.status ? 'HTTP ' + r.status : 'Not reachable'}</div>
                       <div class="meta-row" style="margin-top:4px;">${fileUrl}</div>`;
                let actionsHtml = '';
                if (r.found) {
                    actionsHtml = `<button class="btn btn-secondary" data-validate="${escapeHtml(r.file)}" data-site="${escapeHtml(url)}">Validate this file</button>`;
                } else if (r.file === 'llms.txt') {
                    actionsHtml = `<button class="btn" data-generate="${escapeHtml(url)}">Generate with AI</button>`;
                } else if (r.file === 'llms-ctx.txt') {
                    actionsHtml = `<button class="btn" data-checkout="llms-ctx.txt" data-url="${escapeHtml(url)}">Generate ctx — $2</button>`;
                } else if (r.file === 'llms-full.txt') {
                    actionsHtml = `<button class="btn" data-checkout="llms-full.txt" data-url="${escapeHtml(url)}">Generate full — $4.99</button>`;
                }
                const actions = actionsHtml ? `<div class="detector-card-actions">${actionsHtml}</div>` : '';
                return `
                    <div class="detector-card ${cls}">
                        <div class="detector-card-header">
                            <span class="detector-card-name">${escapeHtml(r.file)}</span>
                            <span class="status-badge ${badgeCls}">${badgeText}</span>
                        </div>
                        <div class="detector-card-meta">${meta}</div>
                        ${actions}
                    </div>
                `;
            }).join('');

            gridEl.querySelectorAll('button[data-validate]').forEach(btn => {
                btn.addEventListener('click', () => {
                    const fileType = btn.dataset.validate;
                    const siteUrl = btn.dataset.site;
                    document.querySelector('.tab[data-tab="url"]').click();
                    setFileType(fileType);
                    document.getElementById('urlField').value = siteUrl;
                    document.getElementById('validator').scrollIntoView({ behavior: 'smooth' });
                    setTimeout(() => document.getElementById('fetchBtn').click(), 450);
                });
            });

            gridEl.querySelectorAll('button[data-generate]').forEach(btn => {
                btn.addEventListener('click', () => runGenerate(btn.dataset.generate));
            });

            gridEl.querySelectorAll('button[data-checkout]').forEach(btn => {
                btn.addEventListener('click', () => runCheckout(btn));
            });

            document.getElementById('detectorResults').style.display = 'block';
        }

        async function runCheckout(btn) {
            const fileType = btn.dataset.checkout;
            const url = btn.dataset.url;
            const originalText = btn.textContent;
            btn.disabled = true;
            btn.textContent = 'Redirecting to Stripe...';
            try {
                const response = await fetch('/checkout', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, file_type: fileType }),
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Checkout failed');
                window.location.href = data.checkout_url;
            } catch (err) {
                alert('Checkout error: ' + err.message);
                btn.disabled = false;
                btn.textContent = originalText;
            }
        }

        async function runPaidGeneration(sessionId) {
            document.getElementById('detector').scrollIntoView({ behavior: 'smooth' });
            const loading = document.getElementById('generateLoading');
            const resultsEl = document.getElementById('generatorResults');
            loading.classList.add('show');
            resultsEl.style.display = 'none';
            try {
                const response = await fetch('/generate-paid?session_id=' + encodeURIComponent(sessionId));
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Generation failed');
                renderGenerated(data);
            } catch (err) {
                alert('Post-payment generation error: ' + err.message);
            } finally {
                loading.classList.remove('show');
            }
        }

        // Handle post-Stripe redirect: ?paid_session_id=...
        (function handlePostPaymentRedirect() {
            const params = new URLSearchParams(window.location.search);
            const paidId = params.get('paid_session_id');
            if (paidId) {
                window.history.replaceState({}, document.title, window.location.pathname);
                runPaidGeneration(paidId);
            } else if (params.get('checkout_canceled')) {
                window.history.replaceState({}, document.title, window.location.pathname);
            }
        })();

        async function runGenerate(siteUrl) {
            const loading = document.getElementById('generateLoading');
            const resultsEl = document.getElementById('generatorResults');
            loading.classList.add('show');
            resultsEl.style.display = 'none';
            try {
                const response = await fetch('/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: siteUrl }),
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Generation failed');
                renderGenerated(data);
            } catch (err) {
                alert('Generation error: ' + err.message);
            } finally {
                loading.classList.remove('show');
            }
        }

        function renderGenerated(data) {
            const { url, content, pages_analyzed, model, usage } = data;
            const fileType = data.file_type || 'llms.txt';
            document.getElementById('generatorSummary').innerHTML = `
                <div>Generated <code>${escapeHtml(fileType)}</code> for <code>${escapeHtml(url)}</code></div>
                <div class="summary-note">${pages_analyzed} pages analyzed &middot; model ${escapeHtml(model)} &middot; ${usage.input_tokens} in / ${usage.output_tokens} out tokens</div>
            `;
            const editor = document.getElementById('generatedContent');
            editor.value = content;
            editor.dataset.fileType = fileType;
            document.getElementById('downloadGeneratedBtn').textContent = 'Download ' + fileType;
            document.getElementById('generatorResults').style.display = 'block';
            document.getElementById('generatorResults').scrollIntoView({ behavior: 'smooth' });
        }

        document.getElementById('copyGeneratedBtn').addEventListener('click', async () => {
            const text = document.getElementById('generatedContent').value;
            try {
                await navigator.clipboard.writeText(text);
                const btn = document.getElementById('copyGeneratedBtn');
                const orig = btn.textContent;
                btn.textContent = 'Copied!';
                setTimeout(() => (btn.textContent = orig), 1500);
            } catch (err) {
                alert('Could not copy: ' + err.message);
            }
        });

        document.getElementById('downloadGeneratedBtn').addEventListener('click', () => {
            const editor = document.getElementById('generatedContent');
            const fileType = editor.dataset.fileType || 'llms.txt';
            const blob = new Blob([editor.value], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = fileType;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        });

        document.getElementById('validateGeneratedBtn').addEventListener('click', () => {
            const editor = document.getElementById('generatedContent');
            const fileType = editor.dataset.fileType || 'llms.txt';
            document.querySelector('.tab[data-tab="paste"]').click();
            setFileType(fileType);
            document.getElementById('contentField').value = editor.value;
            document.getElementById('validator').scrollIntoView({ behavior: 'smooth' });
            setTimeout(() => document.getElementById('validateBtn').click(), 300);
        });

        // Validate button (paste)
        document.getElementById('validateBtn').addEventListener('click', async () => {
            const content = document.getElementById('contentField').value;
            if (!content.trim()) return alert('Please paste some content');

            await validate({ content, file_type: currentFileType });
        });

        // Upload file handling
        const uploadArea = document.getElementById('uploadArea');
        const fileField = document.getElementById('fileField');
        const uploadPlaceholder = document.getElementById('uploadPlaceholder');
        const uploadFileInfo = document.getElementById('uploadFileInfo');
        const uploadFileName = document.getElementById('uploadFileName');

        uploadArea.addEventListener('click', () => fileField.click());
        uploadArea.addEventListener('dragover', (e) => { e.preventDefault(); uploadArea.classList.add('dragover'); });
        uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
        });
        fileField.addEventListener('change', () => { if (fileField.files.length) handleFile(fileField.files[0]); });
        document.getElementById('uploadClear').addEventListener('click', (e) => {
            e.stopPropagation();
            uploadedFileBase64 = null;
            fileField.value = '';
            uploadPlaceholder.style.display = 'block';
            uploadFileInfo.style.display = 'none';
        });

        function setFileType(type) {
            currentFileType = type;
            document.querySelectorAll('.file-type-btn').forEach(b => {
                b.classList.toggle('active', b.dataset.type === type);
            });
        }

        function detectFileType(filename) {
            const name = filename.toLowerCase();
            if (name.includes('full')) return 'llms-full.txt';
            if (name.includes('ctx')) return 'llms-ctx.txt';
            return 'llms.txt';
        }

        function handleFile(file) {
            const reader = new FileReader();
            reader.onload = () => {
                uploadedFileBase64 = btoa(String.fromCharCode(...new Uint8Array(reader.result)));
                uploadFileName.textContent = file.name + ' (' + (file.size / 1024).toFixed(1) + ' KB)';
                uploadPlaceholder.style.display = 'none';
                uploadFileInfo.style.display = 'flex';

                // Auto-detect file type from filename
                setFileType(detectFileType(file.name));
            };
            reader.readAsArrayBuffer(file);
        }

        document.getElementById('uploadBtn').addEventListener('click', async () => {
            if (!uploadedFileBase64) return alert('Please select a file first');
            await validate({ file_base64: uploadedFileBase64, file_type: currentFileType });
        });

        // Edit mode toggle
        let editMode = false;
        const editor = document.getElementById('contentEditor');
        const previewWrapper = document.getElementById('contentPreviewWrapper');
        const editBtn = document.getElementById('editBtn');

        editBtn.addEventListener('click', () => {
            editMode = !editMode;
            if (editMode) {
                editor.value = currentContent;
                editor.style.display = 'block';
                previewWrapper.style.display = 'none';
                editBtn.textContent = 'Preview';
                editBtn.style.background = '#F16365';
                editBtn.style.color = '#fff';
            } else {
                currentContent = editor.value;
                displayContentPreview(currentContent, [], []);
                editor.style.display = 'none';
                previewWrapper.style.display = 'block';
                editBtn.textContent = 'Edit';
                editBtn.style.background = '';
                editBtn.style.color = '';
            }
        });

        // Download as UTF-8 (always available)
        document.getElementById('downloadBtn').addEventListener('click', () => {
            if (!currentContent) return;
            // Sync from editor if in edit mode
            if (editMode) currentContent = editor.value;
            const blob = new Blob([currentContent], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = currentFileType;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        });

        async function validate(data) {
            document.getElementById('loading').classList.add('show');
            document.getElementById('results').classList.remove('show');

            try {
                const response = await fetch('/validate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (!response.ok) {
                    throw new Error(result.detail || 'Validation failed');
                }

                displayResults(result);
            } catch (error) {
                alert('Error: ' + error.message);
            } finally {
                document.getElementById('loading').classList.remove('show');
            }
        }

        // Toggle preview button
        document.getElementById('togglePreviewBtn').addEventListener('click', () => {
            const wrapper = document.getElementById('contentPreviewWrapper');
            const btn = document.getElementById('togglePreviewBtn');
            if (wrapper.style.display === 'none') {
                wrapper.style.display = 'block';
                btn.textContent = 'Hide';
            } else {
                wrapper.style.display = 'none';
                btn.textContent = 'Show';
            }
        });

        function scrollToLine(lineNumber) {
            const lineElement = document.getElementById('line-' + lineNumber);
            if (lineElement) {
                const wrapper = document.getElementById('contentPreviewWrapper');
                wrapper.style.display = 'block';
                document.getElementById('togglePreviewBtn').textContent = 'Hide';
                lineElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                lineElement.style.animation = 'flash 0.5s ease-out 2';
            }
        }

        function displayContentPreview(content, errors, warnings) {
            currentContent = content;
            errorLines = new Set(errors.filter(e => e.line > 0).map(e => e.line));
            warningLines = new Set(warnings.filter(w => w.line > 0).map(w => w.line));

            const lines = content.split('\\n');
            let html = '';

            lines.forEach((line, index) => {
                const lineNum = index + 1;
                let lineClass = 'content-line';
                if (errorLines.has(lineNum)) {
                    lineClass += ' line-error';
                } else if (warningLines.has(lineNum)) {
                    lineClass += ' line-warning';
                }

                const escapedLine = line
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;');

                html += '<div class="' + lineClass + '" id="line-' + lineNum + '">' +
                    '<span class="line-number">' + lineNum + '</span>' +
                    '<span class="line-content">' + (escapedLine || ' ') + '</span>' +
                    '</div>';
            });

            document.getElementById('contentPreview').innerHTML = html;
        }

        function displayResults(data) {
            // Reset edit mode
            editMode = false;
            editor.style.display = 'none';
            previewWrapper.style.display = 'block';
            editBtn.textContent = 'Edit';
            editBtn.style.background = '';
            editBtn.style.color = '';

            // Display content preview first
            if (data.content) {
                displayContentPreview(data.content, data.errors, data.warnings);
                document.getElementById('contentPreviewSection').style.display = 'block';
            }

            // Status badge
            const badge = document.getElementById('statusBadge');
            badge.textContent = data.is_valid ? 'Valid' : 'Invalid';
            badge.className = 'status-badge ' + (data.is_valid ? 'status-valid' : 'status-invalid');

            // File type and title
            document.getElementById('fileTypeLabel').textContent = data.file_type;
            document.getElementById('titleLabel').textContent = data.structure.h1_title ? '- ' + data.structure.h1_title : '';

            // Stats
            document.getElementById('statChars').textContent = data.stats.characters.toLocaleString();
            document.getElementById('statTokens').textContent = '~' + data.stats.tokens_estimate.toLocaleString();
            document.getElementById('statSize').textContent = data.stats.size.formatted;
            document.getElementById('statLinks').textContent = data.stats.link_count;

            // Encoding
            const encSection = document.getElementById('encodingSection');
            const encCard = document.getElementById('statEncodingCard');
            if (data.encoding) {
                const enc = data.encoding;
                const detected = (enc.detected || 'unknown').toUpperCase();
                const encEl = document.getElementById('statEncoding');
                encEl.textContent = detected;
                encEl.style.color = enc.is_utf8 ? '#7FBBE6' : '#FBD779';

                function infoIcon(text) {
                    return '<span class="info-icon">i<span class="tooltip">' + text + '</span></span>';
                }

                let detailsHTML = '';
                detailsHTML += '<div class="structure-item"><span class="structure-label">Detected Encoding' + infoIcon('The character encoding detected by analyzing the raw bytes of the file. Common encodings include UTF-8, ASCII, Latin-1, and Windows-1252.') + '</span><span class="structure-value">' + detected + '</span></div>';
                if (enc.declared) {
                    detailsHTML += '<div class="structure-item"><span class="structure-label">Server Declared' + infoIcon('The encoding the web server says the file uses, set via the Content-Type header (e.g. charset=utf-8). If this doesn\\\'t match the actual encoding, characters may display incorrectly.') + '</span><span class="structure-value">' + enc.declared.toUpperCase() + '</span></div>';
                }
                detailsHTML += '<div class="structure-item"><span class="structure-label">BOM Present' + infoIcon('A Byte Order Mark (BOM) is an invisible character at the very start of a file that signals its encoding. For UTF-8 it\\\'s unnecessary and can cause issues — some parsers treat it as unexpected text, which may break the # Title on line 1.') + '</span><span class="structure-value">' + (enc.has_bom ? 'Yes' : 'No') + '</span></div>';
                detailsHTML += '<div class="structure-item"><span class="structure-label">UTF-8 Compatible' + infoIcon('Whether the file uses UTF-8 or ASCII encoding. UTF-8 is the universal standard for web content and supports all languages and special characters. Non-UTF-8 files risk displaying garbled text across different systems.') + '</span><span class="structure-value" style="color:' + (enc.is_utf8 ? '#7FBBE6' : '#F16365') + ';">' + (enc.is_utf8 ? 'Yes' : 'No') + '</span></div>';
                if (enc.recommendation) {
                    detailsHTML += '<div class="issue-item issue-warning" style="margin-top: 12px;"><span class="issue-line">Tip</span><span class="issue-message">' + enc.recommendation + '</span></div>';
                }
                document.getElementById('encodingDetails').innerHTML = detailsHTML;
                encSection.style.display = 'block';
            } else {
                encSection.style.display = 'none';
                document.getElementById('statEncoding').textContent = '-';
            }

            // Errors
            const errorsSection = document.getElementById('errorsSection');
            const errorsList = document.getElementById('errorsList');
            if (data.errors.length > 0) {
                errorsSection.style.display = 'block';
                document.getElementById('errorCount').textContent = data.errors.length;
                errorsList.innerHTML = data.errors.map(e => `
                    <div class="issue-item issue-error" ${e.line > 0 ? 'onclick="scrollToLine(' + e.line + ')"' : ''} ${e.line > 0 ? 'title="Click to see in preview"' : ''}>
                        <span class="issue-line">${e.line > 0 ? 'Line ' + e.line : 'File'}</span>
                        <span class="issue-message">${e.message}</span>
                    </div>
                `).join('');
            } else {
                errorsSection.style.display = 'none';
            }

            // Warnings
            const warningsSection = document.getElementById('warningsSection');
            const warningsList = document.getElementById('warningsList');
            if (data.warnings.length > 0) {
                warningsSection.style.display = 'block';
                document.getElementById('warningCount').textContent = data.warnings.length;
                warningsList.innerHTML = data.warnings.map(w => `
                    <div class="issue-item issue-warning" ${w.line > 0 ? 'onclick="scrollToLine(' + w.line + ')"' : ''} ${w.line > 0 ? 'title="Click to see in preview"' : ''}>
                        <span class="issue-line">${w.line > 0 ? 'Line ' + w.line : 'File'}</span>
                        <span class="issue-message">${w.message}</span>
                    </div>
                `).join('');
            } else {
                warningsSection.style.display = 'none';
            }

            // Structure
            const structureList = document.getElementById('structureList');
            let structureHTML = '';

            structureHTML += `<div class="structure-item">
                <span class="structure-label">H1 Title</span>
                <span class="structure-value">${data.structure.h1_title || 'Missing'}</span>
            </div>`;

            structureHTML += `<div class="structure-item">
                <span class="structure-label">Blockquote Summary</span>
                <span class="structure-value">${data.structure.blockquote ? 'Present' : 'Missing'}</span>
            </div>`;

            structureHTML += `<div class="structure-item">
                <span class="structure-label">H2 Sections</span>
                <span class="structure-value">${data.structure.h2_sections.length}</span>
            </div>`;

            data.structure.h2_sections.forEach(section => {
                structureHTML += `<div class="structure-item" style="padding-left: 30px;">
                    <span class="structure-label">## ${section.name}</span>
                    <span class="structure-value">${section.link_count} links</span>
                </div>`;
            });

            structureHTML += `<div class="structure-item">
                <span class="structure-label">Total Links</span>
                <span class="structure-value">${data.structure.total_links}</span>
            </div>`;

            if (data.structure.has_optional_section) {
                structureHTML += `<div class="structure-item">
                    <span class="structure-label">Optional Section</span>
                    <span class="structure-value" style="color: #FBD779;">Present</span>
                </div>`;
            }

            structureList.innerHTML = structureHTML;

            document.getElementById('results').classList.add('show');
        }

        // Navbar scroll behavior
        const navbar = document.getElementById('navbar');
        window.addEventListener('scroll', () => {
            navbar.classList.toggle('scrolled', window.scrollY > 60);
        });

        // Scroll reveal for marketing sections
        const revealObserver = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('visible');
                    revealObserver.unobserve(entry.target);
                }
            });
        }, { threshold: 0.12 });

        document.querySelectorAll('.reveal').forEach(el => revealObserver.observe(el));
    </script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_TEMPLATE


SITE_URL = "https://llmvalidator.io"


@app.get("/robots.txt")
async def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml")
async def sitemap_xml():
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'  <url><loc>{SITE_URL}/</loc><priority>1.0</priority><changefreq>weekly</changefreq></url>\n'
        f'  <url><loc>{SITE_URL}/sitemap</loc><priority>0.5</priority><changefreq>monthly</changefreq></url>\n'
        f'  <url><loc>{SITE_URL}/llms.txt</loc><priority>0.5</priority><changefreq>monthly</changefreq></url>\n'
        '</urlset>\n'
    )
    from fastapi.responses import Response
    return Response(content=body, media_type="application/xml; charset=utf-8")


@app.get("/llms.txt", response_class=HTMLResponse)
async def llms_txt():
    body = f"""# LLMs.txt Validator

> A free web-based validator for llms.txt, llms-ctx.txt, and llms-full.txt files. Validates structure, links, encoding, and size against the official llmstxt.org specification.

## Tool

- [Validator]({SITE_URL}/#validator): Paste, upload, or fetch a file by URL to validate
- [Validate API]({SITE_URL}/validate): POST JSON with `content`, `url`, or `file_base64` to validate programmatically

## Pages

- [Home]({SITE_URL}/): Landing page with the validator tool
- [HTML Sitemap]({SITE_URL}/sitemap): Human-readable site map
- [XML Sitemap]({SITE_URL}/sitemap.xml): Machine-readable sitemap
- [Robots]({SITE_URL}/robots.txt): Crawler directives

## Sections

- [What is llms.txt]({SITE_URL}/#about): Overview of the specification
- [Features]({SITE_URL}/#features): Validator capabilities
- [FAQ]({SITE_URL}/#faq): Common questions about the validator and spec

## Reference

- [llms.txt specification](https://llmstxt.org/): Official spec for the llms.txt format
"""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


SITEMAP_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sitemap — LLMs.txt Validator</title>
    <meta name="description" content="Site map for LLMs.txt Validator — all pages, sections, and resources.">
    <link rel="canonical" href="{SITE_URL}/sitemap">
    <meta name="robots" content="index, follow">
    <meta name="theme-color" content="#2C3B4C">
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='20' fill='%23F16365'/%3E%3Cpath d='M28 52 L44 68 L74 34' stroke='white' stroke-width='10' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
    <style>
        @font-face {{
            font-family: 'NHaasGroteskTXPro';
            src: url('/static/Fonts/NHaasGroteskTXPro-65Md.ttf') format('truetype');
            font-weight: 500;
            font-style: normal;
            font-display: swap;
        }}
        @font-face {{
            font-family: 'NeuzeitGro';
            src: url('/static/Fonts/NeuzeitGro-Reg.ttf') format('truetype');
            font-weight: 400;
            font-style: normal;
            font-display: swap;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'NeuzeitGro', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a2330 0%, #2C3B4C 100%);
            background-attachment: fixed;
            min-height: 100vh;
            color: #e2e8f0;
            padding: 60px 20px;
        }}
        h1, h2, h3, h4, h5, h6 {{
            font-family: 'NHaasGroteskTXPro', 'Helvetica Neue', Helvetica, Arial, sans-serif;
        }}
        .wrap {{ max-width: 860px; margin: 0 auto; }}
        a.back {{ color: #7FBBE6; text-decoration: none; font-size: 0.9rem; }}
        a.back:hover {{ text-decoration: underline; }}
        h1 {{ font-size: 2.5rem; margin: 20px 0 8px; }}
        h1 span {{ color: #7FBBE6; }}
        p.lede {{ color: #94a3b8; margin-bottom: 40px; }}
        h2 {{
            font-size: 1.25rem; color: #fff;
            margin: 32px 0 14px; padding-bottom: 10px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }}
        ul {{ list-style: none; padding: 0; }}
        li {{ padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }}
        li:last-child {{ border-bottom: none; }}
        a {{ color: #7FBBE6; text-decoration: none; font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        .desc {{ color: #64748b; font-size: 0.9rem; margin-left: 8px; }}
        footer {{ margin-top: 60px; padding-top: 24px; border-top: 1px solid rgba(255,255,255,0.08); color: #64748b; font-size: 0.85rem; text-align: center; }}
    </style>
</head>
<body>
    <div class="wrap">
        <a href="/" class="back">&larr; Back to validator</a>
        <h1>Site<span>map</span></h1>
        <p class="lede">All pages, sections, and resources on LLMs.txt Validator.</p>

        <h2>Pages</h2>
        <ul>
            <li><a href="/">Home</a><span class="desc">— Landing page with the validator tool</span></li>
            <li><a href="/sitemap">Sitemap</a><span class="desc">— This page</span></li>
            <li><a href="/llms.txt">llms.txt</a><span class="desc">— Our own llms.txt file</span></li>
            <li><a href="/sitemap.xml">XML Sitemap</a><span class="desc">— Machine-readable sitemap</span></li>
            <li><a href="/robots.txt">robots.txt</a><span class="desc">— Crawler directives</span></li>
        </ul>

        <h2>Sections on Home</h2>
        <ul>
            <li><a href="/#validator">Validator</a><span class="desc">— Paste, upload, or fetch a file by URL</span></li>
            <li><a href="/#about">About llms.txt</a><span class="desc">— What the specification is and why it matters</span></li>
            <li><a href="/#features">Features</a><span class="desc">— What the validator checks</span></li>
            <li><a href="/#faq">FAQ</a><span class="desc">— Common questions</span></li>
        </ul>

        <h2>API</h2>
        <ul>
            <li><a href="/validate">POST /validate</a><span class="desc">— Validate content, URL, or base64 file programmatically</span></li>
        </ul>

        <h2>External References</h2>
        <ul>
            <li><a href="https://llmstxt.org/" rel="noopener" target="_blank">llmstxt.org</a><span class="desc">— Official llms.txt specification</span></li>
        </ul>

        <footer>
            A free tool for the llms.txt ecosystem. Built for the AI-ready web.
        </footer>
    </div>
</body>
</html>"""


@app.get("/sitemap", response_class=HTMLResponse)
async def sitemap_html():
    return SITEMAP_HTML


async def check_file_exists(client: httpx.AsyncClient, base_url: str, filename: str) -> dict:
    """Check whether a single llms.txt-family file exists at base_url/filename."""
    full_url = f"{base_url}/{filename}"

    def _from_curl():
        raw, ct = _fetch_with_curl(full_url)
        if raw:
            return {
                "file": filename, "url": full_url, "found": True,
                "status": 200, "size": len(raw),
                "content_type": (ct.split(";")[0].strip() if ct else None),
            }
        return None

    try:
        response = await client.head(full_url)
        if response.status_code in (403, 405, 501):
            response = await client.get(full_url)

        status = response.status_code
        found = 200 <= status < 300

        if not found and status == 403:
            curl_result = _from_curl()
            if curl_result:
                return curl_result

        size = None
        content_type = None
        ct_header = response.headers.get("content-type", "")
        if ct_header:
            content_type = ct_header.split(";")[0].strip() or None

        if found:
            cl = response.headers.get("content-length")
            if cl and cl.isdigit():
                size = int(cl)
            elif response.request.method == "HEAD":
                # HEAD didn't give us size — do a GET to measure
                get_resp = await client.get(full_url)
                size = len(get_resp.content)
                if not content_type:
                    ct_header = get_resp.headers.get("content-type", "")
                    if ct_header:
                        content_type = ct_header.split(";")[0].strip() or None
            else:
                size = len(response.content)

        return {
            "file": filename, "url": full_url, "found": found,
            "status": status, "size": size, "content_type": content_type,
        }
    except (httpx.RequestError, httpx.HTTPError):
        curl_result = _from_curl()
        if curl_result:
            return curl_result
        return {
            "file": filename, "url": full_url, "found": False,
            "status": 0, "size": None, "content_type": None,
        }


@app.post("/detect")
async def detect(request: DetectRequest):
    """Detect presence of llms.txt, llms-ctx.txt, and llms-full.txt at a URL."""
    raw_url = (request.url or "").strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")

    base_url = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        results = await asyncio.gather(
            *[check_file_exists(client, base_url, f) for f in LLMS_FILES]
        )

    found_count = sum(1 for r in results if r["found"])
    return {
        "url": base_url,
        "results": list(results),
        "summary": {"found_count": found_count, "total": len(LLMS_FILES)},
    }


_FILE_TYPE_PROMPTS = {
    "llms.txt": (_GENERATE_SYSTEM_PROMPT, 4096),
    "llms-ctx.txt": (_CTX_SYSTEM_PROMPT, 6144),
    "llms-full.txt": (_FULL_SYSTEM_PROMPT, 8192),
}


def _normalize_url(raw_url: str) -> tuple[str, str]:
    """Return (full_url, base_url). Raises 400 on invalid input."""
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url
    parsed = urlparse(raw_url)
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")
    return raw_url, f"{parsed.scheme}://{parsed.netloc}"


async def _run_generation(raw_url: str, file_type: str) -> dict:
    """Crawl + LLM-generate. Caller is responsible for auth/rate limit."""
    if file_type not in _FILE_TYPE_PROMPTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file_type}")

    _, base_url = _normalize_url(raw_url)
    parsed = urlparse(base_url)
    homepage_title: Optional[str] = None
    homepage_desc: Optional[str] = None

    browser_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=10.0,
        headers={
            "User-Agent": browser_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:
        urls = await _fetch_sitemap_urls(client, base_url)
        if not urls:
            urls, homepage_title, homepage_desc = await _scrape_homepage_links(client, base_url)
        if base_url not in urls:
            urls.insert(0, base_url)

        seen_urls: set[str] = set()
        filtered: list[str] = []
        for u in urls:
            ph = urlparse(u)
            if not ph.netloc:
                continue
            key = f"{ph.scheme}://{ph.netloc}{ph.path.rstrip('/') or '/'}"
            if key in seen_urls:
                continue
            seen_urls.add(key)
            filtered.append(u)
        urls = filtered[:_GENERATE_MAX_URLS]

        if not urls:
            raise HTTPException(
                status_code=400,
                detail="Could not find any pages to analyze. The site may be unreachable or have no internal links.",
            )

        diag = {"httpx_ok": 0, "curl_ok": 0, "non_200": 0, "non_html": 0, "no_meta": 0, "error": 0}
        sem = asyncio.Semaphore(_GENERATE_FETCH_CONCURRENCY)
        results = await asyncio.gather(
            *[_fetch_page_metadata(client, sem, u, diag) for u in urls]
        )
        pages = [r for r in results if r]

    if not pages:
        diag_summary = ", ".join(f"{k}={v}" for k, v in diag.items() if v)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not extract metadata from any of {len(urls)} pages. "
                f"Breakdown: {diag_summary or 'no detail'}. "
                "The site likely blocks automated requests or doesn't expose page metadata."
            ),
        )

    homepage_meta = next(
        (p for p in pages if p["url"].rstrip("/") == base_url.rstrip("/")), None
    )
    if homepage_meta:
        site_title = homepage_meta["title"]
        site_desc = homepage_meta["description"] or homepage_desc or ""
    else:
        site_title = homepage_title or parsed.netloc
        site_desc = homepage_desc or ""

    pages_text = "\n".join(
        f"- {p['url']} — {p['title']}"
        + (f" — {p['description']}" if p["description"] else "")
        for p in pages
    )
    user_prompt = (
        f"Site: {base_url}\n"
        f"Site title: {site_title}\n"
        f"Site description: {site_desc or '(none)'}\n\n"
        f"Pages ({len(pages)}):\n{pages_text}\n\n"
        f"Generate the {file_type} for this site."
    )

    system_prompt, max_tokens = _FILE_TYPE_PROMPTS[file_type]
    anthropic_client = _get_anthropic()
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {str(e)[:300]}")

    content = ""
    for block in response.content:
        if block.type == "text":
            content = block.text
            break

    if not content.strip():
        raise HTTPException(status_code=502, detail="LLM returned empty content")

    return {
        "url": base_url,
        "file_type": file_type,
        "content": content.strip(),
        "pages_analyzed": len(pages),
        "model": response.model,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }


@app.post("/generate")
async def generate(request: GenerateRequest, http_request: Request):
    """Generate llms.txt (free tier). Crawls + asks Claude Haiku to organize it."""
    ip = _client_ip(http_request)
    if not _check_rate_limit(ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit hit ({_GENERATE_MAX_PER_HOUR} generations/hour). Try again in an hour.",
        )
    _get_anthropic()  # fail fast if key missing
    return await _run_generation(request.url, "llms.txt")


class CheckoutRequest(BaseModel):
    url: str
    file_type: str  # "llms-ctx.txt" or "llms-full.txt"


@app.post("/checkout")
async def checkout(request: CheckoutRequest, http_request: Request):
    """Create a Stripe Checkout Session for paid ctx/full generation."""
    _ensure_stripe()
    raw_url, base_url = _normalize_url(request.url)
    if request.file_type not in GENERATION_PRICES:
        raise HTTPException(status_code=400, detail=f"Unsupported paid file type: {request.file_type}")

    price_info = GENERATION_PRICES[request.file_type]
    site_host = urlparse(base_url).netloc
    return_base = str(http_request.base_url).rstrip("/")

    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": price_info["label"],
                        "description": f"AI-generated {request.file_type} for {site_host}",
                    },
                    "unit_amount": price_info["amount_cents"],
                },
                "quantity": 1,
            }],
            success_url=f"{return_base}/?paid_session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{return_base}/?checkout_canceled=1",
            metadata={
                "url": raw_url,
                "file_type": request.file_type,
                "site_host": site_host,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {str(e)[:300]}")

    return {"checkout_url": session.url, "session_id": session.id}


@app.get("/generate-paid")
async def generate_paid(session_id: str):
    """Verify a paid Stripe session and return the generated ctx/full content."""
    _ensure_stripe()
    try:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not retrieve Stripe session: {str(e)[:200]}")

    if session.payment_status != "paid":
        raise HTTPException(
            status_code=402,
            detail=f"Payment not completed (status: {session.payment_status}).",
        )

    metadata = session.metadata or {}
    url = metadata.get("url")
    file_type = metadata.get("file_type")
    if not url or not file_type:
        raise HTTPException(status_code=400, detail="Stripe session is missing required metadata.")
    if file_type not in GENERATION_PRICES:
        raise HTTPException(status_code=400, detail=f"Unsupported paid file type: {file_type}")

    _get_anthropic()
    return await _run_generation(url, file_type)


@app.post("/validate")
async def validate(request: ValidateRequest):
    """Validate llms.txt content or fetch from URL."""

    content = request.content
    encoding_info = None

    if request.url:
        content, encoding_info = await fetch_llmstxt(request.url, request.file_type)
    elif request.file_base64:
        # Uploaded file — decode base64 to raw bytes for encoding detection
        raw = base64.b64decode(request.file_base64)
        encoding_info = detect_encoding(raw)
        # Strip BOM if present before decoding
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]
        detected = (encoding_info["detected"] or "utf-8").replace("-", "_")
        try:
            content = raw.decode(detected)
        except (UnicodeDecodeError, LookupError):
            content = raw.decode("utf-8", errors="replace")
    elif content:
        # For pasted content, detect encoding from the UTF-8 bytes
        raw = content.encode("utf-8")
        encoding_info = detect_encoding(raw)

    if not content:
        raise HTTPException(status_code=400, detail="No content provided")

    result = validate_llmstxt(content, request.file_type)

    # Add encoding warnings to the result
    if encoding_info:
        if not encoding_info["is_utf8"]:
            result.warnings.append(asdict(ValidationError(
                0, f"Encoding is {encoding_info['detected'] or 'unknown'} — UTF-8 is strongly recommended for llms.txt files.", "warning"
            )))
        if encoding_info["has_bom"]:
            result.warnings.append(asdict(ValidationError(
                0, "File contains a BOM (Byte Order Mark). Some LLM parsers may not handle this correctly. Consider saving as UTF-8 without BOM.", "warning"
            )))
        if encoding_info["declared"] and encoding_info["detected"]:
            declared_norm = encoding_info["declared"].lower().replace("-", "").replace("_", "")
            detected_norm = encoding_info["detected"].lower().replace("-", "").replace("_", "")
            if declared_norm != detected_norm and not (declared_norm in ("utf8", "ascii") and detected_norm in ("utf8", "ascii")):
                result.warnings.append(asdict(ValidationError(
                    0, f"Encoding mismatch: server declares {encoding_info['declared']} but content appears to be {encoding_info['detected']}.", "warning"
                )))

    # Include raw content and encoding info in response
    response = asdict(result)
    response["content"] = content
    response["encoding"] = encoding_info

    return response
