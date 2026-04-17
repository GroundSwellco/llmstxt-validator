# LLMs.txt Validator - Project Notes

## Project Overview
A web-based validator for llms.txt files - the proposed standard for providing LLM-friendly content on websites.

**Location:** `C:\Users\rodol\llmstxt-validator`
**GitHub:** (to be created)
**Vercel:** (to be deployed)

## What It Does
1. Validates llms.txt, llms-ctx.txt, and llms-full.txt files
2. Can fetch files from any URL or accept pasted content
3. Checks compliance with the llms.txt specification
4. Provides detailed stats: character count, file size, token estimate
5. Shows structure breakdown (H1, blockquote, H2 sections, links)
6. Reports errors and warnings with line numbers

## Validation Rules (from llmstxt.org spec)
- **Required:** H1 header (`# Title`)
- **Recommended:** Blockquote summary (`> Description`)
- **Optional:** H2 sections (`## Section Name`)
- **Link format:** `- [Title](URL): description`
- **Size limit:** llms.txt should be < 500KB (llms-full.txt can be larger)

## Tech Stack
- **Backend:** FastAPI (Python)
- **HTTP Client:** httpx (for fetching URLs)
- **Frontend:** Vanilla HTML/CSS/JS (embedded in api/index.py)
- **Deployment:** Vercel (serverless Python)

## File Structure
```
llmstxt-validator/
├── api/
│   ├── index.py          # Main app with validation logic + HTML
│   └── requirements.txt  # Python dependencies
├── vercel.json           # Vercel configuration
├── .gitignore
└── CLAUDE.md             # This file
```

## Page Structure
The app is a single-page landing page with marketing content below the validator tool:

1. **Sticky navbar** — Logo, anchor links (Validator, About, Features, FAQ), CTA button
2. **Hero section** — Large headline, value prop, CTA that scrolls to tool
3. **Validator tool** — URL fetch, paste content, file upload tabs + results display
4. **What is llms.txt** — Two-column explainer with styled code example
5. **Why Validate** — 3-column benefit cards
6. **Features** — 2x3 grid of capability cards
7. **How It Works** — 3-step visual flow
8. **FAQ** — Accordion with `<details>`/`<summary>`
9. **CTA band** — Final call to action
10. **Footer** — Links to spec, navigation

## Features
- [x] Fetch llms.txt from any URL
- [x] Paste content directly
- [x] Upload .txt/.md files with drag-and-drop
- [x] Support for llms.txt, llms-ctx.txt, llms-full.txt
- [x] Character count, file size, token estimation
- [x] Validation errors with line numbers
- [x] Warnings for best practices
- [x] Structure visualization
- [x] Encoding detection (UTF-8, BOM, server header comparison)
- [x] Inline editing and download as UTF-8
- [x] Dark theme SaaS-style landing page
- [x] Scroll reveal animations
- [x] Responsive design (mobile/tablet/desktop)
- [x] SEO meta tags and Open Graph

## Token Estimation
Uses approximation: ~1.3 tokens per word + 0.5 per punctuation mark.
This is a rough estimate - actual tokenization varies by model.

## Quick Commands

### Run Locally
```bash
cd C:\Users\rodol\llmstxt-validator
pip install fastapi httpx uvicorn
uvicorn api.index:app --reload
# Opens at http://localhost:8000
```

### Deploy to Vercel
1. Push to GitHub
2. Import to Vercel
3. No environment variables needed

## Reference
- llms.txt specification: https://llmstxt.org/
- Similar validator: https://llmstxtvalidator.dev/
