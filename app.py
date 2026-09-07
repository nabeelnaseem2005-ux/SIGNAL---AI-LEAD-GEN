import csv
import copy
import gzip
import json
import os
import re
import smtplib
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from threading import Lock, Thread
from urllib.parse import urlparse
from uuid import uuid4
from collections.abc import Callable

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, Blueprint, Response, current_app, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_mail import Mail, Message
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from serpapi import GoogleSearch
from sqlalchemy import inspect, or_
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
mail = Mail()
login_manager.login_view = "auth.login"


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "Your session has expired. Please log in again."}), 401
    return redirect(url_for(login_manager.login_view))


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "sqlite:///ai_lead_gen.sqlite3",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
    SCRAPER_TIMEOUT = 4.0
    MAX_SCRAPER_WORKERS = min(int(os.getenv("MAX_SCRAPER_WORKERS", "10")), 10)
    SEARCH_CACHE_TTL = 24 * 60 * 60
    MAIL_SERVER = os.getenv("MAIL_SERVER", "")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "false").lower() == "true"
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", os.getenv("MAIL_USERNAME", ""))
    MAIL_TIMEOUT = float(os.getenv("MAIL_TIMEOUT", "20"))
    PASSWORD_RESET_TOKEN_MAX_AGE = 60 * 60
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    WTF_CSRF_ENABLED = False


class DevelopmentConfig(Config):
    DEBUG = True


def _send_smtp_message(app: Flask, message: Message) -> None:
    """Send one message with a bounded network timeout."""
    host = app.config["MAIL_SERVER"]
    port = app.config["MAIL_PORT"]
    timeout = app.config.get("MAIL_TIMEOUT", 20)
    sender = message.sender or app.config["MAIL_DEFAULT_SENDER"]
    connection = None
    try:
        if app.config.get("MAIL_USE_SSL"):
            connection = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            connection = smtplib.SMTP(host, port, timeout=timeout)
            if app.config.get("MAIL_USE_TLS"):
                connection.starttls()
        if app.config.get("MAIL_USERNAME") and app.config.get("MAIL_PASSWORD"):
            connection.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
        connection.sendmail(sender, message.recipients, message.as_string())
    finally:
        if connection is not None:
            connection.quit()


def get_config() -> type[Config]:
    environment = os.getenv("FLASK_ENV", "development").lower()
    return {
        "testing": TestingConfig,
        "development": DevelopmentConfig,
        "production": Config,
    }.get(environment, DevelopmentConfig)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    leads = db.relationship("Lead", back_populates="owner", lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    search_query = db.Column(db.String(255), nullable=True, index=True)
    search_command = db.Column(db.String(255), nullable=True)
    service_type = db.Column(db.String(80), nullable=False, default="website")
    business_name = db.Column(db.String(255), nullable=False)
    contact_name = db.Column(db.String(255))
    contact_role = db.Column(db.String(80))
    contact_email = db.Column(db.String(255))
    source = db.Column(db.String(40), nullable=False, default="Google Maps")
    phone = db.Column(db.String(80))
    address = db.Column(db.String(500))
    website = db.Column(db.String(500))
    score = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(80))
    issue_reason = db.Column(db.Text)
    pitch = db.Column(db.Text)
    pitch_subject = db.Column(db.String(255))
    pitch_body = db.Column(db.Text)
    lead_status = db.Column(db.String(20), nullable=False, default="New")
    outreach_step_1 = db.Column(db.Text)
    outreach_step_2 = db.Column(db.Text)
    outreach_step_3 = db.Column(db.Text)
    outreach_step_sent = db.Column(db.Integer, nullable=False, default=0)
    last_email_sent_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(
        db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    owner = db.relationship("User", back_populates="leads")
    technical_audit = db.relationship(
        "TechnicalAudit", back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )
    __table_args__ = (
        db.Index("ix_leads_search_command", "search_command"),
        db.Index("ix_leads_service_type", "service_type"),
        db.Index("ix_leads_lead_status", "lead_status"),
        db.Index("ix_leads_score", "score"),
    )


class TechnicalAudit(db.Model):
    __tablename__ = "technical_audits"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, unique=True)
    missing_https = db.Column(db.Boolean, nullable=False, default=False)
    missing_viewport = db.Column(db.Boolean, nullable=False, default=False)
    cms_detected = db.Column(db.String(120))
    pagespeed_estimate = db.Column(db.Integer)
    detected_issues = db.Column(db.Text)
    logo_missing = db.Column(db.Boolean, nullable=False, default=False)
    logo_quality = db.Column(db.String(40))
    analytics_detected = db.Column(db.String(120))
    meta_pixel_detected = db.Column(db.Boolean, nullable=False, default=False)
    thin_content = db.Column(db.Boolean, nullable=False, default=False)
    tech_signals = db.Column(db.Text)

    lead = db.relationship("Lead", back_populates="technical_audit")


def audit_website(url: str | None, timeout: float = 4, service_type: str = "website") -> dict:
    if not url:
        if service_type != "website":
            label = service_label(service_type)
            return {
                "status": f"No {label} Signals",
                "score": 95,
                "reason": f"No public website was found, so the business has no visible {label.lower()} presence.",
                "missing_https": True,
                "missing_viewport": True,
                "cms_detected": None,
                "pagespeed_estimate": 0,
                "detected_issues": "No website or logo detected",
                "logo_missing": service_type in {"logo", "branding"},
                "logo_quality": "missing" if service_type in {"logo", "branding"} else None,
                "analytics_detected": None,
                "meta_pixel_detected": False,
                "thin_content": True,
                "tech_signals": [],
            }
        return {
            "status": "No Website",
            "score": 95,
            "reason": "Business has no website listed on Google. Prime candidate for a new build.",
            "missing_https": True,
            "missing_viewport": True,
            "cms_detected": None,
            "pagespeed_estimate": 0,
            "detected_issues": "No website listed",
            "logo_missing": True,
            "logo_quality": "missing",
            "analytics_detected": None,
            "meta_pixel_detected": False,
            "thin_content": True,
            "tech_signals": [],
        }

    normalized_url = url if url.startswith(("http://", "https://")) else f"https://{url}"
    missing_https = urlparse(normalized_url).scheme != "https"
    try:
        response = requests.get(
            normalized_url,
            timeout=timeout,
            headers={"User-Agent": "AILeadGenBot/1.0 (+website-audit)"},
        )
        if response.status_code >= 400:
            return _unreachable_result(
                f"Website returns HTTP status {response.status_code}.", "Broken Site"
            )

        soup = BeautifulSoup(response.text, "html.parser")
        missing_viewport = soup.find(
            "meta", attrs={"name": re.compile(r"^viewport$", re.IGNORECASE)}
        ) is None
        cms_detected = _detect_cms(response.text, soup)
        analytics_detected, meta_pixel_detected, tech_signals = _detect_tech_signals(response.text)
        logo_audit = _audit_logo(response.text, soup) if service_type == "logo" else {
            "logo_missing": False,
            "logo_quality": None,
        }
        issues = []
        score = 30
        if missing_https:
            issues.append("Missing HTTPS")
            score += 20
        if missing_viewport:
            issues.append("Missing viewport meta tag")
            score += 30
        thin_content = len(soup.get_text(" ", strip=True).split()) < 300
        if thin_content:
            issues.append("Thin page content")
            score += 15

        if service_type == "logo":
            score = logo_audit["score"]
            issues = logo_audit["issues"]
            status = "Logo Opportunity" if score >= 50 else "Logo Detected"
            reason = ", ".join(issues) if issues else "A visible logo was detected."
        elif service_type != "website":
            service_audit = _audit_digital_service(service_type, response.text, soup)
            score = service_audit["score"]
            issues = service_audit["issues"]
            status = f"{service_label(service_type)} Opportunity" if score >= 50 else f"{service_label(service_type)} Detected"
            reason = ", ".join(issues) if issues else f"{service_label(service_type)} signals look healthy."
        else:
            status = "Outdated Website" if score >= 50 else "Modern Website"
            reason = ", ".join(issues) if issues else "Website appears modern and functional."
        return {
            "status": status,
            "score": score,
            "reason": reason,
            "missing_https": missing_https,
            "missing_viewport": missing_viewport,
            "cms_detected": cms_detected,
            "pagespeed_estimate": max(100 - score, 20),
            "detected_issues": reason,
            "analytics_detected": analytics_detected,
            "meta_pixel_detected": meta_pixel_detected,
            "thin_content": thin_content,
            "tech_signals": tech_signals,
            **logo_audit,
        }
    except requests.RequestException:
        return _unreachable_result(
            "Website timed out or refused connection.", "Unreachable / Offline"
        )


def _unreachable_result(reason: str, status: str) -> dict:
    return {
        "status": status,
        "score": 90,
        "reason": reason,
        "missing_https": False,
        "missing_viewport": False,
        "cms_detected": None,
        "pagespeed_estimate": 0,
        "detected_issues": reason,
        "logo_missing": False,
        "logo_quality": None,
        "analytics_detected": None,
        "meta_pixel_detected": False,
        "thin_content": True,
        "tech_signals": [],
    }


def _audit_logo(html: str, soup: BeautifulSoup) -> dict:
    logo_tokens = re.compile(r"logo|brand|wordmark|site-title", re.IGNORECASE)
    logo_images = [
        image for image in soup.find_all(["img", "svg"])
        if logo_tokens.search(" ".join(str(image.get(attribute, "")) for attribute in ("alt", "id", "class", "src", "title")))
    ]
    has_brand_link = any(
        logo_tokens.search(" ".join(str(link.get(attribute, "")) for attribute in ("aria-label", "class", "href", "title")))
        for link in soup.find_all("a")
    )
    if not logo_images and not has_brand_link:
        return {
            "score": 90,
            "issues": ["No visible logo or brand mark detected"],
            "logo_missing": True,
            "logo_quality": "missing",
        }
    if len(logo_images) == 1 and logo_images[0].name == "img" and not logo_images[0].get("alt"):
        return {
            "score": 65,
            "issues": ["Logo image has no accessible brand description"],
            "logo_missing": False,
            "logo_quality": "needs-review",
        }
    return {
        "score": 20,
        "issues": [],
        "logo_missing": False,
        "logo_quality": "detected",
    }


def _audit_digital_service(service_type: str, html: str, soup: BeautifulSoup) -> dict:
    text = soup.get_text(" ", strip=True)
    checks = {
        "seo": (
            [
                (soup.title is None, "Missing page title"),
                (soup.find("meta", attrs={"name": re.compile(r"^description$", re.IGNORECASE)}) is None, "Missing meta description"),
                (not soup.find("h1"), "Missing primary heading"),
                (not soup.find("link", attrs={"rel": lambda value: value and "canonical" in value}), "Missing canonical link"),
            ],
            "SEO",
        ),
        "social": (
            [(not re.search(r"facebook|instagram|linkedin|twitter|tiktok|youtube", html, re.IGNORECASE), "No social profile links detected")],
            "Social media",
        ),
        "branding": (
            [(_audit_logo(html, soup)["logo_missing"], "No visible logo or brand mark detected"), (len(text) < 300, "Brand story has thin on-page content")],
            "Branding",
        ),
        "accessibility": (
            [(any(not image.get("alt") for image in soup.find_all("img")), "Images are missing accessible alt text"), (not soup.find("main"), "Missing main content landmark")],
            "Accessibility",
        ),
        "content": ([(len(text) < 500, "Thin website content")], "Content"),
        "performance": ([(len(html) > 500000, "Large page payload"), (len(soup.find_all("script")) > 20, "Heavy script footprint")], "Performance"),
        "ecommerce": ([(not soup.find(attrs={"itemtype": re.compile(r"Product", re.IGNORECASE)}), "No product structured data detected"), (not re.search(r"cart|shop|product|checkout", text, re.IGNORECASE), "No clear commerce journey detected")], "E-commerce"),
    }
    service_checks, _ = checks.get(service_type, ([(len(text) < 300, f"Limited {service_label(service_type).lower()} signals")], service_label(service_type)))
    issues = [message for failed, message in service_checks if failed]
    return {"score": min(95, 30 + len(issues) * 25), "issues": issues}


def service_label(service_type: str) -> str:
    return {
        "logo": "Logo design",
        "seo": "SEO",
        "social": "Social media",
        "branding": "Branding",
        "accessibility": "Accessibility",
        "content": "Content",
        "performance": "Performance",
        "ecommerce": "E-commerce",
        "website": "Website",
    }.get(service_type, service_type.replace("_", " ").title())


def _detect_cms(html: str, soup: BeautifulSoup) -> str | None:
    generator = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.IGNORECASE)})
    if generator and generator.get("content"):
        return generator["content"][:120]
    signatures = {
        "WordPress": r"wp-content|wp-includes",
        "Shopify": "cdn.shopify.com",
        "Wix": "wix.com",
        "Squarespace": "squarespace.com",
        "Webflow": "webflow",
    }
    for name, signature in signatures.items():
        if re.search(signature, html, re.IGNORECASE):
            version = re.search(rf"{name}[^\d]*(\d+(?:\.\d+)+)", html, re.IGNORECASE)
            return f"{name} {version.group(1)}" if version else name
    return None


def _detect_tech_signals(html: str) -> tuple[str | None, bool, list[str]]:
    analytics = []
    if re.search(r"google-analytics|gtag\(|googletagmanager\.com|google_tag_manager", html, re.IGNORECASE):
        analytics.append("Google Analytics / Tag Manager")
    meta_pixel = bool(re.search(r"connect\.facebook\.net|fbq\(|facebook pixel", html, re.IGNORECASE))
    if meta_pixel:
        analytics.append("Meta Pixel")
    return ", ".join(analytics) or None, meta_pixel, analytics


ProgressCallback = Callable[[int, int], None]


def search_google_businesses(query: str, location: str = "Chicago, Illinois") -> list[dict]:
    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_KEY is not configured")

    requests_to_make = {
        "maps": {"engine": "google_maps", "q": query, "location": location, "z": 12, "api_key": api_key},
        "organic": {"engine": "google", "q": query, "location": location, "api_key": api_key, "num": 20},
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(GoogleSearch(params).get_dict): source
            for source, params in requests_to_make.items()
        }
        responses = {source: future.result() for future, source in ((future, futures[future]) for future in futures)}

    maps_results = responses["maps"]
    organic_results = responses["organic"]
    if maps_results.get("error"):
        raise RuntimeError(f"SerpAPI Maps search failed: {maps_results['error']}")
    if organic_results.get("error"):
        raise RuntimeError(f"Google search failed: {organic_results['error']}")

    businesses = [
        {"name": item.get("title", "Unknown Business"), "website": item.get("website"), "phone": item.get("phone", "N/A"), "address": item.get("address", "N/A"), "source": "Google Maps"}
        for item in maps_results.get("local_results", [])
    ]
    businesses.extend(
        {"name": result.get("title", "Unknown Business"), "website": result.get("link"), "phone": "N/A", "address": location, "source": "Google Search"}
        for result in organic_results.get("organic_results", [])
    )
    return _deduplicate_businesses(businesses)


def _deduplicate_businesses(businesses: list[dict]) -> list[dict]:
    unique = {}
    for business in businesses:
        key = _lead_key(business.get("name"), business.get("website"))
        if key not in unique:
            unique[key] = business
        elif unique[key].get("source") != "Google Maps":
            unique[key]["source"] = "Google Maps + Google Search"
    return list(unique.values())


def enrich_contact(business: dict, timeout: float = 10) -> dict:
    """Find publicly listed contact details without guessing private identities."""
    website = business.get("website")
    if not website:
        return {"contact_name": None, "contact_role": None, "contact_email": None}
    base_url = website if website.startswith(("http://", "https://")) else f"https://{website}"
    pages = [base_url]
    parsed = urlparse(base_url)
    for path in ("/contact", "/contact-us", "/about", "/about-us"):
        pages.append(f"{parsed.scheme}://{parsed.netloc}{path}")
    email_pattern = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
    phone_pattern = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
    emails = []
    contact_name = None
    contact_role = None
    for page in pages:
        try:
            response = requests.get(
                page,
                timeout=timeout,
                headers={"User-Agent": "AILeadGenBot/1.0 (+public-contact-discovery)"},
            )
            if response.status_code >= 400:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(" ", strip=True)
            emails.extend(email_pattern.findall(response.text))
            if business.get("phone") in {None, "", "N/A"}:
                phone = phone_pattern.search(text)
                if phone:
                    business["phone"] = phone.group(0).strip()
            if contact_name is None:
                match = re.search(
                    r"(?:owner|founder|manager|director|contact person)\s*[:\-]\s*([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3})",
                    text,
                )
                if match:
                    contact_name = match.group(1).strip()
                    role_match = re.search(r"(owner|founder|manager|director|contact person)", match.group(0), re.IGNORECASE)
                    contact_role = role_match.group(1).title() if role_match else None
        except requests.RequestException:
            continue
    clean_emails = [email for email in dict.fromkeys(emails) if not email.lower().endswith((".png", ".jpg"))]
    return {
        "contact_name": contact_name,
        "contact_role": contact_role,
        "contact_email": clean_emails[0] if clean_emails else None,
    }


def run_prospector(
    search_query: str,
    progress_callback: ProgressCallback | None = None,
    timeout: float = 10,
) -> list[dict]:
    service_type, discovery_query = parse_search_intent(search_query)
    cache_key = " ".join(search_query.casefold().split())
    cached = _get_cached_search(cache_key)
    if cached is not None:
        cached_leads = copy.deepcopy(cached)
        if progress_callback:
            progress_callback(len(cached_leads), len(cached_leads))
        return cached_leads

    businesses = search_google_businesses(discovery_query)
    total = len(businesses)
    if progress_callback:
        progress_callback(0, total)

    qualified_leads = []

    def process_business(business: dict) -> dict | None:
        contact = enrich_contact(business, timeout=timeout)
        audit = audit_website(business.get("website"), timeout=timeout, service_type=service_type)
        if audit["score"] >= 50:
            name = business["name"]
            address = business.get("address", "N/A")
            proposal = generate_agency_pitch(
                name,
                _extract_city(address),
                service_type,
                {
                    "industry": _infer_industry(discovery_query),
                    "contact_name": contact.get("contact_name"),
                    "logo_missing": audit.get("logo_missing", False),
                    "reason": audit["reason"],
                },
            )
            return {
                    "business_name": name,
                    "service_type": service_type,
                    "contact_name": contact["contact_name"],
                    "contact_role": contact["contact_role"],
                    "contact_email": contact["contact_email"],
                    "source": business.get("source", "Google Maps"),
                    "website": business.get("website"),
                    "phone": business.get("phone", "N/A"),
                    "address": address,
                    "status": audit["status"],
                    "score": audit["score"],
                    "reason": audit["reason"],
                    "pitch": proposal["pitch_body"],
                    "pitch_subject": proposal["pitch_subject"],
                    "pitch_body": proposal["pitch_body"],
                    "audit": audit,
                }
        return None

    with ThreadPoolExecutor(max_workers=min(10, max(1, len(businesses)))) as executor:
        futures = [executor.submit(process_business, business) for business in businesses]
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result:
                qualified_leads.append(result)
            if progress_callback:
                progress_callback(index, total)
    _set_cached_search(cache_key, qualified_leads)
    return qualified_leads


def build_proposal(
    name: str,
    address: str,
    reason: str,
    service_type: str,
    contact_name: str | None = None,
    website: str | None = None,
) -> str:
    """Backward-compatible wrapper around the reusable proposal writer."""
    return generate_agency_pitch(
        name,
        _extract_city(address),
        service_type,
        {"contact_name": contact_name, "reason": reason},
    )["pitch_body"]


def generate_agency_pitch(
    business_name: str,
    city: str,
    service_type: str | None,
    context: dict | None = None,
) -> dict[str, str]:
    """Compose an evidence-led proposal from the lead's context."""
    context = context or {}
    service = service_type or "website"
    label = service_label(service).lower()
    industry = context.get("industry") or "local businesses"
    greeting = context.get("contact_name") or f"Team at {business_name}"
    reason = context.get("reason") or (
        "The site lacks mobile optimization and takes longer than average to load, "
        "which can hurt Google rankings and drive away mobile visitors."
    )
    outcomes = {
        "logo": "stronger brand recognition and a more professional first impression across digital and print touchpoints",
        "seo": "better local search visibility and a clearer path from a Google search to a call or enquiry",
        "social": "a consistent social presence that keeps the business visible between visits",
        "branding": "a recognizable identity that builds trust before a customer decides where to spend",
        "website": "a faster, clearer digital experience that helps more visitors take the next step",
    }
    deliverables = {
        "logo": "a primary logo asset, responsive variations, color/type guidance, and practical usage examples",
        "seo": "a prioritized local SEO review, on-page fixes, and a plan for improving high-intent searches",
        "social": "profile positioning, content pillars, and a practical first-month publishing plan",
        "branding": "a focused identity direction, messaging guidance, and core brand touchpoints",
        "website": "a prioritized UX and performance review with practical improvements for mobile visitors",
    }
    industry_outcomes = {
        "restaurant": "more guests, table reservations, the online menu, and walk-ins",
        "restaurants": "more guests, table reservations, the online menu, and walk-ins",
        "dentist": "more appointment requests and confidence from patients comparing local providers",
        "dentists": "more appointment requests and confidence from patients comparing local providers",
        "real estate": "more qualified property enquiries and trust from buyers and sellers",
    }
    subject = f"Quick {label} note regarding {business_name} ({city})"
    outcome = next(
        (value for key, value in industry_outcomes.items() if key in industry.casefold()),
        outcomes.get(service, f"a more effective {label} experience"),
    )
    body = (
        f"Hi {greeting},\n\n"
        f"I came across {business_name} while looking into {label} for {industry} businesses in {city}.\n\n"
        "While reviewing your public digital presence, I noticed a key area where small improvements could be costing you potential customers:\n\n"
        f"- Key Issue Identified: {reason}\n\n"
        f"Addressing this could support {outcome}.\n\n"
        f"At Quantum Graphics, we specialize in helping businesses turn this kind of opportunity into a clear, practical improvement. I would recommend starting with {deliverables.get(service, f'a focused {label} review with prioritized fixes and implementation guidance')}.\n\n"
        "Would you be open to a quick 5-minute chat this week? Alternatively, I can send over a 2-minute video breakdown showing your team the opportunity step-by-step.\n\n"
        "Best regards,\n\nNabeel Naseem\nQuantum Graphics"
    )
    return {"pitch_subject": subject, "pitch_body": body}


def _infer_industry(query: str) -> str:
    stop_words = {"in", "near", "around", "for", "the", "a", "an"}
    words = [word for word in re.split(r"\s+", query.strip()) if word.casefold() not in stop_words]
    return " ".join(words[:3]) or "local businesses"


def _extract_city(address: str | None) -> str:
    if not address or address in {"N/A", "Unknown"}:
        return "your area"
    parts = [part.strip() for part in address.split(",") if part.strip()]
    return parts[-2] if len(parts) >= 2 else parts[0]


def build_outreach_sequence(
    name: str,
    address: str,
    reason: str,
    service_type: str,
    contact_name: str | None = None,
    website: str | None = None,
) -> dict[str, str]:
    service = service_label(service_type).lower()
    greeting = contact_name or f"the {name} team"
    proposal = build_proposal(name, address, reason, service_type, contact_name, website)
    return {
        "step_1": proposal,
        "step_2": (
            f"Subject: Re: A {service} improvement proposal for {name}\n\n"
            f"Hello {greeting},\n\n"
            f"I wanted to follow up on the proposal I sent after reviewing {name}'s public presence. The main opportunity I noted was {reason.lower()}.\n\n"
            f"I can turn that observation into a concise before-and-after plan for your {service} experience, including priorities, deliverables, and a realistic timeline. There is no obligation to proceed; the aim is to make the opportunity easy to evaluate.\n\n"
            f"Would you like me to send that outline?\n\nBest,\nYour Name"
        ),
        "step_3": (
            f"Subject: Closing the loop on {name}'s {service} opportunity\n\n"
            f"Hello {greeting},\n\n"
            f"I will close the loop for now. I reached out because {reason.lower()} may be worth reviewing as part of {name}'s next digital improvement cycle.\n\n"
            f"If this becomes a priority, reply with \"outline\" and I will send the short proposal with the recommended scope and next steps.\n\nBest,\nYour Name"
        ),
    }


def parse_search_intent(search_query: str) -> tuple[str, str]:
    """Separate the market query from the service the user wants to sell."""
    normalized = " ".join(search_query.split())
    service_patterns = [
        ("website", r"(?:having|with|that have|needing|need|looking for)?\s*(?:a\s+)?(?:poor|bad|weak|outdated|missing|no)?\s*(?:website|web design|web presence)"),
        ("logo", r"(?:having|with|that have|needing|need|looking for)?\s*(?:a\s+)?(?:poor|bad|weak|outdated|no|missing)?\s*logo(?:\s+design)?"),
        ("seo", r"(?:having|with|that have|needing|need|looking for)?\s*(?:poor|bad|weak|missing|no|outdated)?\s*(?:seo|search engine optimization|local seo)"),
        ("social", r"(?:having|with|that have|needing|need|looking for)?\s*(?:poor|bad|weak|missing|no|outdated)?\s*(?:social media|social presence|social profiles)"),
        ("branding", r"(?:having|with|that have|needing|need|looking for)?\s*(?:poor|bad|weak|missing|no|outdated)?\s*(?:branding|brand identity)"),
        ("accessibility", r"(?:having|with|that have|needing|need|looking for)?\s*(?:poor|bad|weak|missing|no|outdated)?\s*(?:accessibility|accessible design)"),
        ("content", r"(?:having|with|that have|needing|need|looking for)?\s*(?:poor|bad|weak|missing|no|outdated)?\s*(?:content|copywriting)"),
        ("performance", r"(?:having|with|that have|needing|need|looking for)?\s*(?:poor|bad|weak|missing|no|outdated)?\s*(?:performance|slow website|page speed)"),
        ("ecommerce", r"(?:having|with|that have|needing|need|looking for)?\s*(?:poor|bad|weak|missing|no|outdated)?\s*(?:e-commerce|ecommerce|online store)"),
    ]
    for service_type, pattern in service_patterns:
        service_pattern = re.compile(rf"\b{pattern}\b", re.IGNORECASE)
        if service_pattern.search(normalized):
            discovery_query = service_pattern.sub("", normalized)
            discovery_query = re.sub(r"\s+", " ", discovery_query).strip(" ,")
            discovery_query = re.sub(r"\s+(?:having|with|that have|needing|need|looking for)(?:\s+(?:a|an))?$", "", discovery_query, flags=re.IGNORECASE).strip()
            return service_type, discovery_query
    return "website", normalized


landing_bp = Blueprint("landing", __name__)
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
prospector_bp = Blueprint("prospector", __name__)
crm_bp = Blueprint("crm", __name__)
_tasks: dict[str, dict] = {}
_tasks_lock = Lock()
_database_save_lock = Lock()
_search_cache: dict[str, tuple[float, list[dict]]] = {}
_search_cache_lock = Lock()
_ALLOWED_STATUSES = {"New", "Contacted", "Closed"}


def _get_cached_search(cache_key: str) -> list[dict] | None:
    with _search_cache_lock:
        cached = _search_cache.get(cache_key)
        if cached is None:
            return None
        created_at, results = cached
        if time.time() - created_at >= Config.SEARCH_CACHE_TTL:
            del _search_cache[cache_key]
            return None
        return results


def _set_cached_search(cache_key: str, results: list[dict]) -> None:
    with _search_cache_lock:
        _search_cache[cache_key] = (time.time(), copy.deepcopy(results))


@landing_bp.get("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("prospector.index"))
    return render_template("landing.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("prospector.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            return render_template("auth/register.html", error="Enter a valid email address."), 400
        if len(password) < 8:
            return render_template("auth/register.html", error="Password must be at least 8 characters."), 400
        if db.session.scalar(db.select(User).where(User.email == email)):
            return render_template("auth/register.html", error="An account with that email already exists."), 409
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for("prospector.index"))
    return render_template("auth/register.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("prospector.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db.session.scalar(db.select(User).where(User.email == email))
        if user is None or not user.check_password(request.form.get("password", "")):
            return render_template("auth/login.html", error="Email or password is incorrect."), 401
        login_user(user)
        return redirect(url_for("prospector.index"))
    return render_template("auth/login.html", reset_success=request.args.get("reset") == "success")


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db.session.scalar(db.select(User).where(User.email == email))
        if user:
            serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
            token = serializer.dumps({"user_id": user.id}, salt="password-reset")
            reset_url = url_for("auth.reset_password", token=token, _external=True)
            try:
                _send_smtp_message(
                    current_app._get_current_object(),
                    Message(
                        subject="Reset your Signal password",
                        recipients=[user.email],
                        body=(
                            "We received a request to reset your Signal password.\n\n"
                            f"Reset it here: {reset_url}\n\n"
                            "This link expires in one hour. If you did not request this, you can ignore this email."
                        ),
                    )
                )
            except Exception:
                current_app.logger.exception("Password reset email delivery failed")
        return render_template(
            "auth/forgot_password.html",
            sent=True,
        )
    return render_template("auth/forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        payload = serializer.loads(
            token,
            salt="password-reset",
            max_age=current_app.config.get("PASSWORD_RESET_TOKEN_MAX_AGE", 60 * 60),
        )
    except (BadSignature, SignatureExpired):
        return render_template(
            "auth/reset_password.html",
            error="This reset link is invalid or expired. Request a new one.",
            invalid_token=True,
        ), 400

    user = db.session.get(User, payload.get("user_id"))
    if user is None:
        return render_template(
            "auth/reset_password.html",
            error="This reset link is invalid or expired. Request a new one.",
            invalid_token=True,
        ), 400
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            return render_template(
                "auth/reset_password.html",
                error="Password must be at least 8 characters.",
            ), 400
        user.set_password(password)
        db.session.commit()
        logout_user()
        return redirect(url_for("auth.login", reset="success"))
    return render_template("auth/reset_password.html")


@auth_bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("landing.landing"))


@prospector_bp.get("/prospect")
@login_required
def index():
    return render_template("prospector/index.html")


@prospector_bp.post("/api/search")
@login_required
def start_search():
    payload = request.get_json(silent=True) or request.form
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "A search query is required."}), 400

    task_id = uuid4().hex
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "queued",
            "progress": 0,
            "found_leads": [],
            "skipped_count": 0,
            "error": None,
        }

    user_id = current_user.id if current_user.is_authenticated else None
    app = current_app._get_current_object()
    thread = Thread(target=_run_search, args=(app, task_id, query, user_id), daemon=True)
    thread.start()
    return jsonify({"task_id": task_id}), 202


@prospector_bp.get("/api/task-status/<task_id>")
@login_required
def task_status(task_id: str):
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        return jsonify({"error": "Task not found."}), 404
    return jsonify(task)


@crm_bp.post("/api/leads/<int:lead_id>/send-email")
@login_required
def send_lead_email(lead_id: int):
    lead = db.one_or_404(db.select(Lead).where(Lead.id == lead_id, Lead.user_id == current_user.id))
    payload = request.get_json(silent=True) or {}
    recipient = (payload.get("email") or lead.contact_email or "").strip()
    step = int(payload.get("step", 1))
    if step not in {1, 2, 3}:
        return jsonify({"error": "Email step must be 1, 2, or 3."}), 400
    next_step = (lead.outreach_step_sent or 0) + 1
    if step != next_step:
        return jsonify({"error": f"Step {step} is not available. Send step {next_step} next."}), 409
    if not recipient or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
        return jsonify({"error": "A valid public contact email is required."}), 400
    if not current_app.config.get("MAIL_SERVER") or not current_app.config.get("MAIL_DEFAULT_SENDER"):
        return jsonify({"error": "SMTP is not configured. Add MAIL_SERVER and MAIL_DEFAULT_SENDER to .env."}), 503

    subject = payload.get("subject") or lead.pitch_subject or f"Quick {service_label(lead.service_type).lower()} note regarding {lead.business_name}"
    body = payload.get("body") or getattr(lead, f"outreach_step_{step}")
    try:
        _send_smtp_message(current_app._get_current_object(), Message(subject=subject, recipients=[recipient], body=body))
    except Exception as exc:
        current_app.logger.exception("Email delivery failed for lead %s", lead_id)
        return jsonify({"error": f"Email delivery failed: {exc}"}), 502
    lead.outreach_step_sent = max(lead.outreach_step_sent or 0, step)
    lead.last_email_sent_at = datetime.now(timezone.utc)
    if step == 1:
        lead.lead_status = "Contacted"
    db.session.commit()
    return jsonify({"status": "sent", "step": step, "recipient": recipient})


def _run_search(app, task_id: str, query: str, user_id: int | None) -> None:
    try:
        _update_task(task_id, status="searching", progress=5)

        def report(processed: int, total: int) -> None:
            progress = 0 if total == 0 else round((processed / total) * 100)
            _update_task(task_id, progress=progress)

        with app.app_context():
            leads = run_prospector(
                query,
                progress_callback=report,
                timeout=app.config["SCRAPER_TIMEOUT"],
            )
            with _database_save_lock:
                existing_keys = {
                    _lead_key(name, website)
                    for name, website in db.session.execute(
                        db.select(Lead.business_name, Lead.website).where(Lead.user_id == user_id)
                    ).all()
                }
                saved_leads = []
                skipped_count = 0
                for lead_data in leads:
                    lead_key = _lead_key(lead_data["business_name"], lead_data["website"])
                    if lead_key in existing_keys:
                        skipped_count += 1
                        continue

                    audit_data = lead_data["audit"]
                    sequence = build_outreach_sequence(
                        lead_data["business_name"],
                        lead_data["address"],
                        lead_data["reason"],
                        lead_data["service_type"],
                        lead_data.get("contact_name"),
                        lead_data.get("website"),
                    )
                    lead = Lead(
                        user_id=user_id,
                        search_query=query,
                        search_command=query,
                        service_type=lead_data["service_type"],
                        business_name=lead_data["business_name"],
                        contact_name=lead_data["contact_name"],
                        contact_role=lead_data["contact_role"],
                        contact_email=lead_data["contact_email"],
                        source=lead_data["source"],
                        phone=lead_data["phone"],
                        address=lead_data["address"],
                        website=lead_data["website"],
                        score=lead_data["score"],
                        status=lead_data["status"],
                        issue_reason=lead_data["reason"],
                        pitch=lead_data["pitch"],
                        pitch_subject=lead_data.get("pitch_subject"),
                        pitch_body=lead_data.get("pitch_body", lead_data["pitch"]),
                        outreach_step_1=sequence["step_1"],
                        outreach_step_2=sequence["step_2"],
                        outreach_step_3=sequence["step_3"],
                        created_at=datetime.now(timezone.utc),
                    )
                    lead.technical_audit = TechnicalAudit(
                        missing_https=audit_data["missing_https"],
                        missing_viewport=audit_data["missing_viewport"],
                        cms_detected=audit_data["cms_detected"],
                        pagespeed_estimate=audit_data["pagespeed_estimate"],
                        detected_issues=audit_data["detected_issues"],
                        logo_missing=audit_data.get("logo_missing", False),
                        logo_quality=audit_data.get("logo_quality"),
                        analytics_detected=audit_data.get("analytics_detected"),
                        meta_pixel_detected=audit_data.get("meta_pixel_detected", False),
                        thin_content=audit_data.get("thin_content", False),
                        tech_signals=json.dumps(audit_data.get("tech_signals", [])),
                    )
                    db.session.add(lead)
                    db.session.flush()
                    existing_keys.add(lead_key)
                    saved_leads.append(_lead_payload(lead_data, lead.id))
                db.session.commit()

        _update_task(
            task_id,
            status="completed",
            progress=100,
            found_leads=saved_leads,
            skipped_count=skipped_count,
        )
    except Exception as exc:
        app.logger.exception("Prospecting task %s failed", task_id)
        _update_task(task_id, status="failed", error=str(exc))


def _update_task(task_id: str, **updates) -> None:
    with _tasks_lock:
        if task_id in _tasks:
            _tasks[task_id].update(updates)


def _lead_key(business_name: str | None, website: str | None) -> tuple[str, str]:
    return (
        " ".join((business_name or "").casefold().split()),
        (website or "").rstrip("/").casefold(),
    )


def _lead_payload(lead: dict, lead_id: int | None = None) -> dict:
    return {
        "id": lead_id,
        "service_type": lead.get("service_type", "website"),
        "pitch_subject": lead.get("pitch_subject"),
        "pitch_body": lead.get("pitch_body", lead.get("pitch")),
        "contact_name": lead.get("contact_name"),
        "contact_role": lead.get("contact_role"),
        "contact_email": lead.get("contact_email"),
        "source": lead.get("source", "Google Maps"),
        **{
            key: lead[key]
            for key in ("business_name", "website", "phone", "address", "status", "score", "reason", "pitch")
        },
    }


@crm_bp.get("/leads")
@login_required
def dashboard():
    all_leads = db.session.execute(
        db.select(Lead).where(Lead.user_id == current_user.id).order_by(Lead.created_at.desc())
    ).scalars().all()
    selected_search = request.args.get("search", "").strip()
    industry_filter = request.args.get("industry", "").strip()

    leads = [
        lead
        for lead in all_leads
        if (not selected_search or (lead.search_query or "") == selected_search)
        and (
            not industry_filter
            or industry_filter.casefold() in (lead.search_query or "").casefold()
        )
    ]
    grouped_leads = {}
    for lead in all_leads:
        group_name = lead.search_query or "Earlier searches"
        grouped_leads.setdefault(group_name, []).append(lead)
    outreach_count = sum(1 for lead in all_leads if (lead.outreach_step_sent or 0) > 0)
    outreach_rate = round((outreach_count / len(all_leads)) * 100) if all_leads else 0
    conversion_count = sum(1 for lead in all_leads if lead.lead_status == "Closed")
    conversion_rate = round((conversion_count / len(all_leads)) * 100) if all_leads else 0
    return render_template(
        "crm/dashboard.html",
        leads=leads,
        all_leads=all_leads,
        grouped_leads=grouped_leads,
        selected_search=selected_search,
        industry_filter=industry_filter,
        page=1,
        per_page=max(len(leads), 1),
        total_filtered=len(leads),
        service_filter="",
        conversion_rate=conversion_rate,
        outreach_rate=outreach_rate,
    )


@crm_bp.get("/leads/<int:lead_id>")
@login_required
def lead_detail(lead_id: int):
    lead = db.one_or_404(db.select(Lead).where(Lead.id == lead_id, Lead.user_id == current_user.id))
    next_step = min((lead.outreach_step_sent or 0) + 1, 3)
    return render_template(
        "crm/lead_detail.html",
        lead=lead,
        audit=lead.technical_audit,
        next_step=next_step,
    )


@crm_bp.post("/update-status/<int:lead_id>")
@login_required
def update_status(lead_id: int):
    new_status = request.form.get("status", "")
    if new_status in _ALLOWED_STATUSES:
        lead = db.one_or_404(db.select(Lead).where(Lead.id == lead_id, Lead.user_id == current_user.id))
        lead.lead_status = new_status
        db.session.commit()
    return redirect(url_for("crm.dashboard"))


@crm_bp.get("/export-csv")
@login_required
def export_csv():
    leads = db.session.execute(
        db.select(Lead).where(Lead.user_id == current_user.id).order_by(Lead.created_at.desc())
    ).scalars().all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "ID", "Business Name", "Contact Name", "Contact Role", "Email", "Phone", "Address", "Website", "Source", "Score",
            "Issue Tier", "Detected Issues", "Pitch", "Status", "Date Found",
        ]
    )
    for lead in leads:
        writer.writerow(
            [
                lead.id, lead.business_name, lead.contact_name, lead.contact_role, lead.contact_email,
                lead.phone, lead.address, lead.website, lead.source, lead.score, lead.status, lead.issue_reason, lead.pitch,
                lead.lead_status, lead.created_at.isoformat() if lead.created_at else "",
            ]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ai_leads.csv"},
    )


def create_app(config_class=None) -> Flask:
    app = Flask(__name__, template_folder="app/templates")
    app.config.from_object(config_class or get_config())
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    mail.init_app(app)
    app.register_blueprint(landing_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(prospector_bp)
    app.register_blueprint(crm_bp)
    with app.app_context():
        db.create_all()
        _ensure_legacy_columns()
        _refresh_legacy_outreach()

    @app.after_request
    def compress_response(response):
        accepted = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accepted.lower() or response.direct_passthrough or response.status_code in {204, 304}:
            return response
        if response.headers.get("Content-Encoding") or not response.get_data():
            return response
        compressed = gzip.compress(response.get_data(), compresslevel=6)
        if len(compressed) >= len(response.get_data()):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
        response.headers["Content-Length"] = str(len(compressed))
        return response

    @app.context_processor
    def inject_csrf_token():
        def csrf_token():
            token = session.get("csrf_token")
            if token is None:
                token = uuid4().hex
                session["csrf_token"] = token
            return token

        return {"csrf_token": csrf_token}

    @app.before_request
    def validate_csrf_token():
        if request.method == "POST" and request.endpoint not in {"prospector.start_search", "crm.send_lead_email"}:
            expected = session.get("csrf_token")
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if expected and supplied != expected:
                return jsonify({"error": "Invalid CSRF token."}), 400
    return app


def _ensure_legacy_columns() -> None:
    """Keep the local SQLite database usable after adding lightweight fields."""
    if db.engine.dialect.name != "sqlite":
        return
    columns = {column["name"] for column in inspect(db.engine).get_columns("leads")}
    if "search_query" not in columns:
        db.session.execute(db.text("ALTER TABLE leads ADD COLUMN search_query VARCHAR(255)"))
        db.session.commit()
    if "service_type" not in columns:
        db.session.execute(db.text("ALTER TABLE leads ADD COLUMN service_type VARCHAR(80) DEFAULT 'website'"))
        db.session.commit()
    for column_name, column_type in (
        ("search_command", "VARCHAR(255)"),
        ("outreach_step_1", "TEXT"),
        ("outreach_step_2", "TEXT"),
        ("outreach_step_3", "TEXT"),
        ("outreach_step_sent", "INTEGER DEFAULT 0"),
        ("last_email_sent_at", "DATETIME"),
        ("pitch_subject", "VARCHAR(255)"),
        ("pitch_body", "TEXT"),
    ):
        if column_name not in columns:
            db.session.execute(db.text(f"ALTER TABLE leads ADD COLUMN {column_name} {column_type}"))
            db.session.commit()
    for column_name, column_type in (
        ("contact_name", "VARCHAR(255)"),
        ("contact_role", "VARCHAR(80)"),
        ("contact_email", "VARCHAR(255)"),
        ("source", "VARCHAR(40) DEFAULT 'Google Maps'"),
    ):
        if column_name not in columns:
            db.session.execute(db.text(f"ALTER TABLE leads ADD COLUMN {column_name} {column_type}"))
            db.session.commit()
    audit_columns = {column["name"] for column in inspect(db.engine).get_columns("technical_audits")}
    if "logo_missing" not in audit_columns:
        db.session.execute(db.text("ALTER TABLE technical_audits ADD COLUMN logo_missing BOOLEAN DEFAULT 0"))
        db.session.commit()
    if "logo_quality" not in audit_columns:
        db.session.execute(db.text("ALTER TABLE technical_audits ADD COLUMN logo_quality VARCHAR(40)"))
        db.session.commit()
    for column_name, column_type in (
        ("analytics_detected", "VARCHAR(120)"),
        ("meta_pixel_detected", "BOOLEAN DEFAULT 0"),
        ("thin_content", "BOOLEAN DEFAULT 0"),
        ("tech_signals", "TEXT"),
    ):
        if column_name not in audit_columns:
            db.session.execute(db.text(f"ALTER TABLE technical_audits ADD COLUMN {column_name} {column_type}"))
            db.session.commit()
    for index_name, column_name in (
        ("ix_leads_search_command", "search_command"),
        ("ix_leads_service_type", "service_type"),
        ("ix_leads_lead_status", "lead_status"),
        ("ix_leads_score", "score"),
    ):
        db.session.execute(db.text(f"CREATE INDEX IF NOT EXISTS {index_name} ON leads ({column_name})"))
    db.session.commit()


def _refresh_legacy_outreach() -> None:
    """Upgrade only records still using the original generic outreach copy."""
    legacy_leads = db.session.execute(
        db.select(Lead).where(
            or_(
                Lead.pitch.like("Hi % team, I noticed your business%"),
                Lead.outreach_step_1.like("Hi % team, I noticed your business%"),
            )
        )
    ).scalars().all()
    for lead in legacy_leads:
        service_type = lead.service_type or "website"
        sequence = build_outreach_sequence(
            lead.business_name,
            lead.address or "your area",
            lead.issue_reason or "The public website has an opportunity for improvement.",
            service_type,
            lead.contact_name,
            lead.website,
        )
        lead.pitch = sequence["step_1"]
        lead.outreach_step_1 = sequence["step_1"]
        lead.outreach_step_2 = sequence["step_2"]
        lead.outreach_step_3 = sequence["step_3"]
    if legacy_leads:
        db.session.commit()


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


app = create_app()


if __name__ == "__main__":
    app.run()
