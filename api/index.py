import re
import base64
import subprocess
import httpx
from charset_normalizer import from_bytes
from urllib.parse import urlparse
from typing import Optional
from dataclasses import dataclass, asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="LLMs.txt Validator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ValidateRequest(BaseModel):
    content: Optional[str] = None
    url: Optional[str] = None
    file_base64: Optional[str] = None
    file_type: str = "llms.txt"  # llms.txt, llms-ctx.txt, llms-full.txt


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
    <title>LLMs.txt Validator</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 100%);
            min-height: 100vh;
            color: #e2e8f0;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 40px 20px; }

        header { text-align: center; margin-bottom: 40px; }
        .logo { font-size: 2.5rem; font-weight: 700; }
        .logo span { color: #10b981; }
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
        .tab.active { background: #10b981; color: #fff; border-color: #10b981; }

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
        .url-input:focus { outline: none; border-color: #10b981; }

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
        textarea:focus { outline: none; border-color: #10b981; }
        textarea::placeholder { color: #475569; }

        .btn {
            padding: 12px 24px;
            background: #10b981;
            border: none;
            border-radius: 8px;
            color: #fff;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn:hover { background: #059669; transform: translateY(-1px); }
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
        .file-type-btn.active { background: rgba(16,185,129,0.2); color: #10b981; border-color: #10b981; }

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
        .status-valid { background: rgba(16,185,129,0.2); color: #10b981; }
        .status-invalid { background: rgba(239,68,68,0.2); color: #f87171; }

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
        .stat-value { font-size: 1.8rem; font-weight: 700; color: #10b981; }
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
        .issue-error { border-left: 3px solid #f87171; }
        .issue-warning { border-left: 3px solid #fbbf24; }

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
        .structure-value { color: #10b981; font-weight: 500; }

        .upload-area {
            border: 2px dashed rgba(255,255,255,0.15);
            border-radius: 12px;
            padding: 40px 20px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s;
        }
        .upload-area:hover, .upload-area.dragover {
            border-color: #10b981;
            background: rgba(16,185,129,0.05);
        }
        .upload-placeholder p { margin-top: 8px; color: #94a3b8; }
        .upload-file-info {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 16px;
            font-size: 1rem;
        }
        .upload-file-info span:first-child { color: #10b981; font-weight: 500; }

        .loading { display: none; text-align: center; padding: 40px; }
        .loading.show { display: block; }
        .spinner {
            width: 40px;
            height: 40px;
            border: 3px solid rgba(255,255,255,0.1);
            border-left-color: #10b981;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 16px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        footer { text-align: center; margin-top: 60px; color: #475569; font-size: 0.9rem; }
        footer a { color: #10b981; text-decoration: none; }

        .example-link { color: #10b981; cursor: pointer; font-size: 0.85rem; }
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
            border-left: 3px solid #f87171;
        }
        .line-error .line-number {
            background: rgba(239, 68, 68, 0.2);
            color: #f87171;
        }
        .line-warning {
            background: rgba(251, 191, 36, 0.15);
            border-left: 3px solid #fbbf24;
        }
        .line-warning .line-number {
            background: rgba(251, 191, 36, 0.2);
            color: #fbbf24;
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
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo">LLMs<span>.txt</span> Validator</div>
            <p class="subtitle">Validate your llms.txt, llms-ctx.txt, and llms-full.txt files</p>
        </header>

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
                <div class="issues-title" style="color: #38bdf8;">
                    Encoding Details
                </div>
                <div id="encodingDetails"></div>
                <div id="downloadFixWrap" style="display:none; margin-top: 16px; text-align: center;">
                    <button class="btn" id="downloadFixBtn" style="background: #3b82f6;">
                        &#8681; Download as UTF-8
                    </button>
                    <p style="margin-top: 8px; font-size: 0.8rem; color: #64748b;">
                        Re-encoded as UTF-8 without BOM — ready to replace your current file
                    </p>
                </div>
            </div>

            <div class="content-preview-section" id="contentPreviewSection">
                <div class="content-preview-title">
                    <span>Content Preview</span>
                    <button class="btn btn-secondary toggle-preview-btn" id="togglePreviewBtn">Hide</button>
                </div>
                <div class="content-preview-wrapper" id="contentPreviewWrapper">
                    <div id="contentPreview"></div>
                </div>
            </div>

            <div class="issues-section" id="errorsSection" style="display:none;">
                <div class="issues-title" style="color: #f87171;">
                    Errors <span class="count" id="errorCount">0</span>
                </div>
                <div id="errorsList"></div>
            </div>

            <div class="issues-section" id="warningsSection" style="display:none;">
                <div class="issues-title" style="color: #fbbf24;">
                    Warnings <span class="count" id="warningCount">0</span>
                </div>
                <div id="warningsList"></div>
            </div>

            <div class="structure-section">
                <div class="structure-title">Structure</div>
                <div id="structureList"></div>
            </div>
        </div>

        <footer>
            <p>Learn more about the <a href="https://llmstxt.org/" target="_blank">llms.txt specification</a></p>
        </footer>
    </div>

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

        function handleFile(file) {
            const reader = new FileReader();
            reader.onload = () => {
                uploadedFileBase64 = btoa(String.fromCharCode(...new Uint8Array(reader.result)));
                uploadFileName.textContent = file.name + ' (' + (file.size / 1024).toFixed(1) + ' KB)';
                uploadPlaceholder.style.display = 'none';
                uploadFileInfo.style.display = 'flex';
            };
            reader.readAsArrayBuffer(file);
        }

        document.getElementById('uploadBtn').addEventListener('click', async () => {
            if (!uploadedFileBase64) return alert('Please select a file first');
            await validate({ file_base64: uploadedFileBase64, file_type: currentFileType });
        });

        // Download re-encoded UTF-8 file
        document.getElementById('downloadFixBtn').addEventListener('click', () => {
            if (!currentContent) return;
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
                encEl.style.color = enc.is_utf8 ? '#10b981' : '#fbbf24';

                let detailsHTML = '';
                detailsHTML += '<div class="structure-item"><span class="structure-label">Detected Encoding</span><span class="structure-value">' + detected + '</span></div>';
                if (enc.declared) {
                    detailsHTML += '<div class="structure-item"><span class="structure-label">Server Declared</span><span class="structure-value">' + enc.declared.toUpperCase() + '</span></div>';
                }
                detailsHTML += '<div class="structure-item"><span class="structure-label">BOM Present</span><span class="structure-value">' + (enc.has_bom ? 'Yes' : 'No') + '</span></div>';
                detailsHTML += '<div class="structure-item"><span class="structure-label">UTF-8 Compatible</span><span class="structure-value" style="color:' + (enc.is_utf8 ? '#10b981' : '#f87171') + ';">' + (enc.is_utf8 ? 'Yes' : 'No') + '</span></div>';
                if (enc.recommendation) {
                    detailsHTML += '<div class="issue-item issue-warning" style="margin-top: 12px;"><span class="issue-line">Tip</span><span class="issue-message">' + enc.recommendation + '</span></div>';
                }
                document.getElementById('encodingDetails').innerHTML = detailsHTML;
                encSection.style.display = 'block';

                // Show download-as-UTF-8 button when there's an encoding issue
                const needsFix = !enc.is_utf8 || enc.has_bom;
                document.getElementById('downloadFixWrap').style.display = needsFix ? 'block' : 'none';
            } else {
                encSection.style.display = 'none';
                document.getElementById('statEncoding').textContent = '-';
                document.getElementById('downloadFixWrap').style.display = 'none';
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
                    <span class="structure-value" style="color: #fbbf24;">Present</span>
                </div>`;
            }

            structureList.innerHTML = structureHTML;

            document.getElementById('results').classList.add('show');
        }
    </script>
</body>
</html>'''


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_TEMPLATE


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
