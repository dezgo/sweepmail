"""
Sweepmail — Gmail Inbox Cleaner
"""

import os
import re
import secrets
import threading
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from flask import Flask, redirect, render_template, request, session, url_for, jsonify
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["PREFERRED_URL_SCHEME"] = "https"

APP_DIR = Path(__file__).parent
CREDS_PATH = APP_DIR / "credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

# Only allow HTTP in local dev — production runs behind HTTPS
if os.environ.get("FLASK_ENV") == "development":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# In-memory job store: job_id -> {status, progress, total, result, error}
jobs = {}

# ---------------------------------------------------------------------------
# Categorisation rules
# ---------------------------------------------------------------------------

NEWSLETTER_PATTERNS = [
    r"unsubscribe",
    r"email preferences",
    r"manage your subscription",
    r"opt.out",
    r"view in browser",
    r"view this email",
]

JUNK_SENDER_KEYWORDS = [
    "noreply", "no-reply", "marketing", "promo", "deals", "offers",
    "newsletter", "digest", "notification", "alert", "mailer-daemon",
]

SOCIAL_DOMAINS = [
    "facebookmail.com", "twitter.com", "x.com", "linkedin.com",
    "instagram.com", "reddit.com", "discord.com", "slack.com",
    "meetup.com", "nextdoor.com", "tiktok.com", "pinterest.com",
    "quora.com", "tumblr.com", "youtube.com",
]

SHOPPING_DOMAINS = [
    "amazon.com", "ebay.com", "walmart.com", "target.com",
    "bestbuy.com", "etsy.com", "aliexpress.com", "shopify.com",
    "stripe.com", "paypal.com", "venmo.com", "squareup.com",
]

CATEGORY_COLORS = {
    "Newsletter": "#f59e0b",
    "Automated": "#ef4444",
    "Social": "#3b82f6",
    "Shopping": "#8b5cf6",
    "Calendar": "#10b981",
    "Personal": "#6b7280",
}


def get_domain(email_addr: str) -> str:
    match = re.search(r"@([\w.-]+)", email_addr)
    return match.group(1).lower() if match else ""


def extract_name_and_email(sender: str) -> tuple:
    """Extract display name and email from a From header."""
    match = re.match(r'"?([^"<]*)"?\s*<?([^>]*)>?', sender)
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip()
        return name or email, email
    return sender, sender


def categorize_email(sender: str, subject: str, snippet: str, headers: dict) -> str:
    sender_lower = sender.lower()
    domain = get_domain(sender_lower)
    text = f"{subject} {snippet}".lower()

    if any(d in domain for d in SOCIAL_DOMAINS):
        return "Social"
    if any(d in domain for d in SHOPPING_DOMAINS):
        return "Shopping"
    if re.search(r"(order confirm|shipping|delivered|receipt|invoice)", text):
        return "Shopping"

    list_unsubscribe = headers.get("List-Unsubscribe", "")
    if list_unsubscribe:
        return "Newsletter"
    if any(re.search(p, text) for p in NEWSLETTER_PATTERNS):
        return "Newsletter"

    if any(kw in sender_lower for kw in JUNK_SENDER_KEYWORDS):
        return "Automated"

    if re.search(r"(invitation|calendar|rsvp|event)", text):
        return "Calendar"

    return "Personal"


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------


def build_gmail_service(creds_data: dict):
    """Build a Gmail service from credentials dict."""
    creds = Credentials(**creds_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def get_gmail_service():
    """Build a Gmail service from session credentials."""
    creds_data = session.get("credentials")
    if not creds_data:
        return None
    creds = Credentials(**creds_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        session["credentials"] = creds_to_dict(creds)
    return build("gmail", "v1", credentials=creds)


def creds_to_dict(creds):
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }


def fetch_messages(service, max_results=500, query="in:inbox"):
    messages = []
    page_token = None
    while len(messages) < max_results:
        batch_size = min(100, max_results - len(messages))
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=batch_size, pageToken=page_token)
            .execute()
        )
        batch = resp.get("messages", [])
        if not batch:
            break
        messages.extend(batch)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def _parse_message(msg: dict) -> dict:
    headers = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers[h["name"]] = h["value"]
    return {
        "id": msg["id"],
        "snippet": msg.get("snippet", ""),
        "headers": headers,
        "label_ids": msg.get("labelIds", []),
        "size": msg.get("sizeEstimate", 0),
    }


def batch_get_message_details(service, msg_ids: list, job_id: str = None) -> list:
    all_results = {}
    for batch_start in range(0, len(msg_ids), 100):
        batch_ids = msg_ids[batch_start: batch_start + 100]
        batch_req = service.new_batch_http_request()

        def _callback(request_id, response, exception):
            if exception is None:
                all_results[response["id"]] = _parse_message(response)

        for mid in batch_ids:
            req = (
                service.users()
                .messages()
                .get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe"],
                )
            )
            batch_req.add(req, callback=_callback)
        batch_req.execute()

        # Update job progress
        if job_id and job_id in jobs:
            jobs[job_id]["progress"] = min(batch_start + 100, len(msg_ids))

    return [all_results[mid] for mid in msg_ids if mid in all_results]


def analyze_inbox(service, max_messages=500, job_id=None):
    if job_id:
        jobs[job_id]["status"] = "listing"
    raw_messages = fetch_messages(service, max_results=max_messages)
    msg_ids = [m["id"] for m in raw_messages]

    if job_id:
        jobs[job_id]["status"] = "fetching"
        jobs[job_id]["total"] = len(msg_ids)
        jobs[job_id]["progress"] = 0

    all_details = batch_get_message_details(service, msg_ids, job_id=job_id)

    if job_id:
        jobs[job_id]["status"] = "categorizing"

    emails = []
    sender_counter = Counter()
    category_counter = Counter()
    sender_emails = defaultdict(list)
    category_emails = defaultdict(list)
    total_size = 0

    for details in all_details:
        sender = details["headers"].get("From", "Unknown")
        subject = details["headers"].get("Subject", "(no subject)")
        snippet = details["snippet"]
        date_str = details["headers"].get("Date", "")
        category = categorize_email(sender, subject, snippet, details["headers"])

        email_data = {
            "id": details["id"],
            "sender": sender,
            "subject": subject,
            "date": date_str,
            "category": category,
            "size": details["size"],
        }
        emails.append(email_data)
        sender_counter[sender] += 1
        category_counter[category] += 1
        sender_emails[sender].append(email_data)
        category_emails[category].append(email_data)
        total_size += details["size"]

    return {
        "emails": emails,
        "sender_counter": dict(sender_counter),
        "category_counter": dict(category_counter),
        "sender_emails": {k: v for k, v in sender_emails.items()},
        "category_emails": {k: v for k, v in category_emails.items()},
        "total_size": total_size,
    }


def _build_response_data(analysis: dict) -> dict:
    """Build the JSON response from an analysis result."""
    categories = []
    for cat, count in sorted(analysis["category_counter"].items(), key=lambda x: -x[1]):
        categories.append({
            "name": cat,
            "count": count,
            "color": CATEGORY_COLORS.get(cat, "#6b7280"),
            "pct": round(count / len(analysis["emails"]) * 100, 1) if analysis["emails"] else 0,
        })

    senders = []
    sender_counter = Counter(analysis["sender_counter"])
    for sender, count in sender_counter.most_common(30):
        cats = Counter(e["category"] for e in analysis["sender_emails"][sender])
        top_cat = cats.most_common(1)[0][0]
        name, email = extract_name_and_email(sender)
        senders.append({
            "raw": sender,
            "name": name,
            "email": email,
            "count": count,
            "category": top_cat,
            "color": CATEGORY_COLORS.get(top_cat, "#6b7280"),
        })

    junk_cats = ["Newsletter", "Automated", "Social", "Shopping"]
    junk_count = sum(analysis["category_counter"].get(c, 0) for c in junk_cats)
    junk_pct = round(junk_count / len(analysis["emails"]) * 100, 1) if analysis["emails"] else 0

    return {
        "total": len(analysis["emails"]),
        "unique_senders": len(analysis["sender_counter"]),
        "total_size_mb": round(analysis["total_size"] / (1024 * 1024), 1),
        "junk_count": junk_count,
        "junk_pct": junk_pct,
        "categories": categories,
        "senders": senders,
    }


def _batch_trash(service, msg_ids: list):
    for i in range(0, len(msg_ids), 100):
        batch = msg_ids[i: i + 100]
        service.users().messages().batchModify(
            userId="me",
            body={"ids": batch, "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
        ).execute()


# ---------------------------------------------------------------------------
# Background scan worker
# ---------------------------------------------------------------------------


def _run_scan(job_id: str, creds_data: dict, max_messages: int):
    """Run inbox analysis in a background thread."""
    try:
        service = build_gmail_service(creds_data)
        analysis = analyze_inbox(service, max_messages=max_messages, job_id=job_id)
        jobs[job_id]["result"] = _build_response_data(analysis)
        jobs[job_id]["analysis"] = analysis
        jobs[job_id]["status"] = "done"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    if "credentials" not in session:
        return render_template("login.html")
    return render_template("dashboard.html")


@app.route("/auth/login")
def auth_login():
    if not CREDS_PATH.exists():
        return "credentials.json not found. See README for setup.", 500

    flow = Flow.from_client_secrets_file(
        str(CREDS_PATH), scopes=SCOPES,
        redirect_uri=url_for("auth_callback", _external=True),
    )
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    flow = Flow.from_client_secrets_file(
        str(CREDS_PATH), scopes=SCOPES,
        redirect_uri=url_for("auth_callback", _external=True),
        state=session.get("oauth_state"),
    )
    flow.fetch_token(authorization_response=request.url)
    session["credentials"] = creds_to_dict(flow.credentials)
    return redirect(url_for("index"))


@app.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Start a background scan. Returns a job ID to poll."""
    creds_data = session.get("credentials")
    if not creds_data:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json() or {}
    max_messages = min(data.get("max", 500), 5000)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "starting",
        "progress": 0,
        "total": 0,
        "result": None,
        "analysis": None,
        "error": None,
    }

    thread = threading.Thread(target=_run_scan, args=(job_id, creds_data, max_messages))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/scan/<job_id>")
def api_scan_status(job_id):
    """Poll scan progress."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    resp = {
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
    }

    if job["status"] == "done":
        resp["result"] = job["result"]
    elif job["status"] == "error":
        resp["error"] = job["error"]

    return jsonify(resp)


@app.route("/api/trash", methods=["POST"])
def api_trash():
    service = get_gmail_service()
    if not service:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    msg_ids = data.get("ids", [])
    if not msg_ids:
        return jsonify({"error": "No message IDs provided"}), 400

    _batch_trash(service, msg_ids)
    return jsonify({"trashed": len(msg_ids)})


@app.route("/api/trash_by_sender", methods=["POST"])
def api_trash_by_sender():
    """Trash emails by sender using the last scan's data."""
    service = get_gmail_service()
    if not service:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    senders = data.get("senders", [])
    job_id = data.get("job_id")

    job = jobs.get(job_id) if job_id else None
    if not job or not job.get("analysis"):
        return jsonify({"error": "No scan data found. Run a scan first."}), 400

    analysis = job["analysis"]
    msg_ids = []
    for sender in senders:
        msg_ids.extend(e["id"] for e in analysis["sender_emails"].get(sender, []))

    if not msg_ids:
        return jsonify({"trashed": 0})

    _batch_trash(service, msg_ids)
    return jsonify({"trashed": len(msg_ids)})


@app.route("/api/trash_by_category", methods=["POST"])
def api_trash_by_category():
    """Trash emails by category using the last scan's data."""
    service = get_gmail_service()
    if not service:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    categories = data.get("categories", [])
    job_id = data.get("job_id")

    job = jobs.get(job_id) if job_id else None
    if not job or not job.get("analysis"):
        return jsonify({"error": "No scan data found. Run a scan first."}), 400

    analysis = job["analysis"]
    msg_ids = []
    for cat in categories:
        msg_ids.extend(e["id"] for e in analysis["category_emails"].get(cat, []))

    if not msg_ids:
        return jsonify({"trashed": 0})

    _batch_trash(service, msg_ids)
    return jsonify({"trashed": len(msg_ids)})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
