# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import time
os.environ["TZ"] = "Asia/Kolkata"
try:
    time.tzset()
except Exception:
    pass
import io
import re
import random
import hashlib
import urllib.parse
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)

import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify)
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or config.get("app", "secret_key")
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


@app.template_filter("format_ist")
def format_ist_filter(val, fmt="%Y-%m-%d %H:%M:%S IST"):
    if not val:
        return "-"
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return val if "IST" in val else f"{val} IST"
    if hasattr(val, "tzinfo") and val.tzinfo is not None:
        val = val.astimezone(IST)
    elif isinstance(val, date) and not isinstance(val, datetime):
        return val.strftime("%d/%m/%Y")
    return val.strftime(fmt)


@app.before_request
def enforce_security_guards():
    path = request.path
    # 1. Admin route protection:
    # Protect all /admin/* routes except the login page (/admin, /admin/) and logout (/admin/logout)
    if path.startswith("/admin"):
        if path not in ("/admin", "/admin/", "/admin/logout"):
            if not session.get("admin"):
                flash("Please log in to access the admin portal.")
                return redirect(url_for("admin_login"))

    # 2. Application form steps protection:
    # User cannot access form steps, upload, or declaration without mobile verification
    if path.startswith("/form/") or path in ("/upload", "/declaration"):
        if not session.get("verified_mobile"):
            flash("Please verify your mobile number with OTP first.")
            return redirect(url_for("apply"))


ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# An applicant may hold at most one application in these states at a time.
# WITHDRAWN and REJECTED applications do not block a fresh application.
ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")

SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL") or config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or config.get("app", "upload_dir")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except Exception:
    UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
    os.makedirs(UPLOAD_DIR, exist_ok=True)

SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


from urllib.parse import quote_plus, unquote

def format_db_url(url):
    if not url:
        return url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    prefix, _, rest = url.partition("://")
    if "@" in rest:
        auth_part, host_part = rest.rsplit("@", 1)
        if ":" in auth_part:
            user, password = auth_part.split(":", 1)
            clean_pw = unquote(password)
            encoded_pw = quote_plus(clean_pw)
            url = f"{prefix}://{user}:{encoded_pw}@{host_part}"
    return url


def get_db():
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        conn = psycopg2.connect(format_db_url(db_url))
    else:
        conn = psycopg2.connect(
            host=os.environ.get("DB_HOST", config.get("database", "host")),
            port=os.environ.get("DB_PORT", config.get("database", "port")),
            dbname=os.environ.get("DB_NAME", config.get("database", "name")),
            user=os.environ.get("DB_USER", config.get("database", "user")),
            password=os.environ.get("DB_PASSWORD", config.get("database", "password")),
        )
    try:
        with conn.cursor() as cur:
            cur.execute("SET timezone = 'Asia/Kolkata'")
        conn.commit()
    except Exception:
        pass
    return conn



def init_db_if_needed():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('applications');")
        res = cur.fetchone()
        if not res or not res[0]:
            app.logger.info("Initializing database schema from schema.sql...")
            schema_path = os.path.join(BASE_DIR, "schema.sql")
            if os.path.exists(schema_path):
                with open(schema_path, "r", encoding="utf-8") as f:
                    cur.execute(f.read())
                conn.commit()
                app.logger.info("Database schema initialized successfully.")
        else:
            # Ensure doc columns exist for database-backed file storage
            cur.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS doc_data BYTEA;")
            cur.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS doc_mime VARCHAR(100);")
            cur.execute("ALTER TABLE applications ADD COLUMN IF NOT EXISTS doc_name VARCHAR(200);")
            conn.commit()

            # Backfill any files present on disk into the database
            cur.execute("SELECT id, doc_path FROM applications WHERE doc_data IS NULL AND doc_path IS NOT NULL AND doc_path != '';")
            for app_rec_id, dpath in cur.fetchall():
                if dpath and os.path.exists(dpath):
                    try:
                        with open(dpath, "rb") as f:
                            data = f.read()
                        dname = os.path.basename(dpath)
                        ext = dname.rsplit(".", 1)[-1].lower() if "." in dname else ""
                        dmime = "application/pdf" if ext == "pdf" else ("image/png" if ext == "png" else "image/jpeg")
                        cur.execute("UPDATE applications SET doc_data = %s, doc_mime = %s, doc_name = %s WHERE id = %s",
                                    (psycopg2.Binary(data), dmime, dname, app_rec_id))
                    except Exception:
                        pass
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        app.logger.warning("Database schema check notice: %s" % e)


init_db_if_needed()


def sanitize(value, maxlen=100):
    # Trim whitespace and cap at the column width, counting characters (not
    # bytes) so Assamese/Bangla names are stored intact.
    if value is None:
        return ""
    return value.strip()[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


_FALLBACK_SMS = []


def send_sms(mobile, text):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO sms_logs (mobile, text, created_at) VALUES (%s, %s, %s)",
                    (mobile, text, now_ist()))
        # Delete older messages if count exceeds 50
        cur.execute("""
            DELETE FROM sms_logs WHERE id NOT IN (
                SELECT id FROM sms_logs ORDER BY id DESC LIMIT 50
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        app.logger.warning("Could not persist SMS to database: %s" % e)
        _FALLBACK_SMS.append({
            "to": mobile,
            "body": text,
            "sent_at": now_ist().strftime("%H:%M:%S IST")
        })
        while len(_FALLBACK_SMS) > 50:
            _FALLBACK_SMS.pop(0)

    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=1)
    except Exception:
        pass


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + now_ist().strftime("%y") + str(random.randint(100000, 999999))


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (app_id, action, actor, note, now_ist()))


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def validate_personal_details(data):
    """Validate Step 1 (Personal Details & Age Eligibility). Returns (errors, error_fields)."""
    errors = []
    error_fields = set()
    name = sanitize(data.get("applicant_name"))
    if not name:
        errors.append("full name is required")
        error_fields.add("applicant_name")

    dob_raw = (data.get("dob") or "").strip()
    if not dob_raw:
        errors.append("date of birth is required")
        error_fields.add("dob")
    else:
        dob = parse_dob(dob_raw)
        if dob is None:
            errors.append("date of birth must be given as DD/MM/YYYY")
            error_fields.add("dob")
        else:
            today = now_ist().date()
            if dob > today:
                errors.append("date of birth cannot be in the future")
                error_fields.add("dob")
            else:
                age = age_on(dob, today)
                if age < MIN_AGE:
                    errors.append("applicant must be %d years of age or above (calculated age: %d years)" % (MIN_AGE, age))
                    error_fields.add("dob")

    gender = (data.get("gender") or "").strip()
    if not gender or gender not in ("Male", "Female", "Other"):
        errors.append("gender is required")
        error_fields.add("gender")

    marital_status = (data.get("marital_status") or "").strip()
    if not marital_status:
        errors.append("marital status is required")
        error_fields.add("marital_status")

    return errors, list(error_fields)


def validate_address_details(data):
    """Validate Step 2 (Address Details). Returns (errors, error_fields)."""
    errors = []
    error_fields = set()
    village = (data.get("village") or "").strip()
    if not village:
        errors.append("village is required")
        error_fields.add("village")

    block = (data.get("block") or "").strip()
    if not block:
        errors.append("block is required")
        error_fields.add("block")
    elif block not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))
        error_fields.add("block")

    return errors, list(error_fields)


def validate_bank_details(data):
    """Validate Step 3 (Bank Details). Returns (errors, error_fields)."""
    errors = []
    error_fields = set()
    bank_account = re.sub(r"[\s-]", "", data.get("bank_account") or "")
    if not bank_account:
        errors.append("bank account is required")
        error_fields.add("bank_account")
    elif not re.fullmatch(r"\d{9,18}", bank_account):
        errors.append("bank account number must be 9 to 18 digits")
        error_fields.add("bank_account")

    ifsc = re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper()
    if not ifsc:
        errors.append("IFSC code is required")
        error_fields.add("ifsc")
    elif not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")
        error_fields.add("ifsc")

    return errors, list(error_fields)


def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid);
    `cleaned` holds the normalised values ready for create_application().
    """
    errors = []
    p_err, _ = validate_personal_details(data)
    a_err, _ = validate_address_details(data)
    b_err, _ = validate_bank_details(data)
    errors.extend(p_err)
    errors.extend(a_err)
    errors.extend(b_err)

    dob = parse_dob(data.get("dob"))
    cleaned = {
        "applicant_name": sanitize(data.get("applicant_name")),
        "village": (data.get("village") or "").strip(),
        "block": (data.get("block") or "").strip(),
        "gender": data.get("gender") or "",
        "marital_status": data.get("marital_status") or "",
        "husband_name": (data.get("husband_name") or "").strip(),
        "husband_employer": "",  # no longer collected (not a scheme requirement)
        "bank_account": re.sub(r"[\s-]", "", data.get("bank_account") or ""),
        "ifsc": re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper(),
        "doc_path": doc_path or "",
        "dob": dob,
    }

    if not cleaned["doc_path"]:
        errors.append("age proof document is required")
    if now_ist() > SCHEME_DEADLINE:
        errors.append("the application window has closed")
    return errors, cleaned


def active_application(cur, mobile):
    """The applicant's current live application, if any."""
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN %s "
                "ORDER BY submitted_at DESC LIMIT 1", (mobile, ACTIVE_STATUSES))
    return cur.fetchone()


def create_application(cur, mobile, cleaned):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no).
    """
    app_no = new_application_no()
    doc_path = cleaned.get("doc_path", "")
    doc_data = None
    doc_mime = None
    doc_name = None
    if doc_path and os.path.exists(doc_path):
        try:
            with open(doc_path, "rb") as df:
                doc_data = df.read()
            doc_name = os.path.basename(doc_path)
            ext = doc_name.rsplit(".", 1)[-1].lower() if "." in doc_name else ""
            if ext == "pdf" or (doc_data and b"%PDF-" in doc_data[:1024]):
                doc_mime = "application/pdf"
            elif ext == "png" or (doc_data and doc_data.startswith(b"\x89PNG")):
                doc_mime = "image/png"
            elif ext in ("jpg", "jpeg") or (doc_data and doc_data.startswith(b"\xff\xd8")):
                doc_mime = "image/jpeg"
            else:
                doc_mime = "application/octet-stream"
        except Exception as e:
            app.logger.warning("Could not read doc file for DB storage: %s" % e)

    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, doc_data, doc_mime, doc_name, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
         cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
         cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
         cleaned["doc_path"], psycopg2.Binary(doc_data) if doc_data else None,
         doc_mime, doc_name, now_ist()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile)
    return new_id, app_no


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/images/<path:filename>")
def serve_image(filename):
    from urllib.parse import unquote
    filename = unquote(filename)
    path = os.path.join(BASE_DIR, "images", filename)
    return send_file(path)


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    subpath = (subpath or "").strip("/")

    # Check if API request for messages (JSON polling)
    if subpath in ("messages", "api/messages"):
        mobile = request.args.get("mobile") or request.args.get("to")
        msgs = []
        try:
            conn = get_db()
            cur = conn.cursor()
            if mobile:
                cur.execute("SELECT id, mobile, text, created_at FROM sms_logs WHERE mobile = %s ORDER BY id DESC LIMIT 20", (mobile,))
            else:
                cur.execute("SELECT id, mobile, text, created_at FROM sms_logs ORDER BY id DESC LIMIT 50")
            for r in cur.fetchall():
                msgs.append({
                    "id": r[0], "to": r[1], "text": r[2],
                    "delivered": True, "sent_at": r[3].isoformat() if r[3] else None
                })
            # Fallback to otps if sms_logs was empty
            if not msgs:
                if mobile:
                    cur.execute("SELECT id, mobile, code, created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 20", (mobile,))
                else:
                    cur.execute("SELECT id, mobile, code, created_at FROM otps ORDER BY id DESC LIMIT 50")
                for r in cur.fetchall():
                    msgs.append({
                        "id": r[0], "to": r[1],
                        "text": f"Your Sewa Setu OTP is {r[2]}. Valid for 5 minutes.",
                        "delivered": True, "sent_at": r[3].isoformat() if r[3] else None
                    })
            cur.close()
            conn.close()
        except Exception as e:
            app.logger.warning("Gateway API DB error: %s" % e)
        return jsonify({"messages": msgs})

    # Web UI Console
    messages = []
    try:
        conn = get_db()
        cur = conn.cursor()
        # 1. Fetch from sms_logs
        cur.execute("SELECT id, mobile, text, created_at FROM sms_logs ORDER BY id DESC")
        for r in cur.fetchall():
            messages.append({"id": f"sms-{r[0]}", "raw_id": r[0], "type": "sms", "to": r[1], "text": r[2], "created_at": r[3]})

        # 2. Also fetch from otps table so existing OTPs are never missed
        cur.execute("SELECT id, mobile, code, created_at FROM otps ORDER BY id DESC")
        existing_signatures = {(m["to"], m["text"]) for m in messages}
        for r in cur.fetchall():
            text = f"Your Sewa Setu OTP is {r[2]}. Valid for 5 minutes."
            if (r[1], text) not in existing_signatures:
                messages.append({"id": f"otp-{r[0]}", "raw_id": r[0], "type": "otp", "to": r[1], "text": text, "created_at": r[3]})

        # Sort all messages by timestamp descending
        messages.sort(key=lambda x: x["created_at"] or datetime.min, reverse=True)

        # If messages exceed 50 in count, delete the older ones one by one
        if len(messages) > 50:
            excess = messages[50:]
            sms_ids_to_del = [m["raw_id"] for m in excess if m.get("type") == "sms"]
            otp_ids_to_del = [m["raw_id"] for m in excess if m.get("type") == "otp"]
            if sms_ids_to_del:
                cur.execute("DELETE FROM sms_logs WHERE id = ANY(%s)", (sms_ids_to_del,))
            if otp_ids_to_del:
                cur.execute("DELETE FROM otps WHERE id = ANY(%s)", (otp_ids_to_del,))
            conn.commit()
            messages = messages[:50]

        cur.close()
        conn.close()
    except Exception as e:
        app.logger.warning("Gateway Console DB error: %s" % e)
        while len(_FALLBACK_SMS) > 50:
            _FALLBACK_SMS.pop(0)
        messages = [{"id": f"fallback-{i+1}", "to": m.get("to"), "text": m.get("body"), "created_at": None} for i, m in enumerate(reversed(_FALLBACK_SMS))]

    now = now_ist()
    rows = []
    for i, m in enumerate(messages[:50]):
        created_at = m.get("created_at")
        sent_str = created_at.strftime("%H:%M:%S (%d %b) IST") if created_at else "Just now"

        status_badge = '<span style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:9999px;font-size:12px;font-weight:600;background:#dcfce7;color:#15803d"><span style="width:7px;height:7px;border-radius:50%;background:#22c55e"></span> Delivered</span>'

        if created_at and "OTP" in (m.get("text") or ""):
            age_sec = (now - created_at).total_seconds()
            if age_sec <= OTP_VALIDITY_SECONDS:
                mins_left = max(1, int((OTP_VALIDITY_SECONDS - age_sec) // 60))
                status_badge = f'<span style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:9999px;font-size:12px;font-weight:600;background:#e0f2fe;color:#0369a1"><span style="width:7px;height:7px;border-radius:50%;background:#0284c7"></span> Active (~{mins_left}m left)</span>'
            else:
                status_badge = '<span style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:9999px;font-size:12px;font-weight:500;background:#f1f5f9;color:#64748b">Expired</span>'

        text_html = m.get("text", "")
        text_html = re.sub(r'\b(\d{6})\b', r'<strong style="font-size:17px;letter-spacing:1.5px;color:#1e40af;background:#eff6ff;padding:2px 8px;border-radius:6px;border:1px solid #bfdbfe">\1</strong>', text_html)

        rows.append(f"""<tr>
            <td style="color:#64748b;font-weight:500">{i+1}</td>
            <td style="font-weight:600;color:#0f172a">{m.get('to')}</td>
            <td>{text_html}</td>
            <td style="color:#475569">{sent_str}</td>
            <td>{status_badge}</td>
        </tr>""")

    rows_html = "".join(rows) if rows else '''<tr>
        <td colspan="5" style="text-align:center;padding:48px 16px;">
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;max-width:320px;margin:0 auto;text-align:center;">
                <div style="width:48px;height:48px;border-radius:50%;background:#ffffff;border:1px solid #e2e8f0;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;color:#94a3b8;box-shadow:0 1px 2px rgba(0,0,0,0.05);">
                    <svg style="width:24px;height:24px;" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                </div>
                <h4 style="font-size:16px;font-weight:700;color:#0f172a;margin-bottom:4px;">No dispatches found</h4>
                <p style="font-size:13px;color:#64748b;margin:0;">No SMS messages or OTPs have been dispatched yet.</p>
            </div>
        </td>
    </tr>'''

    html = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PurvSMS Gateway Console (Persistent Simulation)</title>
    <meta http-equiv="refresh" content="5">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif; }}
        body {{ background: #f8fafc; color: #1e293b; padding: 32px 20px; }}
        .container {{ max-width: 1080px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }}
        .header h1 {{ font-size: 22px; font-weight: 700; color: #0f172a; display: flex; align-items: center; gap: 10px; }}
        .badge-live {{ display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 600; background: #ecfdf5; color: #059669; border: 1px solid #a7f3d0; }}
        .pulse {{ width: 8px; height: 8px; border-radius: 50%; background: #10b981; animation: pulse 2s infinite; }}
        @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} 100% {{ opacity: 1; }} }}
        .card {{ background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05); overflow: hidden; }}
        .card-header {{ padding: 16px 20px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 14px; color: #64748b; display: flex; justify-content: space-between; align-items: center; }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
        th {{ background: #f8fafc; color: #475569; font-weight: 600; padding: 14px 18px; border-bottom: 1px solid #e2e8f0; }}
        td {{ padding: 14px 18px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }}
        tr:hover {{ background: #f8fafc; }}
        .info-bar {{ margin-top: 16px; padding: 12px 18px; background: #eff6ff; border: 1px solid #dbeafe; border-radius: 12px; font-size: 13px; color: #1e40af; display: flex; align-items: center; gap: 8px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>PurvSMS Gateway Console</h1>
                <p style="color: #64748b; font-size: 14px; margin-top: 4px;">Shared Persistent SMS & OTP Simulation (Database-backed)</p>
            </div>
            <div class="badge-live">
                <span class="pulse"></span> Auto-updating every 5s
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <span>Recent Incoming & Outgoing Dispatches</span>
                <span style="font-size: 12px;">Auto-synced with PostgreSQL</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th style="width: 60px;">#</th>
                        <th style="width: 140px;">Recipient</th>
                        <th>Message / OTP Payload</th>
                        <th style="width: 170px;">Sent Time</th>
                        <th style="width: 150px;">Status</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>

        <div class="info-bar">
            <span>💡</span>
            <span><strong>Tip:</strong> All OTPs are persisted in the database. You can refresh this tab as often as you want—messages will stay saved and will not disappear.</span>
        </div>
    </div>
</body>
</html>"""
    return (html, 200, {"Content-Type": "text/html"})


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "GET":
        session.pop("verified_mobile", None)
        session.pop("form_data", None)
        session.pop("doc_path", None)
    if request.method == "POST":
        session.pop("verified_mobile", None)
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha(),
                                   mobile=mobile, error_fields=["captcha"])
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha(),
                                   mobile=mobile, error_fields=["mobile"])
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                     (mobile, code, now_ist()))
        cur.execute("""
            DELETE FROM otps WHERE id NOT IN (
                SELECT id FROM otps ORDER BY id DESC LIMIT 50
            )
        """)
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha(), mobile=session.get("apply_mobile", ""), error_fields=[])


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (now_ist() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
        return render_template("verify.html", mobile=mobile, error_fields=["otp"])
    return render_template("verify.html", mobile=mobile, error_fields=[])


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    mobile = session["verified_mobile"]
    conn = get_db()
    cur = conn.cursor()
    existing = active_application(cur, mobile)
    cur.close(); conn.close()
    if existing:
        if step != 1 or request.method == "POST":
            return redirect(url_for("form_step", step=1))
        return render_template("form_step1.html", data={}, blocks=BLOCKS,
                               existing=existing, error_fields=[])

    # Progressive check on GET: ensure prior steps are valid before accessing later steps
    if request.method == "GET":
        saved = session.get("form_data", {})
        if step >= 2:
            s1_err, _ = validate_personal_details(saved)
            if s1_err:
                flash("Please complete personal details and verify eligibility first.")
                return redirect(url_for("form_step", step=1))
        if step >= 3:
            s2_err, _ = validate_address_details(saved)
            if s2_err:
                flash("Please complete address details first.")
                return redirect(url_for("form_step", step=2))

    if request.method == "POST":
        data = session.get("form_data", {})
        action = request.form.get("action", "next")
        for k, v in request.form.items():
            if k != "action":
                data[k] = v
        session["form_data"] = data
        if action == "back":
            if step == 1:
                return redirect(url_for("verify"))
            return redirect(url_for("form_step", step=step - 1))

        # Validate current step before advancing
        step_errors = []
        err_fields = []
        if step == 1:
            step_errors, err_fields = validate_personal_details(data)
        elif step == 2:
            step_errors, err_fields = validate_address_details(data)
        elif step == 3:
            step_errors, err_fields = validate_bank_details(data)

        if step_errors:
            if len(step_errors) == 1:
                flash(step_errors[0])
            else:
                flash("<strong>Please correct the following:</strong><ul class='list-disc list-inside mt-1.5 space-y-0.5'>" +
                      "".join(f"<li>{e}</li>" for e in step_errors) + "</ul>")
            return render_template("form_step%d.html" % step,
                                   data=data, blocks=BLOCKS,
                                   existing=None, error_fields=err_fields)

        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS,
                           existing=None, error_fields=[])


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    mobile = session["verified_mobile"]
    conn = get_db()
    cur = conn.cursor()
    existing = active_application(cur, mobile)
    cur.close(); conn.close()
    if existing:
        return redirect(url_for("form_step", step=1))

    # Guard: Ensure earlier steps are completed
    saved = session.get("form_data", {})
    s1_err, _ = validate_personal_details(saved)
    if s1_err:
        flash("Please complete personal details and verify eligibility first.")
        return redirect(url_for("form_step", step=1))
    s2_err, _ = validate_address_details(saved)
    if s2_err:
        flash("Please complete address details first.")
        return redirect(url_for("form_step", step=2))
    s3_err, _ = validate_bank_details(saved)
    if s3_err:
        flash("Please complete bank details first.")
        return redirect(url_for("form_step", step=3))

    if request.method == "POST":
        action = request.form.get("action", "next")
        if action == "back":
            return redirect(url_for("form_step", step=3))
        f = request.files.get("document")
        if f is None or f.filename == "":
            if session.get("doc_path") and os.path.exists(session.get("doc_path")):
                return redirect(url_for("declaration"))
            flash("Please choose a file to upload (JPG, PNG or PDF, up to 5 MB).")
            doc_path = session.get("doc_path", "")
            doc_name = os.path.basename(doc_path) if doc_path and os.path.exists(doc_path) else None
            return render_template("upload.html", doc_name=doc_name, error_fields=["document"])
        orig_filename = f.filename
        content = f.read()

        # 1. Size validation: must be less than 5 MB and not empty
        MAX_DOC_SIZE = 5 * 1024 * 1024  # 5 MB
        if len(content) == 0:
            flash("The uploaded file is empty. Please choose a valid file.")
            doc_path = session.get("doc_path", "")
            doc_name = os.path.basename(doc_path) if doc_path and os.path.exists(doc_path) else None
            return render_template("upload.html", doc_name=doc_name, error_fields=["document"])

        if len(content) > MAX_DOC_SIZE:
            flash("File size exceeds 5 MB. Please upload a file smaller than 5 MB.")
            doc_path = session.get("doc_path", "")
            doc_name = os.path.basename(doc_path) if doc_path and os.path.exists(doc_path) else None
            return render_template("upload.html", doc_name=doc_name, error_fields=["document"])

        # 2. Extension validation: must be PDF, JPG, JPEG, or PNG
        ext = orig_filename.rsplit(".", 1)[-1].lower() if "." in orig_filename else ""
        if ext not in ("pdf", "jpg", "jpeg", "png"):
            flash("Invalid file format. Only JPG, PNG, or PDF files are allowed.")
            doc_path = session.get("doc_path", "")
            doc_name = os.path.basename(doc_path) if doc_path and os.path.exists(doc_path) else None
            return render_template("upload.html", doc_name=doc_name, error_fields=["document"])

        # 3. Magic bytes / header validation to ensure genuine image or PDF
        is_pdf = (ext == "pdf") and (b"%PDF-" in content[:1024])
        is_jpeg = (ext in ("jpg", "jpeg")) and content.startswith(b"\xff\xd8")
        is_png = (ext == "png") and content.startswith(b"\x89PNG\r\n\x1a\n")

        if not (is_pdf or is_jpeg or is_png):
            flash("Invalid file content. The file does not appear to be a valid image (JPG, PNG) or PDF document.")
            doc_path = session.get("doc_path", "")
            doc_name = os.path.basename(doc_path) if doc_path and os.path.exists(doc_path) else None
            return render_template("upload.html", doc_name=doc_name, error_fields=["document"])

        if is_pdf:
            mime_type = "application/pdf"
        elif is_png:
            mime_type = "image/png"
        else:
            mime_type = "image/jpeg"

        clean_filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", os.path.basename(orig_filename))
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, "%s_%s" % (session["verified_mobile"], clean_filename))
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        session["doc_name"] = clean_filename
        session["doc_mime"] = mime_type
        return redirect(url_for("declaration"))
    doc_path = session.get("doc_path", "")
    doc_name = os.path.basename(doc_path) if doc_path and os.path.exists(doc_path) else None
    return render_template("upload.html", doc_name=doc_name, error_fields=[])


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    mobile = session["verified_mobile"]
    conn = get_db()
    cur = conn.cursor()
    existing = active_application(cur, mobile)
    cur.close(); conn.close()
    if existing:
        return redirect(url_for("form_step", step=1))

    # Guard: Ensure earlier steps and document upload are completed
    saved = session.get("form_data", {})
    s1_err, _ = validate_personal_details(saved)
    if s1_err:
        flash("Please complete personal details and verify eligibility first.")
        return redirect(url_for("form_step", step=1))
    s2_err, _ = validate_address_details(saved)
    if s2_err:
        flash("Please complete address details first.")
        return redirect(url_for("form_step", step=2))
    s3_err, _ = validate_bank_details(saved)
    if s3_err:
        flash("Please complete bank details first.")
        return redirect(url_for("form_step", step=3))
    if not session.get("doc_path") or not os.path.exists(session.get("doc_path")):
        flash("Please upload an age proof document first.")
        return redirect(url_for("upload"))

    if request.method == "POST":
        return handle_submission()
    # Preview everything before the applicant commits.
    data = session.get("form_data", {})
    errors, _ = validate_application(data, session.get("doc_path", ""))
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=os.path.basename(session.get("doc_path", "") or ""))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    errors, cleaned = validate_application(data, session.get("doc_path", ""))
    if errors:
        flash("Please correct the following before submitting: " + "; ".join(errors) + ".")
        return redirect(url_for("form_step", step=1))

    conn = get_db()
    cur = conn.cursor()
    existing = active_application(cur, mobile)
    if existing:
        cur.close(); conn.close()
        flash("Application %s (%s) already exists for this mobile number. A pending "
              "application can be withdrawn from the status portal if you need to "
              "apply afresh." % (existing[1], existing[2]))
        return redirect(url_for("form_step", step=1))

    new_id, app_no = create_application(cur, mobile, cleaned)
    conn.commit()
    cur.close(); conn.close()

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("doc_name", None)
    session.pop("doc_mime", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            if label == "Submitted At" and val:
                val_str = format_ist_filter(val)
            elif isinstance(val, date):
                val_str = val.strftime("%d/%m/%Y")
            else:
                val_str = str(val) if val is not None else "-"
            pdf.cell(0, 8, val_str, border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    rejection_reason = None
    if row[8] == 'REJECTED':
        cur.execute("SELECT note FROM audit_log WHERE application_id = %s AND action = 'REJECTED' "
                    "ORDER BY at DESC, id DESC LIMIT 1", (app_id,))
        rej_row = cur.fetchone()
        if rej_row and rej_row[0] and rej_row[0].strip():
            rejection_reason = rej_row[0].strip()
        else:
            rejection_reason = "Reason not specified by departmental officer."
    cur.close(); conn.close()
    return render_template("application.html", a=row, app_id=app_id,
                           rejection_reason=rejection_reason)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own PENDING application so they can apply afresh."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (now_ist(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
        flash("Application %s has been withdrawn. You may submit a new application." % row[0])
    else:
        flash("Only a pending application can be withdrawn.")
    cur.close(); conn.close()
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET" and session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        if (ADMIN_PASSWORD and request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_user", None)
    session.pop("logged_in", None)
    flash("You have been logged out successfully.")
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (now_ist() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    q = (request.args.get("q") or request.args.get("mobile") or "").strip()
    status = request.args.get("status", "" if q else "PENDING").strip().upper()
    block = request.args.get("block", "").strip()
    gender = request.args.get("gender", "").strip()

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT block FROM applications WHERE block IS NOT NULL AND block != '' ORDER BY block")
    blocks = [r[0] for r in cur.fetchall()]

    where, params = [], []
    if q:
        p = f"%{q}%"
        where.append("(application_no ILIKE %s OR applicant_name ILIKE %s OR mobile ILIKE %s)")
        params.extend([p, p, p])
    if status and status != "ALL":
        where.append("status = %s")
        params.append(status)
    if block and block != "ALL":
        where.append("block = %s")
        params.append(block)
    if gender and gender != "ALL":
        where.append("gender ILIKE %s")
        params.append(gender)

    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(f"SELECT count(*) FROM applications {sql_where}", tuple(params))
    total_count = cur.fetchone()[0]

    cur.execute(f"SELECT id, application_no, applicant_name, mobile, block, status, submitted_at "
                f"FROM applications {sql_where} ORDER BY submitted_at DESC, id DESC LIMIT %s OFFSET %s",
                tuple(params + [per_page, offset]))
    rows = cur.fetchall()
    cur.close(); conn.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    start_entry = (page - 1) * per_page + 1 if total_count > 0 else 0
    end_entry = min(page * per_page, total_count)

    active_filters = sum([
        bool(status and status != "ALL" and (q or status != "PENDING")),
        bool(block and block != "ALL"),
        bool(gender and gender != "ALL")
    ])

    if total_pages <= 7:
        pagination_items = list(range(1, total_pages + 1))
    elif page <= 4:
        pagination_items = [1, 2, 3, 4, 5, '...', total_pages]
    elif page >= total_pages - 3:
        pagination_items = [1, '...'] + list(range(total_pages - 4, total_pages + 1))
    else:
        pagination_items = [1, '...', page - 1, page, page + 1, '...', total_pages]

    qargs = {k: v for k, v in {"q": q, "status": status, "block": block, "gender": gender}.items() if v and v != "ALL"}
    base_query = urllib.parse.urlencode(qargs)
    display_status = f"Search: {q}" if q else (status.replace('_', ' ').title() if status and status != "ALL" else "All Applications")

    return render_template("admin_list.html", rows=rows, status=display_status,
                           raw_status=status, page=page, total_pages=total_pages,
                           total_count=total_count, per_page=per_page, q=q, mobile=q,
                           start_entry=start_entry, end_entry=end_entry,
                           pagination_items=pagination_items, all_blocks=blocks,
                           filter_status=status, filter_block=block, filter_gender=gender,
                           active_filters=active_filters, base_query=base_query)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by, "
                "doc_name, doc_mime, (doc_data IS NOT NULL) AS has_doc_data "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    cur.close(); conn.close()

    doc_path = row[10]
    has_doc_data = bool(row[17]) if len(row) > 17 else False
    has_document = bool(has_doc_data or (doc_path and os.path.exists(doc_path)))
    doc_name = (row[15] if len(row) > 15 and row[15] else None) or (os.path.basename(doc_path) if doc_path else "document")
    doc_mime = (row[16] if len(row) > 16 and row[16] else None) or ("application/pdf" if doc_name.lower().endswith(".pdf") else "image/jpeg")
    is_image = (doc_mime.startswith("image/") if doc_mime else False) or doc_name.lower().endswith((".jpg", ".jpeg", ".png"))
    is_pdf = (doc_mime == "application/pdf") or doc_name.lower().endswith(".pdf")

    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit,
                           has_document=has_document, has_doc_data=has_doc_data,
                           doc_name=doc_name, doc_mime=doc_mime,
                           is_image=is_image, is_pdf=is_pdf)


@app.route("/admin/application/<int:app_id>/document")
def admin_view_document(app_id):
    """Serve the applicant's uploaded verification document to an authenticated admin."""
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT doc_data, doc_mime, doc_name, doc_path, application_no "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    doc_data, doc_mime, doc_name, doc_path, app_no = row
    download = request.args.get("download", "0") == "1"

    if doc_data is not None:
        raw_bytes = bytes(doc_data)
        mime = doc_mime or "application/octet-stream"
        filename = doc_name or f"age_proof_{app_no}"
        return send_file(
            io.BytesIO(raw_bytes),
            mimetype=mime,
            as_attachment=download,
            download_name=filename
        )
    elif doc_path and os.path.exists(doc_path):
        mime = doc_mime or ("application/pdf" if doc_path.lower().endswith(".pdf") else "image/jpeg")
        filename = doc_name or os.path.basename(doc_path)
        return send_file(
            doc_path,
            mimetype=mime,
            as_attachment=download,
            download_name=filename
        )
    else:
        abort(404)


@app.route("/application/<int:app_id>/document")
def citizen_view_document(app_id):
    """Serve the applicant's own uploaded verification document to an authenticated citizen."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT doc_data, doc_mime, doc_name, doc_path, application_no "
                "FROM applications WHERE id = %s AND mobile = %s", (app_id, mobile))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    doc_data, doc_mime, doc_name, doc_path, app_no = row
    download = request.args.get("download", "0") == "1"

    if doc_data is not None:
        raw_bytes = bytes(doc_data)
        mime = doc_mime or "application/octet-stream"
        filename = doc_name or f"age_proof_{app_no}"
        return send_file(
            io.BytesIO(raw_bytes),
            mimetype=mime,
            as_attachment=download,
            download_name=filename
        )
    elif doc_path and os.path.exists(doc_path):
        mime = doc_mime or ("application/pdf" if doc_path.lower().endswith(".pdf") else "image/jpeg")
        filename = doc_name or os.path.basename(doc_path)
        return send_file(
            doc_path,
            mimetype=mime,
            as_attachment=download,
            download_name=filename
        )
    else:
        abort(404)


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided and when."""
    if not session.get("admin"):
        abort(403)
    actor = session.get("admin_user", ADMIN_USERNAME)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'PENDING' RETURNING application_no",
                (new_status, now_ist(), actor, app_id))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, new_status, "admin:%s" % actor, note)
        conn.commit()
        flash("Application %s %s." % (row[0], new_status.lower()))
    else:
        flash("Only a pending application can be decided.")
    cur.close(); conn.close()
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
