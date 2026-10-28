# -*- coding: utf-8 -*-
"""Sewa Setu Portal Service Layer.

Authoritative business logic and database queries for pension scheme inquiries,
application validation, and status tracking. Used by both the Flask web portal
and the MCP servers to ensure zero logic duplication.
"""

import sys
import os
import base64
import re
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, date

app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

import app as portal_app


def _calculate_days_pending(now_dt: datetime, sub_dt: Optional[datetime]) -> int:
    """Calculate whole days elapsed since submitted_at, handling naive/aware datetimes."""
    if not sub_dt:
        return 0
    n = now_dt.replace(tzinfo=None) if getattr(now_dt, "tzinfo", None) is not None else now_dt
    s = sub_dt.replace(tzinfo=None) if getattr(sub_dt, "tzinfo", None) is not None else sub_dt
    return max(0, (n - s).days)


def get_scheme_information() -> Dict[str, Any]:
    """Retrieve official scheme requirements and statutory guidelines."""
    now = portal_app.now_ist()
    deadline = portal_app.SCHEME_DEADLINE
    window_open = now <= deadline
    hours_left = portal_app.deadline_remaining()

    return {
        "scheme_name": "Purvanchal Old Age Pension Scheme",
        "department": "Department of Social Welfare, Government of Purvanchal",
        "statutory_authority": "Purvanchal Right to Public Services Act (RTPS)",
        "benefit_amount": "₹250 per month",
        "eligibility_rules": {
            "minimum_age": portal_app.MIN_AGE,
            "minimum_age_description": f"Applicant must be {portal_app.MIN_AGE} years of age or older as of today's date (calculated in Indian Standard Time).",
            "jurisdiction": "Resident of Purvanchal belonging to one of the 6 approved blocks.",
            "approved_blocks": portal_app.BLOCKS,
            "active_application_limit": "An applicant may hold at most one active application (Pending, Approved, or Deemed Approved). If a previous application was Withdrawn or Rejected, the applicant may apply afresh."
        },
        "required_fields": [
            {"field": "applicant_name", "type": "string", "label": "Full Name", "description": "Full name as per official government records"},
            {"field": "dob", "type": "string", "label": "Date of Birth", "description": "Format DD/MM/YYYY. Applicant must be 60 years or older."},
            {"field": "gender", "type": "enum", "label": "Gender", "options": ["Male", "Female", "Other"]},
            {"field": "marital_status", "type": "string", "label": "Marital Status", "description": "e.g. Married, Widowed, Single"},
            {"field": "husband_name", "type": "string", "label": "Husband's Name", "description": "Required if married/widowed woman"},
            {"field": "village", "type": "string", "label": "Village", "description": "Village of residence"},
            {"field": "block", "type": "enum", "label": "Block / Region", "options": portal_app.BLOCKS},
            {"field": "bank_account", "type": "string", "label": "Bank Account Number", "description": "9 to 18 numeric digits"},
            {"field": "ifsc", "type": "string", "label": "IFSC Code", "description": "11-character code matching pattern ^[A-Z]{4}0[A-Z0-9]{6}$"}
        ],
        "required_documents": [
            {
                "document": "age_proof",
                "label": "Age Proof Document",
                "description": "Valid official document verifying age (Aadhaar Card, Voter ID, Birth Certificate, School Certificate)",
                "allowed_formats": ["PDF", "JPG", "JPEG", "PNG"],
                "max_size_bytes": 5 * 1024 * 1024,
                "max_size_mb": 5
            }
        ],
        "statutory_sla_days": portal_app.SLA_DAYS,
        "statutory_sla_policy": "Under the Purvanchal Right to Public Services Act, any application not processed within 15 calendar days is automatically deemed approved (DEEMED_APPROVED) by the nightly cron job.",
        "scheme_window": {
            "is_open": True,
            "deadline_ist": "Ongoing (No deadline - continuous open window)",
            "hours_remaining": None
        }
    }


def get_citizen_application_status(
    mobile: str,
    app_reference: Optional[str] = None
) -> Dict[str, Any]:
    """Retrieve application status records strictly owned by the authenticated citizen.

    Args:
        mobile: Authenticated citizen's 10-digit mobile number derived from token.
        app_reference: Optional application number or ID to filter.

    Returns:
        Structured dictionary with status, applications list, and plain-language summary.
    """
    if not mobile or len(mobile) != 10:
        return {
            "status": "error",
            "message": "Invalid mobile number format."
        }

    try:
        conn = portal_app.get_db()
        cur = conn.cursor()

        query = (
            "SELECT id, application_no, applicant_name, village, block, status, "
            "submitted_at, decided_at, decided_by "
            "FROM applications "
            "WHERE mobile = %s "
        )
        params: List[Any] = [mobile]

        if app_reference and app_reference.strip():
            ref = app_reference.strip()
            query += "AND (application_no ILIKE %s OR id::text = %s) "
            params.extend([ref, ref])

        query += "ORDER BY submitted_at DESC, id DESC LIMIT 5"

        cur.execute(query, tuple(params))
        rows = cur.fetchall()

        if not rows:
            cur.close()
            conn.close()
            return {
                "status": "not_found",
                "count": 0,
                "applications": [],
                "summary": "No applications found under your registered mobile number."
            }

        apps_list = []
        for r in rows:
            app_id, app_no, name, village, block, status, sub_at, dec_at, dec_by = r

            # If rejected, fetch the officer's decision note from audit_log
            rejection_reason = None
            if status == "REJECTED":
                cur.execute(
                    "SELECT note FROM audit_log WHERE application_id = %s AND action = 'REJECTED' "
                    "ORDER BY at DESC, id DESC LIMIT 1",
                    (app_id,)
                )
                rej_row = cur.fetchone()
                if rej_row and rej_row[0] and rej_row[0].strip():
                    rejection_reason = rej_row[0].strip()
                else:
                    rejection_reason = "Reason not specified by departmental officer."

            # Construct human-readable next step guidance
            if status == "PENDING":
                explanation = "Your application is currently under departmental review by the Block Social Welfare Officer. Under the Purvanchal Right to Public Services Act, a decision must be taken within 15 calendar days."
                can_withdraw = True
            elif status == "APPROVED":
                explanation = "Your old age pension application has been officially APPROVED. Monthly disbursement of ₹250 will be credited directly to your registered bank account."
                can_withdraw = False
            elif status == "DEEMED_APPROVED":
                explanation = "Your application stands DEEMED APPROVED under the Purvanchal Right to Public Services Act as the 15-day processing SLA elapsed without officer action."
                can_withdraw = False
            elif status == "REJECTED":
                explanation = f"Your application was REJECTED by the departmental officer. Reason: {rejection_reason}. You may address the stated reason and submit a fresh application."
                can_withdraw = False
            elif status == "WITHDRAWN":
                explanation = "This application was withdrawn by the applicant. You are eligible to submit a fresh application."
                can_withdraw = False
            else:
                explanation = f"Current application status: {status}."
                can_withdraw = False

            apps_list.append({
                "application_id": app_id,
                "application_no": app_no,
                "applicant_name": name,
                "village": village,
                "block": block,
                "status": status,
                "submitted_at": sub_at.strftime("%Y-%m-%d %H:%M:%S IST") if sub_at else None,
                "decided_at": dec_at.strftime("%Y-%m-%d %H:%M:%S IST") if dec_at else None,
                "decided_by": dec_by,
                "rejection_reason": rejection_reason,
                "status_explanation": explanation,
                "can_withdraw": can_withdraw
            })

        cur.close()
        conn.close()

        latest = apps_list[0]
        return {
            "status": "found",
            "count": len(apps_list),
            "applications": apps_list,
            "latest_application": latest,
            "summary": f"Found {len(apps_list)} application(s) for your mobile. Latest application {latest['application_no']} is currently {latest['status']}."
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Database query failure: {str(e)}"
        }


def check_application_draft(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate completeness and scheme eligibility of an application draft.

    Reuses authoritative validation rules from app.py without mutating database.
    """
    p_err, p_fields = portal_app.validate_personal_details(data)
    a_err, a_fields = portal_app.validate_address_details(data)
    b_err, b_fields = portal_app.validate_bank_details(data)

    all_errors = p_err + a_err + b_err
    all_fields = list(set(p_fields + a_fields + b_fields))

    # Check if age proof document is indicated
    doc_name = data.get("doc_name") or data.get("document_filename") or data.get("doc_path")
    has_doc = bool(doc_name)
    if not has_doc:
        all_errors.append("age proof document is required")
        all_fields.append("document")

    # Scheme window is ongoing with no expiration deadline
    is_valid = len(all_errors) == 0

    return {
        "valid": is_valid,
        "is_eligible": len(p_err) == 0,
        "errors": all_errors,
        "error_fields": all_fields,
        "summary": "Application draft is complete and satisfies all scheme rules." if is_valid else "Draft has missing or invalid fields that must be corrected before submission."
    }


def validate_document_bytes(
    doc_base64: str,
    doc_filename: str
) -> Tuple[Optional[bytes], Optional[str], Optional[str], Optional[str]]:
    """Validate document format, magic bytes, and size entirely in-memory.

    Returns:
        (content_bytes, clean_filename, mime_type, None) on success,
        or (None, None, None, error_message) on failure.
    """
    if not doc_base64 or not doc_base64.strip():
        return None, None, None, "age proof document is required"

    try:
        content = base64.b64decode(doc_base64)
    except Exception:
        return None, None, None, "The uploaded document is not valid base64-encoded data."

    if len(content) == 0:
        return None, None, None, "The uploaded file is empty. Please choose a valid file."

    MAX_DOC_SIZE = 5 * 1024 * 1024  # 5 MB
    if len(content) > MAX_DOC_SIZE:
        return None, None, None, "File size exceeds 5 MB. Please upload a file smaller than 5 MB."

    orig_filename = os.path.basename(doc_filename or "document.pdf")
    ext = orig_filename.rsplit(".", 1)[-1].lower() if "." in orig_filename else ""
    if ext not in ("pdf", "jpg", "jpeg", "png"):
        return None, None, None, "Invalid file format. Only JPG, PNG, or PDF files are allowed."

    is_pdf = (ext == "pdf") and (b"%PDF-" in content[:1024])
    is_jpeg = (ext in ("jpg", "jpeg")) and content.startswith(b"\xff\xd8")
    is_png = (ext == "png") and content.startswith(b"\x89PNG\r\n\x1a\n")

    if not (is_pdf or is_jpeg or is_png):
        return None, None, None, "Invalid file content. The file does not appear to be a valid image (JPG, PNG) or PDF document."

    clean_filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", orig_filename)
    if is_pdf:
        mime = "application/pdf"
    elif is_png:
        mime = "image/png"
    else:
        mime = "image/jpeg"

    return content, clean_filename, mime, None


def submit_citizen_application(
    mobile: str,
    data: Dict[str, Any],
    document_base64: Optional[str] = None,
    document_filename: Optional[str] = None
) -> Dict[str, Any]:
    """Validate and atomically submit a new pension application.

    Guarantees:
    - Zero rule duplication: reuses validate_application() from app.py.
    - Token-derived ownership: strictly uses authenticated mobile.
    - Magic-byte validated document storage directly in PostgreSQL BYTEA (doc_data).
    - Zero disk leakage: documents are stored exclusively in the database.
    - Idempotency & deduplication: checks active_application() before insertion.
    - Atomic database commit with portal_users and audit_log record creation.
    """
    if not mobile or len(mobile) != 10:
        return {"status": "error", "error": "INVALID_MOBILE", "message": "Invalid mobile number."}

    # 1. Early deduplication check: reject if active application already exists
    try:
        conn = portal_app.get_db()
        cur = conn.cursor()
        existing = portal_app.active_application(cur, mobile)
        if existing:
            cur.close()
            conn.close()
            return {
                "status": "duplicate_active_application",
                "existing_application": {
                    "application_id": existing[0],
                    "application_no": existing[1],
                    "status": existing[2]
                },
                "message": f"Application {existing[1]} ({existing[2]}) already exists for this mobile number. A pending application can be withdrawn from the status portal before applying afresh."
            }
    except Exception as e:
        return {"status": "error", "error": "DB_ERROR", "message": f"Database error checking active applications: {str(e)}"}

    # 2. Process and validate document in-memory
    if not document_base64:
        cur.close()
        conn.close()
        return {
            "status": "validation_error",
            "errors": ["age proof document is required"],
            "message": "age proof document is required"
        }

    doc_bytes, clean_filename, mime_type, doc_err = validate_document_bytes(
        doc_base64=document_base64,
        doc_filename=document_filename or "age_proof.pdf"
    )
    if doc_err:
        cur.close()
        conn.close()
        return {
            "status": "validation_error",
            "errors": [doc_err],
            "message": doc_err
        }

    # 3. Validate full application using portal rules
    data_with_doc = dict(data)
    data_with_doc["doc_data"] = doc_bytes
    data_with_doc["doc_name"] = clean_filename
    data_with_doc["doc_mime"] = mime_type

    errors, cleaned = portal_app.validate_application(data_with_doc, doc_path=clean_filename)
    if errors:
        cur.close()
        conn.close()
        return {
            "status": "validation_error",
            "errors": errors,
            "message": "; ".join(errors)
        }

    cleaned["doc_data"] = doc_bytes
    cleaned["doc_name"] = clean_filename
    cleaned["doc_mime"] = mime_type
    cleaned["doc_path"] = ""  # Zero filesystem path, stored exclusively in DB

    # 4. Concurrency guard & atomic persistence
    try:
        existing_recheck = portal_app.active_application(cur, mobile)
        if existing_recheck:
            cur.close()
            conn.close()
            return {
                "status": "duplicate_active_application",
                "existing_application": {
                    "application_id": existing_recheck[0],
                    "application_no": existing_recheck[1],
                    "status": existing_recheck[2]
                },
                "message": f"Application {existing_recheck[1]} ({existing_recheck[2]}) already exists for this mobile number."
            }

        new_id, app_no = portal_app.create_application(cur, mobile, cleaned)
        conn.commit()
        cur.close()
        conn.close()

        return {
            "status": "submitted",
            "application_id": new_id,
            "application_no": app_no,
            "applicant_name": cleaned["applicant_name"],
            "mobile": mobile,
            "village": cleaned["village"],
            "block": cleaned["block"],
            "application_status": "PENDING",
            "submitted_at": portal_app.now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "summary": f"Your pension application has been successfully submitted with Application Reference {app_no}. It is currently PENDING review by the Block Social Welfare Officer."
        }
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {"status": "error", "error": "DB_MUTATION_FAILED", "message": f"Failed to submit application: {str(e)}"}


def withdraw_citizen_application(
    mobile: str,
    app_reference: Optional[str] = None
) -> Dict[str, Any]:
    """Withdraw a PENDING application owned by the authenticated citizen.

    Guarantees:
    - Ownership bound strictly to authenticated mobile.
    - Concurrency guard: status must be 'PENDING'.
    - Atomic status transition to 'WITHDRAWN' with audit log.
    - If non-pending or non-owned, returns safe informative error without mutation.
    """
    if not mobile or len(mobile) != 10:
        return {"status": "error", "error": "INVALID_MOBILE", "message": "Invalid mobile number."}

    try:
        conn = portal_app.get_db()
        cur = conn.cursor()

        # 1. Locate application
        if app_reference and app_reference.strip():
            ref = app_reference.strip()
            cur.execute(
                "SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND (application_no ILIKE %s OR id::text = %s)",
                (mobile, ref, ref)
            )
            target = cur.fetchone()
            if not target:
                cur.close()
                conn.close()
                return {
                    "status": "not_found",
                    "message": f"No application matching '{ref}' found under your registered mobile."
                }
        else:
            # Find the active PENDING application
            cur.execute(
                "SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status = 'PENDING' "
                "ORDER BY submitted_at DESC LIMIT 1",
                (mobile,)
            )
            target = cur.fetchone()
            if not target:
                # Check if there is an approved or deemed approved one to provide a helpful message
                cur.execute(
                    "SELECT id, application_no, status FROM applications "
                    "WHERE mobile = %s AND status IN ('APPROVED', 'DEEMED_APPROVED') "
                    "ORDER BY submitted_at DESC LIMIT 1",
                    (mobile,)
                )
                approved = cur.fetchone()
                cur.close()
                conn.close()
                if approved:
                    return {
                        "status": "cannot_withdraw",
                        "application_no": approved[1],
                        "current_status": approved[2],
                        "message": f"Application {approved[1]} is {approved[2]} and cannot be withdrawn. Only PENDING applications may be withdrawn."
                    }
                return {
                    "status": "not_found",
                    "message": "No pending application found to withdraw."
                }

        app_id, app_no, current_status = target
        if current_status != "PENDING":
            cur.close()
            conn.close()
            return {
                "status": "cannot_withdraw",
                "application_no": app_no,
                "current_status": current_status,
                "message": f"Only an application in PENDING status can be withdrawn. Application {app_no} has status {current_status}."
            }

        # Concurrency-safe atomic update
        cur.execute(
            "UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT' "
            "WHERE id = %s AND mobile = %s AND status = 'PENDING' "
            "RETURNING application_no",
            (portal_app.now_ist(), app_id, mobile)
        )
        updated = cur.fetchone()
        if not updated:
            conn.rollback()
            cur.close()
            conn.close()
            return {
                "status": "conflict",
                "message": f"Application {app_no} could not be withdrawn because its status is no longer PENDING."
            }

        portal_app.write_audit(cur, app_id, "WITHDRAW", f"applicant:{mobile}")
        conn.commit()
        cur.close()
        conn.close()

        return {
            "status": "withdrawn",
            "application_id": app_id,
            "application_no": updated[0],
            "withdrawn_at": portal_app.now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "summary": f"Application {updated[0]} has been successfully withdrawn. You may now submit a fresh application."
        }
    except Exception as e:
        return {"status": "error", "error": "DB_ERROR", "message": f"Database failure during withdrawal: {str(e)}"}


def get_pending_queue(
    block: Optional[str] = None,
    limit: int = 20,
    offset: int = 0
) -> Dict[str, Any]:
    """Retrieve paginated list of applications in PENDING status for officer review.

    Guarantees:
    - FIFO ordering: oldest pending cases first to assist with statutory SLA compliance.
    - Data minimization: redacts bank account numbers and document bytes.
    - Bounded pagination: limits clamped between 1 and 50.
    """
    bounded_limit = min(max(1, limit), 50)
    bounded_offset = max(0, offset)

    try:
        conn = portal_app.get_db()
        cur = conn.cursor()

        where = ["status = 'PENDING'"]
        params: List[Any] = []

        if block and block.strip() and block.strip() != "ALL":
            clean_block = block.strip()
            where.append("block = %s")
            params.append(clean_block)

        where_clause = "WHERE " + " AND ".join(where)

        cur.execute(f"SELECT count(*) FROM applications {where_clause}", tuple(params))
        total_pending = cur.fetchone()[0]

        cur.execute(
            f"SELECT id, application_no, applicant_name, village, block, submitted_at "
            f"FROM applications {where_clause} "
            f"ORDER BY submitted_at ASC, id ASC "
            f"LIMIT %s OFFSET %s",
            tuple(params + [bounded_limit, bounded_offset])
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        apps = []
        now = portal_app.now_ist()
        for r in rows:
            app_id, app_no, name, village, blk, sub_at = r
            days_pending = _calculate_days_pending(now, sub_at)
            sla_warning = days_pending >= 12
            apps.append({
                "application_id": app_id,
                "application_no": app_no,
                "applicant_name": name,
                "village": village,
                "block": blk,
                "submitted_at": sub_at.strftime("%Y-%m-%d %H:%M:%S IST") if sub_at else None,
                "days_pending": days_pending,
                "sla_warning": sla_warning
            })

        return {
            "status": "success",
            "total_pending": total_pending,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "count": len(apps),
            "block_filter": block if block else "ALL",
            "applications": apps,
            "summary": f"Found {total_pending} total pending application(s). Showing {len(apps)} (offset {bounded_offset})."
        }
    except Exception as e:
        return {"status": "error", "error": "DB_ERROR", "message": f"Database failure retrieving pending queue: {str(e)}"}


def get_application_dossier(
    app_reference: str
) -> Dict[str, Any]:
    """Retrieve complete application details, eligibility status, document metadata, and audit log.

    Guarantees:
    - Authoritative read from PostgreSQL.
    - Masked bank account (e.g. '********1234') for data minimization.
    - Document metadata verification without binary payload dump.
    - Full audit trail history.
    """
    if not app_reference or not str(app_reference).strip():
        return {"status": "error", "error": "INVALID_REFERENCE", "message": "Application reference is required."}

    ref = str(app_reference).strip()
    try:
        conn = portal_app.get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, dob, gender, "
            "marital_status, husband_name, village, block, bank_account, ifsc, "
            "doc_path, status, submitted_at, decided_at, decided_by, "
            "doc_name, doc_mime, (doc_data IS NOT NULL) AS has_doc_data "
            "FROM applications WHERE application_no ILIKE %s OR id::text = %s",
            (ref, ref)
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return {
                "status": "not_found",
                "message": f"Application '{ref}' not found in the database."
            }

        (app_id, app_no, name, mobile, dob, gender, marital, husband,
         village, block, bank_acc, ifsc, doc_path, status, sub_at, dec_at,
         dec_by, doc_name, doc_mime, has_doc_data) = row

        cur.execute(
            "SELECT at, action, actor, note FROM audit_log "
            "WHERE application_id = %s ORDER BY at ASC, id ASC",
            (app_id,)
        )
        audit_rows = cur.fetchall()
        cur.close()
        conn.close()

        audit_events = []
        for a in audit_rows:
            audit_events.append({
                "at": a[0].strftime("%Y-%m-%d %H:%M:%S IST") if a[0] else None,
                "action": a[1],
                "actor": a[2],
                "note": a[3] or ""
            })

        today = portal_app.now_ist().date()
        age = portal_app.age_on(dob, today) if dob else None
        meets_age = (age >= portal_app.MIN_AGE) if age is not None else False

        clean_bank = (bank_acc or "").strip()
        masked_bank = ("*" * (len(clean_bank) - 4) + clean_bank[-4:]) if len(clean_bank) >= 4 else clean_bank

        has_doc = bool(has_doc_data or (doc_path and os.path.exists(doc_path)))
        resolved_doc_name = doc_name or (os.path.basename(doc_path) if doc_path else "age_proof")
        resolved_mime = doc_mime or ("application/pdf" if str(resolved_doc_name).lower().endswith(".pdf") else "image/jpeg")

        days_pending = _calculate_days_pending(portal_app.now_ist(), sub_at)

        return {
            "status": "found",
            "application_id": app_id,
            "application_no": app_no,
            "applicant_name": name,
            "mobile": mobile,
            "dob": dob.strftime("%d/%m/%Y") if dob else None,
            "calculated_age": age,
            "meets_age_requirement": meets_age,
            "gender": gender,
            "marital_status": marital,
            "husband_name": husband or "",
            "village": village,
            "block": block,
            "bank_account_masked": masked_bank,
            "ifsc": ifsc,
            "application_status": status,
            "submitted_at": sub_at.strftime("%Y-%m-%d %H:%M:%S IST") if sub_at else None,
            "decided_at": dec_at.strftime("%Y-%m-%d %H:%M:%S IST") if dec_at else None,
            "decided_by": dec_by,
            "days_pending": days_pending,
            "sla_deemed_eligible": (days_pending >= portal_app.SLA_DAYS and status == "PENDING"),
            "document": {
                "has_document": has_doc,
                "filename": resolved_doc_name,
                "mime_type": resolved_mime,
                "verified_format": True if has_doc else False
            },
            "audit_history": audit_events,
            "summary": f"Application {app_no} ({name}, age {age}, block {block}) is currently {status} with {len(audit_events)} audit record(s)."
        }
    except Exception as e:
        return {"status": "error", "error": "DB_ERROR", "message": f"Database failure retrieving application: {str(e)}"}


def record_officer_decision(
    app_reference: str,
    decision: str,
    actor_username: str,
    reason: str = ""
) -> Dict[str, Any]:
    """Approve or reject a PENDING application, recording the officer identity and audit trail.

    Guarantees:
    - Enforces decision in {'APPROVE', 'REJECT'}.
    - Mandatory reason for rejections.
    - Concurrency protection: atomic UPDATE WHERE status = 'PENDING'.
    - Stale state detection if application was already decided.
    - Writes audit log row with actor 'officer:<username>'.
    - Commits transaction to database.
    """
    if not app_reference or not str(app_reference).strip():
        return {"status": "error", "error": "INVALID_REFERENCE", "message": "Application reference is required."}

    ref = str(app_reference).strip()
    norm_decision = decision.strip().upper()
    if norm_decision in ("APPROVE", "APPROVED"):
        new_status = "APPROVED"
    elif norm_decision in ("REJECT", "REJECTED"):
        new_status = "REJECTED"
    else:
        return {
            "status": "error",
            "error": "INVALID_DECISION",
            "message": f"Decision must be 'APPROVE' or 'REJECT' (received: '{decision}')."
        }

    clean_reason = (reason or "").strip()
    if new_status == "REJECTED" and not clean_reason:
        return {
            "status": "error",
            "error": "MISSING_REASON",
            "message": "A detailed rejection reason is mandatory when rejecting a pension application."
        }

    actor = (actor_username or "admin").strip()
    actor_str = f"officer:{actor}"

    try:
        conn = portal_app.get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT id, application_no, status FROM applications "
            "WHERE application_no ILIKE %s OR id::text = %s",
            (ref, ref)
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return {"status": "not_found", "message": f"Application '{ref}' not found."}

        app_id = row[0]
        app_no = row[1]
        current_status = row[2] if len(row) == 3 else row[13]
        if current_status != "PENDING":
            cur.close()
            conn.close()
            return {
                "status": "stale_decision",
                "application_id": app_id,
                "application_no": app_no,
                "current_status": current_status,
                "message": f"Application {app_no} cannot be decided because its current status is {current_status}. Only PENDING applications can be decided."
            }

        now = portal_app.now_ist()
        cur.execute(
            "UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
            "WHERE id = %s AND status = 'PENDING' RETURNING application_no",
            (new_status, now, actor, app_id)
        )
        updated = cur.fetchone()
        if not updated:
            conn.rollback()
            cur.close()
            conn.close()
            return {
                "status": "conflict",
                "message": f"Application {app_no} could not be updated due to a concurrent status modification."
            }

        portal_app.write_audit(cur, app_id, new_status, actor_str, clean_reason)
        conn.commit()
        cur.close()
        conn.close()

        return {
            "status": "decided",
            "application_id": app_id,
            "application_no": updated[0],
            "decision": new_status,
            "decided_by": actor,
            "decided_at": now.strftime("%Y-%m-%d %H:%M:%S IST"),
            "reason": clean_reason,
            "summary": f"Application {updated[0]} has been successfully {new_status} by officer {actor}."
        }
    except Exception as e:
        return {"status": "error", "error": "DB_ERROR", "message": f"Database failure recording decision: {str(e)}"}

