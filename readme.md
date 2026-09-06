# Signal AI Lead Gen

Signal is an AI-assisted B2B prospecting and CRM system for agencies, freelancers, and sales teams that sell digital services.

It turns a natural-language market request into a prospecting workflow:

1. Understand the market and service described in the search.
2. Discover businesses through Google Maps and Google Search.
3. Deduplicate businesses found by both sources.
4. Inspect public websites for service-specific opportunity signals.
5. Find publicly listed business contact details.
6. Score and qualify the strongest opportunities.
7. Generate a personalized outreach pitch.
8. Save the opportunity into a searchable CRM.

## What The AI System Does

The system is not a predictive machine-learning model trained on a private dataset. It is an AI-assisted rules and web-intelligence engine that combines natural-language intent parsing, search APIs, HTML analysis, lead scoring, contact extraction, and automated pitch generation.

This makes the output explainable: every lead includes the public signal that caused it to qualify.

### Natural-Language Service Intent

The search bar accepts a market and a digital-service opportunity in one sentence.

Examples:

```text
restaurants in Chicago having poor logo design
 dentists in Canada needing SEO
 real estate agencies with no social media
 law firms needing accessibility
 shops needing an online store
 businesses with poor website performance
```

The intent parser separates the market from the service:

```text
Input:   restaurants in Chicago having poor logo design
Market:  restaurants in Chicago
Service: logo design
```

Supported service modes include:

- Website design and web presence
- Logo design
- SEO and local SEO
- Social media presence
- Branding and brand identity
- Accessibility
- Content and copywriting
- Website performance and page speed
- E-commerce and online stores

Unknown or ordinary searches use the default website audit mode.

## Discovery Engine

Each search uses two SerpAPI sources:

### Google Maps

Maps results provide structured local-business information such as:

- Business name
- Website
- Phone number
- Address
- Local listing context

### Google Search

Organic Google results expand coverage beyond businesses that appear in the Maps result set. Search results can reveal:

- Official business websites
- Contact pages
- About pages
- Businesses with weak or incomplete Maps listings
- Additional companies relevant to the market query

Results from both sources are merged and deduplicated using normalized business name and website values. A lead can be marked as coming from `Google Maps`, `Google Search`, or both.

## Public Contact Discovery

For businesses with a public website, the system checks the homepage and common contact paths:

```text
/
/contact
/contact-us
/about
/about-us
```

It extracts publicly visible:

- Email addresses
- Phone numbers when the listing did not already provide one
- Contact names when clearly labeled
- Contact roles such as owner, founder, manager, director, or contact person

The system does not guess private identities. A person is saved only when the website publicly presents the name in a clear role-based context.

Contact fields include:

- Contact name
- Contact role
- Contact email
- Phone number
- Discovery source

## Service-Specific Auditing

The website auditor changes its checks according to the requested service.

### Website Opportunity

Checks include:

- Missing HTTPS
- Missing mobile viewport metadata
- Thin page content
- Broken or unreachable websites
- Missing website listings
- CMS detection

### Logo And Branding Opportunity

Checks include:

- No visible logo or brand mark
- Logo image without an accessible brand description
- Missing logo-related HTML signals
- Thin brand-story content

### SEO Opportunity

Checks include:

- Missing page title
- Missing meta description
- Missing primary heading
- Missing canonical link

### Social Media Opportunity

Checks for public links or references to platforms such as:

- Facebook
- Instagram
- LinkedIn
- X/Twitter
- TikTok
- YouTube

### Accessibility Opportunity

Checks include:

- Images without alt text
- Missing main content landmarks

### Content Opportunity

Checks for thin publicly visible page content.

### Performance Opportunity

Checks for basic warning signals such as:

- Very large HTML payloads
- Heavy script counts

### E-commerce Opportunity

Checks include:

- Product structured data
- Product, shop, cart, checkout, or commerce journey signals

These are lightweight public-HTML heuristics. They are useful for prospect qualification, not a replacement for a full professional audit, Lighthouse run, accessibility review, or brand assessment.

## Lead Scoring

Every candidate receives a score from the service-specific audit. Higher scores represent stronger visible opportunity signals.

Leads with a score of 50 or higher are saved. Each saved lead includes:

- Business profile
- Original search command
- Requested service type
- Lead score
- Opportunity status
- Detected issue reason
- Generated pitch
- Website audit data
- Public contact data
- Search source
- CRM stage

The original search command is preserved so leads from searches such as these remain distinguishable:

```text
dentists in Canada
real estate agency in Canada
restaurants in Chicago having poor logo design
```

## CRM Workflow

Open the CRM at:

```text
http://127.0.0.1:5000/leads
```

The CRM provides:

- A library of every saved search command
- Lead counts per search
- Exact search selection
- Industry filtering across related searches
- Contact directory for discovered public emails and names
- Lead stages: `New`, `Contacted`, and `Closed`
- Individual lead case studies
- CSV export

### Exact Search View

Click a saved command such as:

```text
dentists in Canada
```

The CRM shows only leads created by that search.

The equivalent URL is:

```text
/leads?search=dentists+in+Canada
```

### Industry View

Use the CRM filter to combine all searches containing an industry term:

```text
/leads?industry=dentists
```

That can combine results from:

```text
dentists in Canada
dentists in Toronto
dentists in Vancouver
```

### Lead Case Study

Click a business name from the CRM table to open:

```text
/leads/<lead_id>
```

The case study contains:

- Business profile
- Search command and service context
- Contact intelligence
- Opportunity explanation
- Technical evidence
- Score and CRM stage
- Generated outreach pitch
- Copy-pitch action
- Recommended outreach structure

## Asynchronous Search

The prospector does not block the browser while external search and website checks run.

### Start A Search

```http
POST /api/search
Content-Type: application/json

{"query": "restaurants in Chicago having poor logo design"}
```

The endpoint returns a task ID:

```json
{"task_id": "..."}
```

### Poll Progress

```http
GET /api/task-status/<task_id>
```

The response reports:

- `queued`
- `searching`
- `completed`
- `failed`
- Progress percentage
- Newly saved leads
- Duplicate leads skipped
- Error details when a provider fails

The prospector template polls this endpoint every second and renders completed results without a full page refresh.

## Project Structure

```text
AI LEAD GEN/
├── app.py                              # Flask app, models, services, routes
├── app/
│   └── templates/
│       ├── prospector/
│       │   └── index.html              # Search workspace and polling UI
│       └── crm/
│           ├── dashboard.html          # Search library and lead CRM
│           └── lead_detail.html        # Lead case study and pitch
├── requirements.txt
├── .env                                # Local secrets and configuration
├── .gitignore
└── ai_lead_gen.sqlite3                 # Local SQLite database, created at runtime
```

The Python backend is intentionally consolidated into `app.py` for this project version.

## Technology Stack

### Backend

- Python 3.11+
- Flask
- Flask-SQLAlchemy
- SQLAlchemy ORM
- Flask-Migrate
- Flask-Login extension
- SQLite for local persistence
- Requests
- BeautifulSoup
- SerpAPI

### Frontend

- Jinja2 templates
- Tailwind CSS CDN
- Vanilla JavaScript
- Fetch API
- Inline scripts at the bottom of the relevant templates
- No Node.js, npm, React, Next.js, external CSS files, or external JavaScript files

## Setup

### 1. Activate The Virtual Environment

PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

If the virtual environment does not exist yet:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install Dependencies

```powershell
py -m pip install -r requirements.txt
```

Always install using the same interpreter that launches the app:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. Configure `.env`

Create `.env` in the project root:

```dotenv
DATABASE_URL=sqlite:///ai_lead_gen.sqlite3
SECRET_KEY=replace-with-a-long-random-secret
SERPAPI_KEY=replace-with-your-serpapi-key
SCRAPER_TIMEOUT=4
MAIL_SERVER=smtp.example.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USE_SSL=false
MAIL_USERNAME=you@example.com
MAIL_PASSWORD=your-smtp-password
MAIL_DEFAULT_SENDER=you@example.com
```

Generate a Flask secret key with:

```powershell
py -c "import secrets; print(secrets.token_urlsafe(32))"
```

Do not commit `.env` or publish your SerpAPI key.

SMTP fields are optional. Without them, the outreach editor remains available but the send endpoint returns a configuration error instead of attempting delivery.

### 4. Start The Application

```powershell
py app.py
```

Open:

```text
http://127.0.0.1:5000/
```

Prospector:

```text
http://127.0.0.1:5000/
```

CRM:

```text
http://127.0.0.1:5000/leads
```

## Database Behavior

The current local configuration uses SQLite, so PostgreSQL is not required to run the application.

The app creates its tables at startup and upgrades the local SQLite schema for newly added nullable fields, including:

- Search command
- Service type
- Contact name
- Contact role
- Contact email
- Discovery source
- Logo audit fields

The local database file is ignored by Git.

For a production deployment with multiple workers, replace the in-process SQLite and thread-based task registry with a managed database and durable worker system such as PostgreSQL plus Celery or RQ.

### Live Hosting Database

Do not use the local SQLite `DATABASE_URL` on a live host unless the service has a persistent disk mounted at the database location. A redeploy or container restart can otherwise discard users and password changes, making a successful password reset appear not to work during the next login.

Set the hosting provider's `DATABASE_URL` to a persistent managed PostgreSQL database, or attach a persistent disk for SQLite. Confirm that the reset request and login request reach the same database. The `SECRET_KEY` must also remain unchanged between deploys so password-reset links remain valid for their one-hour lifetime.

## Duplicate Prevention

Repeated searches do not insert the same business repeatedly. Leads are normalized by business name and website before saving.

When a search discovers an already saved lead:

- The lead is skipped.
- It is not inserted again.
- The task reports the skipped count.
- The CRM keeps the original saved record.

## Important Limitations

- SerpAPI requires a valid active API key and available account credits.
- Google Maps and Google Search results depend on SerpAPI response quality and regional availability.
- Search and contact extraction are external network operations and may be slow.
- Contact data is limited to information publicly exposed by business websites.
- Many businesses do not publish owner names or email addresses.
- The logo, SEO, accessibility, content, and performance checks are heuristic signals, not definitive audits.
- The background task registry and 24-hour search cache are process-local and intended for local development or a single app process. Website enrichment is capped at 10 concurrent workers with a strict four-second timeout per fetch.
- Flask-Login and the User model are present for future authentication, but this version does not yet provide a complete login and registration workflow.

## Privacy And Responsible Use

Use contact data only when it is publicly available and relevant to legitimate business outreach. Respect website terms, applicable privacy laws, email regulations, robots directives, rate limits, and provider terms of service. Do not use the system to collect private credentials or non-public personal information.

## Troubleshooting

### `SERPAPI_KEY is not configured`

Confirm `.env` is in the same project root as `app.py`, then restart the app.

### `Invalid API key`

Create or regenerate a valid key in your SerpAPI account and update `.env`.

### `location and ll parameters can't be used together`

The current Maps request uses `location` and `z` without `ll`.

### `Missing z or m parameter`

The Maps request includes `z=12` for location-based searches.

### Database connection errors mentioning PostgreSQL

Confirm `.env` contains the SQLite configuration:

```dotenv
DATABASE_URL=sqlite:///ai_lead_gen.sqlite3
```

Then restart with the project virtual environment:

```powershell
.\.venv\Scripts\python.exe app.py
```

### No leads returned

Check:

1. The SerpAPI key is valid.
2. The SerpAPI account has credits.
3. The query contains a clear market.
4. The requested service phrase is supported.
5. The returned websites expose enough public HTML signals to qualify.

## License

No license has been specified for this project yet.
