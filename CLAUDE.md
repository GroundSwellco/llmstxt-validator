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
3. Configure environment variables (see below)

## Environment Variables
Set in Vercel project settings (or `.env.local` for local dev):

| Variable | Required for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `/generate` | Claude Haiku for llms.txt authoring |
| `STRIPE_SECRET_KEY` | `/checkout`, `/generate-paid`, `/api/subscribe` | Server-side Stripe key (`sk_live_...` / `sk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | `/api/billing/webhook` | Signing secret for the Stripe webhook endpoint (`whsec_...`). Created when you add the webhook in Stripe → Developers → Webhooks. |
| `SUPABASE_URL` | Auth, billing | Public project URL, e.g. `https://xxx.supabase.co` |
| `SUPABASE_ANON_KEY` | Auth | Public anon key — safe to expose to the browser |
| `SUPABASE_JWT_SECRET` | Server-side auth (HS256 projects only) | HS256 secret from Supabase → Project Settings → API → JWT Settings. **Server-only.** Newer Supabase projects use asymmetric ES256 keys and don't need this — the backend auto-detects and verifies via the project's JWKS endpoint. |
| `SUPABASE_SERVICE_ROLE_KEY` | Billing webhook + tier reads | Server-only key from Supabase → Project Settings → API. Bypasses RLS so the backend can update `profiles` from the Stripe webhook (where there is no user session). **Never expose to the browser.** |

When `SUPABASE_URL` / `SUPABASE_ANON_KEY` are missing, the page still renders but the auth modal shows a configuration notice and submission is disabled. When `SUPABASE_SERVICE_ROLE_KEY` is missing, `/api/me` falls back to free-tier reporting and the webhook can't persist subscription state.

## Database Migrations
SQL migrations live in `supabase/migrations/`. Run them in order via the Supabase SQL Editor:

| File | Adds |
|---|---|
| `0001_billing.sql` | `profiles` table (tier, stripe IDs, subscription status), auto-create profile trigger on user signup, RLS so users read only their own row. |

## Auth Architecture
- **Client:** `@supabase/supabase-js` (UMD bundle from jsDelivr) handles signup/login/session persistence via `localStorage`.
- **Server:** No Supabase SDK on Python side — just PyJWT verifying HS256 tokens. `get_current_user(request)` extracts the bearer token and returns `{id, email, role}` or `None`. `require_user(request)` raises 401.
- **Auth flow:** email + password only at launch. Magic links / OAuth deferred. Email confirmation behavior follows the Supabase project setting; if confirmation is required, signup shows a "check your email" message instead of immediately signing the user in.
- **Calling protected endpoints from JS:** `await window.authHeaders()` returns `{ Authorization: 'Bearer ...' }` (or `{}` if logged out).

## Pricing Tiers
| Tier | Price | What's included | Stripe `lookup_key` |
|---|---|---|---|
| Free | $0 | Validator + Detector + account | — |
| Business | $19/mo | Save runs (next PR), in-app editing (TBD), ongoing reviews (TBD) | `business_monthly` |
| Agency | $49/mo | Bulk reports + audit deliverables (TBD) | `agency_monthly` |

Subscription billing layers on top of the existing one-time `/checkout` flow (kept for now — Stripe Products are auto-created on first `/api/subscribe` call via `lookup_key`, so no manual Stripe Dashboard setup is required for prices).

### Stripe Webhook
Add a webhook endpoint in Stripe → Developers → Webhooks pointing at `https://<your-host>/api/billing/webhook`. Listen to at least:
- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`

Copy the signing secret (`whsec_...`) into `STRIPE_WEBHOOK_SECRET` in Vercel.

## Reference
- llms.txt specification: https://llmstxt.org/
- Similar validator: https://llmstxtvalidator.dev/
