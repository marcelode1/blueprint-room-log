from flask import Flask, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os, uuid, zipfile, tempfile, json, mimetypes, smtplib, ssl, secrets, csv, io, urllib.parse, urllib.request, urllib.error, base64, re
import psycopg
from psycopg.rows import dict_row
from supabase import create_client

try:
    import fitz
except Exception:
    fitz = None

try:
    from timezonefinder import TimezoneFinder
except Exception:
    TimezoneFinder = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.permanent_session_lifetime = timedelta(days=int(os.environ.get("STAY_LOGGED_IN_DAYS", "365")))

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "blueprint-files")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME or "no-reply@projectonus.app")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "")
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/New_York")
TIMEZONE_FINDER = TimezoneFinder() if TimezoneFinder else None

COMMON_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Puerto_Rico",
    "UTC",
]

ALLOWED_PHOTOS = {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif"}
ALLOWED_AUDIO = {"webm", "mp3", "m4a", "wav", "ogg"}
ALLOWED_LOGOS = {"png", "jpg", "jpeg", "webp", "gif", "svg"}
ALLOWED_BLUEPRINTS = {"pdf", "png", "jpg", "jpeg", "webp"}
CONTENT_TYPES_BY_EXT = {
    "heic": "image/heic",
    "heif": "image/heif",
}


def file_ext(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def allowed_photo(filename):
    return file_ext(filename) in ALLOWED_PHOTOS


def allowed_blueprint(filename):
    return file_ext(filename) in ALLOWED_BLUEPRINTS


def allowed_audio(filename):
    return file_ext(filename) in ALLOWED_AUDIO


def allowed_logo(filename):
    return file_ext(filename) in ALLOWED_LOGOS


def upload_content_type(filename, fallback="application/octet-stream"):
    return CONTENT_TYPES_BY_EXT.get(file_ext(filename)) or fallback or "application/octet-stream"


def is_pdf(filename):
    return file_ext(filename) == "pdf"


def normalize_database_url(url):
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is missing.")
    return psycopg.connect(normalize_database_url(DATABASE_URL), row_factory=dict_row)


def get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL or SUPABASE_KEY is missing.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def upload_bytes_to_storage(data, filename, content_type="application/octet-stream"):
    safe_name = secure_filename(filename)
    unique_path = f"{datetime.now().strftime('%Y/%m')}/{uuid.uuid4().hex}_{safe_name}"
    get_supabase().storage.from_(SUPABASE_BUCKET).upload(
        unique_path,
        data,
        file_options={"content-type": content_type, "upsert": "false"}
    )
    return unique_path


def upload_file_to_storage(file_storage):
    return upload_bytes_to_storage(
        file_storage.read(),
        file_storage.filename,
        upload_content_type(file_storage.filename, file_storage.content_type)
    )


def first_uploaded_file(*field_names):
    for field_name in field_names:
        uploaded = request.files.get(field_name)
        if uploaded and uploaded.filename:
            return uploaded
    return None


def file_url(path):
    if not path:
        return ""
    try:
        return get_supabase().storage.from_(SUPABASE_BUCKET).get_public_url(path)
    except Exception:
        return ""


def download_storage_file(path):
    try:
        return get_supabase().storage.from_(SUPABASE_BUCKET).download(path)
    except Exception:
        return b""


def external_url(endpoint, **values):
    if APP_BASE_URL:
        return APP_BASE_URL.rstrip("/") + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


def safe_next_url(default_endpoint="index", **values):
    target = request.form.get("next") or request.args.get("next") or request.referrer or ""
    if target.startswith("/"):
        return target
    if target and target.startswith(request.host_url):
        return target
    return url_for(default_endpoint, **values)


def build_full_address(street, city, state, zip_code):
    city_state = ", ".join(part for part in [city, state] if part)
    if zip_code:
        city_state = f"{city_state} {zip_code}".strip()
    return street, ", ".join(part for part in [street, city_state] if part), city, state, zip_code


def project_address_from_form():
    return build_full_address(
        request.form.get("customer_address", "").strip(),
        request.form.get("customer_city", "").strip(),
        request.form.get("customer_state", "").strip().upper(),
        request.form.get("customer_zip", "").strip()
    )


def supplier_address_from_form(prefix="supplier_"):
    return build_full_address(
        request.form.get(prefix + "address", "").strip(),
        request.form.get(prefix + "city", "").strip(),
        request.form.get(prefix + "state", "").strip().upper(),
        request.form.get(prefix + "zip", "").strip()
    )


def billing_address_from_form(customer_address_parts):
    billing_same_as_customer = request.form.get("billing_same_as_customer") == "on"
    if billing_same_as_customer:
        return (True, *customer_address_parts)

    return (
        False,
        *build_full_address(
            request.form.get("billing_street", "").strip(),
            request.form.get("billing_city", "").strip(),
            request.form.get("billing_state", "").strip().upper(),
            request.form.get("billing_zip", "").strip()
        )
    )


def send_email(to_email, subject, body, attachments=None):
    if not SMTP_HOST:
        print("Email not sent: SMTP_HOST is not configured.")
        return False
    try:
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)
        for attachment in attachments or []:
            filename, data, mime_type = attachment
            if not data:
                continue
            maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls(context=ssl.create_default_context())
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print("Email send failed:", e)
        return False


def send_sms(phone_number, body, return_error=False):
    def result(ok, message=""):
        return (ok, message) if return_error else ok

    phone_number = (phone_number or "").strip()
    if not phone_number:
        return result(False, "Cellphone number is missing.")
    missing = []
    if not TWILIO_ACCOUNT_SID:
        missing.append("TWILIO_ACCOUNT_SID")
    if not TWILIO_AUTH_TOKEN:
        missing.append("TWILIO_AUTH_TOKEN")
    if not TWILIO_FROM_NUMBER:
        missing.append("TWILIO_FROM_NUMBER")
    if missing:
        message = "Missing Render environment variable(s): " + ", ".join(missing)
        print("SMS not sent:", message)
        return result(False, message)
    try:
        payload = urllib.parse.urlencode({
            "To": phone_number,
            "From": TWILIO_FROM_NUMBER,
            "Body": body[:1500],
        }).encode("utf-8")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        request_obj = urllib.request.Request(url, data=payload, method="POST")
        token = f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")
        request_obj.add_header("Authorization", "Basic " + base64.b64encode(token).decode("ascii"))
        request_obj.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request_obj, timeout=20) as response:
            ok = 200 <= response.status < 300
            return result(ok, "" if ok else f"Twilio returned HTTP {response.status}.")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            detail = parsed.get("message") or raw
        except Exception:
            detail = str(e)
        message = f"Twilio error: {detail}"
        print("SMS send failed:", message)
        return result(False, message)
    except Exception as e:
        message = f"SMS send failed: {e}"
        print(message)
        return result(False, message)


def new_token():
    return uuid.uuid4().hex + uuid.uuid4().hex


def unusable_password_hash():
    return generate_password_hash(new_token())


def has_admin_account(conn=None):
    close_conn = False
    if conn is None:
        conn = db()
        close_conn = True
    row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'").fetchone()
    if close_conn:
        conn.close()
    return bool(row and row["c"])


def create_pdf_preview_from_bytes(pdf_bytes):
    """
    Convert first PDF page to PNG and upload it to Supabase Storage.
    If conversion fails, the app will fall back to showing the PDF in an iframe.
    """
    if fitz is None:
        print("PDF preview conversion skipped: PyMuPDF/fitz is not available.")
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        png_bytes = pix.tobytes("png")
        doc.close()
        preview_path = upload_bytes_to_storage(
            png_bytes,
            f"blueprint_preview_{uuid.uuid4().hex}.png",
            "image/png"
        )
        print("PDF preview created:", preview_path)
        return preview_path
    except Exception as e:
        print("PDF preview conversion failed:", str(e))
        return None


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        username TEXT,
        email TEXT UNIQUE NOT NULL,
        phone_number TEXT,
        sms_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        password_hash TEXT NOT NULL,
        pin_hash TEXT,
        invite_token TEXT,
        invite_sent_at TEXT,
        reset_token TEXT,
        reset_created_at TEXT,
        setup_token TEXT,
        setup_created_at TEXT,
        role TEXT NOT NULL DEFAULT 'worker',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        customer_name TEXT,
        customer_street TEXT,
        customer_address TEXT,
        customer_city TEXT,
        customer_state TEXT,
        customer_zip TEXT,
        billing_street TEXT,
        billing_address TEXT,
        billing_city TEXT,
        billing_state TEXT,
        billing_zip TEXT,
        billing_same_as_customer BOOLEAN NOT NULL DEFAULT TRUE,
        dtools_cloud_project_ref TEXT,
        customer_phone TEXT,
        customer_email TEXT,
        blueprint_file TEXT,
        blueprint_preview_file TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_blueprints (
        id SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        blueprint_file TEXT NOT NULL,
        blueprint_preview_file TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS material_inventory (
        id SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        item_date TEXT NOT NULL,
        quantity REAL NOT NULL DEFAULT 0,
        part_number TEXT,
        description TEXT NOT NULL,
        material_status TEXT NOT NULL DEFAULT 'not_in_stock',
        picture_file TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rooms (
        id SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        x REAL NOT NULL DEFAULT 0,
        y REAL NOT NULL DEFAULT 0,
        w REAL NOT NULL DEFAULT 0,
        h REAL NOT NULL DEFAULT 0,
        polygon_points TEXT,
        category TEXT DEFAULT 'general',
        room_color TEXT DEFAULT 'blue',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS suppliers (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        contact_name TEXT,
        email TEXT,
        phone TEXT,
        street TEXT,
        address TEXT,
        city TEXT,
        state TEXT,
        zip TEXT,
        website TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory_items (
        id SERIAL PRIMARY KEY,
        item_date TEXT NOT NULL,
        quantity REAL NOT NULL DEFAULT 0,
        item_name TEXT NOT NULL,
        item_model TEXT,
        brand TEXT,
        item_condition TEXT NOT NULL DEFAULT 'new',
        location_type TEXT NOT NULL DEFAULT 'warehouse',
        location_detail TEXT,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
        supplier_pickup_time TEXT,
        status TEXT NOT NULL DEFAULT 'available',
        added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        used_at TEXT,
        used_note TEXT,
        picture_file TEXT,
        supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
        purchased_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        purchased_at TEXT,
        legacy_material_id INTEGER UNIQUE,
        dtools_cloud_source_id TEXT,
        dtools_cloud_item_id TEXT,
        dtools_cloud_project_ref TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        subscription_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS login_events (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        project_id INTEGER,
        task_id INTEGER,
        user_name TEXT,
        user_email TEXT,
        role TEXT,
        event_type TEXT NOT NULL DEFAULT 'login',
        message TEXT,
        is_read BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_permissions (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        see_comments BOOLEAN NOT NULL DEFAULT TRUE,
        write_comments BOOLEAN NOT NULL DEFAULT FALSE,
        edit_comments BOOLEAN NOT NULL DEFAULT FALSE,
        delete_comments BOOLEAN NOT NULL DEFAULT FALSE,
        see_pictures BOOLEAN NOT NULL DEFAULT TRUE,
        add_pictures BOOLEAN NOT NULL DEFAULT FALSE,
        delete_pictures BOOLEAN NOT NULL DEFAULT FALSE,
        see_audio BOOLEAN NOT NULL DEFAULT TRUE,
        add_audio BOOLEAN NOT NULL DEFAULT FALSE,
        delete_audio BOOLEAN NOT NULL DEFAULT FALSE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_permissions (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, project_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id SERIAL PRIMARY KEY,
        room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        note_date TEXT NOT NULL,
        comment TEXT NOT NULL,
        photo_file TEXT,
        audio_file TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        task_number TEXT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
        assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        task_date TEXT NOT NULL,
        task_start_date TEXT,
        task_start_time TEXT,
        task_end_date TEXT,
        title TEXT NOT NULL,
        instructions TEXT,
        task_photo_file TEXT,
        task_audio_file TEXT,
        supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
        supplier_inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL,
        require_picture BOOLEAN NOT NULL DEFAULT FALSE,
        allow_picture_upload BOOLEAN NOT NULL DEFAULT TRUE,
        allow_comment BOOLEAN NOT NULL DEFAULT TRUE,
        allow_audio BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL DEFAULT 'open',
        accepted_at TEXT,
        completion_comment TEXT,
        completion_photo_file TEXT,
        completion_audio_file TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_number_counters (
        month_key TEXT PRIMARY KEY,
        next_sequence INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_attachments (
        id SERIAL PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
        file_type TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        original_filename TEXT,
        comment TEXT,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_room_statuses (
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
        is_done BOOLEAN NOT NULL DEFAULT FALSE,
        updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        updated_at TEXT,
        PRIMARY KEY (task_id, room_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_supplier_items (
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, inventory_item_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance_events (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        event_type TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        address TEXT,
        event_timezone TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS project_delete_codes (
        id SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        pin_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_delete_codes (
        id SERIAL PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        pin_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS room_delete_codes (
        id SERIAL PRIMARY KEY,
        room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
        admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        pin_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS worker_location_pings (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        attendance_event_id INTEGER REFERENCES attendance_events(id) ON DELETE SET NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        accuracy REAL,
        address TEXT,
        event_timezone TEXT,
        created_at TEXT NOT NULL
    )
    """)

    conn.commit()

    # Safe migrations for older deployments
    migrations = [
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_name TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_street TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_address TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_city TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_state TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_zip TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS billing_street TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS billing_address TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS billing_city TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS billing_state TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS billing_zip TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS billing_same_as_customer BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS dtools_cloud_project_ref TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_phone TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_email TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS sms_enabled BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_hash TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_sent_at TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_created_at TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS setup_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS setup_created_at TEXT",
        "ALTER TABLE notes ADD COLUMN IF NOT EXISTS audio_file TEXT",
        "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS blueprint_id INTEGER REFERENCES project_blueprints(id) ON DELETE SET NULL",
        "ALTER TABLE project_blueprints ADD COLUMN IF NOT EXISTS blueprint_preview_file TEXT",
        "ALTER TABLE project_blueprints DROP COLUMN IF EXISTS blueprint_id",
        "ALTER TABLE user_permissions ADD COLUMN IF NOT EXISTS create_rooms BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE user_permissions ADD COLUMN IF NOT EXISTS view_inventory BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE user_permissions ADD COLUMN IF NOT EXISTS edit_inventory BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE TABLE IF NOT EXISTS suppliers (id SERIAL PRIMARY KEY, name TEXT NOT NULL, contact_name TEXT, email TEXT, phone TEXT, street TEXT, address TEXT, city TEXT, state TEXT, zip TEXT, website TEXT, notes TEXT, created_at TEXT NOT NULL, updated_at TEXT)",
        "CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, task_number TEXT, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL, assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_by INTEGER REFERENCES users(id) ON DELETE SET NULL, task_date TEXT NOT NULL, title TEXT NOT NULL, instructions TEXT, require_picture BOOLEAN NOT NULL DEFAULT FALSE, allow_picture_upload BOOLEAN NOT NULL DEFAULT TRUE, allow_comment BOOLEAN NOT NULL DEFAULT TRUE, allow_audio BOOLEAN NOT NULL DEFAULT TRUE, status TEXT NOT NULL DEFAULT 'open', completion_comment TEXT, completion_photo_file TEXT, completion_audio_file TEXT, completion_at TEXT, created_at TEXT NOT NULL)",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_number TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS tasks_task_number_idx ON tasks(task_number) WHERE task_number IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS task_number_counters (month_key TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_attachments (id SERIAL PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL, file_type TEXT NOT NULL, storage_path TEXT NOT NULL, original_filename TEXT, comment TEXT, created_by INTEGER REFERENCES users(id) ON DELETE SET NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_room_statuses (task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, is_done BOOLEAN NOT NULL DEFAULT FALSE, updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL, updated_at TEXT, PRIMARY KEY (task_id, room_id))",
        "CREATE TABLE IF NOT EXISTS task_supplier_items (task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, inventory_item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE, created_at TEXT NOT NULL, PRIMARY KEY (task_id, inventory_item_id))",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completion_audio_file TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS accepted_at TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_start_date TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_start_time TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_end_date TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_photo_file TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_audio_file TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS supplier_inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL",
        "ALTER TABLE tasks DROP COLUMN IF EXISTS completion_at",
        "CREATE TABLE IF NOT EXISTS attendance_events (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL, event_type TEXT NOT NULL, latitude REAL, longitude REAL, address TEXT, event_timezone TEXT, created_at TEXT NOT NULL)",
        "ALTER TABLE attendance_events ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL",
        "ALTER TABLE attendance_events ADD COLUMN IF NOT EXISTS event_timezone TEXT",
        "CREATE TABLE IF NOT EXISTS project_delete_codes (id SERIAL PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE, pin_hash TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS task_delete_codes (id SERIAL PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE, pin_hash TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS room_delete_codes (id SERIAL PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE, admin_id INTEGER REFERENCES users(id) ON DELETE CASCADE, pin_hash TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS worker_location_pings (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL, attendance_event_id INTEGER REFERENCES attendance_events(id) ON DELETE SET NULL, latitude REAL NOT NULL, longitude REAL NOT NULL, accuracy REAL, address TEXT, event_timezone TEXT, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS inventory_items (id SERIAL PRIMARY KEY, item_date TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0, item_name TEXT NOT NULL, item_model TEXT, brand TEXT, item_condition TEXT NOT NULL DEFAULT 'new', location_type TEXT NOT NULL DEFAULT 'warehouse', location_detail TEXT, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL, room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL, status TEXT NOT NULL DEFAULT 'available', added_by INTEGER REFERENCES users(id) ON DELETE SET NULL, used_by INTEGER REFERENCES users(id) ON DELETE SET NULL, used_at TEXT, used_note TEXT, picture_file TEXT, legacy_material_id INTEGER UNIQUE, created_at TEXT NOT NULL, updated_at TEXT)",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS item_date TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS quantity REAL NOT NULL DEFAULT 0",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS item_name TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS item_model TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS brand TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS item_condition TEXT NOT NULL DEFAULT 'new'",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS location_type TEXT NOT NULL DEFAULT 'warehouse'",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS location_detail TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS supplier_pickup_time TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'available'",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS added_by INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS used_by INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS used_at TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS used_note TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS picture_file TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS purchased_by INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS purchased_at TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS legacy_material_id INTEGER",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS dtools_cloud_source_id TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS dtools_cloud_item_id TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS dtools_cloud_project_ref TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS created_at TEXT",
        "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS updated_at TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS inventory_items_legacy_material_id_idx ON inventory_items(legacy_material_id)",
        """
        INSERT INTO inventory_items
        (item_date, quantity, item_name, item_model, brand, item_condition, location_type, location_detail, project_id, room_id, status, added_by, used_by, used_at, used_note, picture_file, legacy_material_id, created_at, updated_at)
        SELECT material_inventory.item_date, material_inventory.quantity, COALESCE(NULLIF(material_inventory.description, ''), 'Material item'), material_inventory.part_number, '', 'new', 'job_site', '', material_inventory.project_id, NULL,
               CASE WHEN material_inventory.material_status = 'in_stock' THEN 'available' WHEN material_inventory.material_status = 'used' THEN 'used' ELSE 'needs_purchase' END,
               material_inventory.user_id,
               CASE WHEN material_inventory.material_status = 'used' THEN material_inventory.user_id ELSE NULL END,
               CASE WHEN material_inventory.material_status = 'used' THEN material_inventory.created_at ELSE NULL END,
               '', material_inventory.picture_file, material_inventory.id, material_inventory.created_at, material_inventory.created_at
        FROM material_inventory
        WHERE NOT EXISTS (SELECT 1 FROM inventory_items WHERE inventory_items.legacy_material_id = material_inventory.id)
        """,
        "CREATE TABLE IF NOT EXISTS project_blueprints (id SERIAL PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL, blueprint_file TEXT NOT NULL, blueprint_preview_file TEXT, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS login_events (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, project_id INTEGER, task_id INTEGER, user_name TEXT, user_email TEXT, role TEXT, event_type TEXT NOT NULL DEFAULT 'login', message TEXT, is_read BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL)",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS project_id INTEGER",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS task_id INTEGER",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS user_name TEXT",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS user_email TEXT",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS role TEXT",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'login'",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS message TEXT",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS is_read BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE login_events ADD COLUMN IF NOT EXISTS created_at TEXT",
        "CREATE TABLE IF NOT EXISTS user_permissions (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, see_comments BOOLEAN NOT NULL DEFAULT TRUE, write_comments BOOLEAN NOT NULL DEFAULT FALSE, edit_comments BOOLEAN NOT NULL DEFAULT FALSE, delete_comments BOOLEAN NOT NULL DEFAULT FALSE, see_pictures BOOLEAN NOT NULL DEFAULT TRUE, add_pictures BOOLEAN NOT NULL DEFAULT FALSE, delete_pictures BOOLEAN NOT NULL DEFAULT FALSE, see_audio BOOLEAN NOT NULL DEFAULT TRUE, add_audio BOOLEAN NOT NULL DEFAULT FALSE, delete_audio BOOLEAN NOT NULL DEFAULT FALSE, create_rooms BOOLEAN NOT NULL DEFAULT FALSE)",
        "CREATE TABLE IF NOT EXISTS project_permissions (user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, created_at TEXT NOT NULL, PRIMARY KEY (user_id, project_id))",
        "DELETE FROM users WHERE lower(email) = 'admin@example.com'"
    ]
    for sql in migrations:
        try:
            cur.execute(sql)
        except Exception as e:
            print("Migration skipped:", sql, e)
    conn.commit()

    try:
        assign_missing_task_numbers(conn)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("Task number backfill skipped:", e)

    conn.close()


def task_number_month_key(value=None):
    dt = local_datetime(value) if value else None
    if dt:
        return dt.strftime("%Y%m")
    text = str(value or "").strip()
    if text:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y%m")
        except Exception:
            pass
    return local_now().strftime("%Y%m")


def task_number_for_sequence(month_key, sequence):
    return f"{month_key}{int(sequence):04d}"


def next_task_number(conn, reference_value=None):
    month_key = task_number_month_key(reference_value)
    row = conn.execute(
        """
        INSERT INTO task_number_counters (month_key, next_sequence, updated_at)
        VALUES (%s, 1, %s)
        ON CONFLICT (month_key) DO UPDATE SET
            next_sequence = task_number_counters.next_sequence + 1,
            updated_at = EXCLUDED.updated_at
        RETURNING next_sequence - 1 AS sequence_number
        """,
        (month_key, utc_now_iso())
    ).fetchone()
    return task_number_for_sequence(month_key, row["sequence_number"])


def sync_task_number_counters(conn):
    rows = conn.execute(
        "SELECT task_number FROM tasks WHERE task_number IS NOT NULL AND task_number <> ''"
    ).fetchall()
    max_by_month = {}
    for row in rows:
        number = str(row.get("task_number") or "").strip()
        if len(number) < 10 or not number[:6].isdigit() or not number[6:].isdigit():
            continue
        month_key = number[:6]
        sequence = int(number[6:])
        max_by_month[month_key] = max(sequence, max_by_month.get(month_key, -1))
    for month_key, max_sequence in max_by_month.items():
        conn.execute(
            """
            INSERT INTO task_number_counters (month_key, next_sequence, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (month_key) DO UPDATE SET
                next_sequence = GREATEST(task_number_counters.next_sequence, EXCLUDED.next_sequence),
                updated_at = EXCLUDED.updated_at
            """,
            (month_key, max_sequence + 1, utc_now_iso())
        )


def assign_missing_task_numbers(conn):
    sync_task_number_counters(conn)
    rows = conn.execute(
        """
        SELECT id, created_at, task_start_date, task_date
        FROM tasks
        WHERE task_number IS NULL OR task_number = ''
        ORDER BY COALESCE(created_at, task_start_date, task_date), id
        """
    ).fetchall()
    for row in rows:
        reference_value = row.get("created_at") or row.get("task_start_date") or row.get("task_date")
        conn.execute(
            "UPDATE tasks SET task_number = %s WHERE id = %s",
            (next_task_number(conn, reference_value), row["id"])
        )


def task_display_name(task):
    title = (task or {}).get("title") or (task or {}).get("task_title") or "Task"
    number = (task or {}).get("task_number")
    return f"{number} - {title}" if number else title


GENERIC_PHOTO_FILENAMES = {
    "image.jpg", "image.jpeg", "image.png",
    "photo.jpg", "photo.jpeg", "photo.png",
    "picture.jpg", "picture.jpeg", "picture.png",
    "marked_picture.jpg", "blob", "file.jpg",
}


def phone_style_photo_filename(extension="jpg"):
    safe_ext = extension if extension in ALLOWED_PHOTOS else "jpg"
    return f"IMG_{local_now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6].upper()}.{safe_ext}"


def task_attachment_display_filename(file_storage, field_name, file_type):
    original = (file_storage.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    safe_original = secure_filename(original)
    lower_name = safe_original.lower()
    original_ext = file_ext(safe_original)
    if file_type == "photo" and ("camera" in (field_name or "").lower() or lower_name in GENERIC_PHOTO_FILENAMES):
        return phone_style_photo_filename(original_ext)
    if original:
        return original
    extension = "webm" if file_type == "audio" else "jpg"
    return f"{file_type}_{local_now().strftime('%Y%m%d_%H%M%S')}.{extension}"


def project_room_id_or_none(conn, project_id, value):
    room_id = optional_int(value)
    if not room_id:
        return None
    room = conn.execute(
        "SELECT id FROM rooms WHERE id = %s AND project_id = %s",
        (room_id, project_id)
    ).fetchone()
    return room["id"] if room else None


def collect_task_attachment_uploads(conn, project_id, default_room_id=None):
    uploads = []
    related_room_ids = set()
    indexes = [idx for idx in request.form.getlist("attachment_indexes") if str(idx).strip()]

    def add_upload(field_name, room_id, comment, file_type):
        uploaded = request.files.get(field_name)
        if not uploaded or not uploaded.filename:
            return None
        if file_type == "photo" and not allowed_photo(uploaded.filename):
            return "Please upload a valid task picture."
        if file_type == "audio" and not allowed_audio(uploaded.filename):
            return "Please upload a valid task audio file."
        data = uploaded.read()
        if not data:
            return None
        display_name = task_attachment_display_filename(uploaded, field_name, file_type)
        uploads.append({
            "room_id": room_id,
            "file_type": file_type,
            "data": data,
            "filename": display_name,
            "content_type": upload_content_type(
                display_name,
                uploaded.content_type or ("audio/webm" if file_type == "audio" else "image/jpeg")
            ),
            "comment": comment,
        })
        if room_id:
            related_room_ids.add(room_id)
        return None

    for idx in indexes:
        requested_room = request.form.get(f"attachment_{idx}_room_id", "")
        room_id = project_room_id_or_none(conn, project_id, requested_room)
        if requested_room and not room_id:
            return "Choose a room that belongs to this project.", [], set()
        comment = request.form.get(f"attachment_{idx}_comment", "").strip()
        for field_name, file_type in [
            (f"attachment_{idx}_photo", "photo"),
            (f"attachment_{idx}_camera", "photo"),
            (f"attachment_{idx}_audio", "audio"),
        ]:
            error = add_upload(field_name, room_id, comment, file_type)
            if error:
                return error, [], set()

    if not indexes:
        comment = request.form.get("task_attachment_comment", "").strip()
        for field_name, file_type in [
            ("task_photo", "photo"),
            ("task_camera_photo", "photo"),
            ("task_audio", "audio"),
        ]:
            error = add_upload(field_name, default_room_id, comment, file_type)
            if error:
                return error, [], set()

    return None, uploads, related_room_ids


def collect_completion_uploads(conn, project_id, default_room_id=None):
    uploads = []
    indexes = [idx for idx in request.form.getlist("completion_attachment_indexes") if str(idx).strip()]

    def add_upload(field_name, room_id, comment, file_type):
        uploaded = request.files.get(field_name)
        if not uploaded or not uploaded.filename:
            return None
        if file_type == "photo" and not allowed_photo(uploaded.filename):
            return "Please upload a valid completion picture."
        if file_type == "audio" and not allowed_audio(uploaded.filename):
            return "Please upload a valid completion audio file."
        data = uploaded.read()
        if not data:
            return None
        display_name = task_attachment_display_filename(uploaded, field_name, file_type)
        uploads.append({
            "room_id": room_id,
            "file_type": file_type,
            "data": data,
            "filename": display_name,
            "content_type": upload_content_type(
                display_name,
                uploaded.content_type or ("audio/webm" if file_type == "audio" else "image/jpeg")
            ),
            "comment": comment,
        })
        return None

    if indexes:
        for idx in indexes:
            room_id = default_room_id
            requested_room = request.form.get(f"completion_attachment_{idx}_room_id", "")
            if requested_room:
                room_id = project_room_id_or_none(conn, project_id, requested_room)
                if not room_id:
                    return "Choose a room that belongs to this project.", []
            comment = request.form.get(f"completion_attachment_{idx}_comment", "").strip()
            for field_name, file_type in [
                (f"completion_attachment_{idx}_camera", "photo"),
                (f"completion_attachment_{idx}_photo", "photo"),
                (f"completion_attachment_{idx}_audio", "audio"),
            ]:
                error = add_upload(field_name, room_id, comment, file_type)
                if error:
                    return error, []
    else:
        comment = request.form.get("completion_comment", "").strip()
        for field_name, file_type in [
            ("completion_camera", "photo"),
            ("completion_photo", "photo"),
            ("completion_audio", "audio"),
        ]:
            error = add_upload(field_name, default_room_id, comment, file_type)
            if error:
                return error, []

    return None, uploads


def insert_task_attachments(conn, task_id, uploads):
    inserted = []
    first_photo = None
    first_audio = None
    related_room_ids = set()
    for item in uploads:
        storage_path = upload_bytes_to_storage(item["data"], item["filename"], item["content_type"])
        attachment = conn.execute(
            """
            INSERT INTO task_attachments
            (task_id, room_id, file_type, storage_path, original_filename, comment, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                task_id,
                item.get("room_id"),
                item["file_type"],
                storage_path,
                item["filename"],
                item.get("comment", ""),
                session.get("user_id"),
                utc_now_iso(),
            )
        ).fetchone()
        inserted.append(attachment)
        if item.get("room_id"):
            related_room_ids.add(item["room_id"])
        if item["file_type"] == "photo" and not first_photo:
            first_photo = storage_path
        if item["file_type"] == "audio" and not first_audio:
            first_audio = storage_path
    return inserted, first_photo, first_audio, related_room_ids


def apply_task_legacy_media(conn, task, first_photo=None, first_audio=None):
    updates = []
    params = []
    if first_photo and not task.get("task_photo_file"):
        updates.append("task_photo_file = %s")
        params.append(first_photo)
    if first_audio and not task.get("task_audio_file"):
        updates.append("task_audio_file = %s")
        params.append(first_audio)
    if not updates:
        return task
    params.append(task["id"])
    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s", tuple(params))
    refreshed = conn.execute("SELECT * FROM tasks WHERE id = %s", (task["id"],)).fetchone()
    return refreshed or task


def load_task_attachments(conn, task_id, room_id=None):
    where = ["task_attachments.task_id = %s"]
    params = [task_id]
    if room_id:
        where.append("(task_attachments.room_id = %s OR task_attachments.room_id IS NULL)")
        params.append(room_id)
    return conn.execute(
        """
        SELECT task_attachments.*, rooms.name AS room_name, users.name AS created_by_name
        FROM task_attachments
        LEFT JOIN rooms ON task_attachments.room_id = rooms.id
        LEFT JOIN users ON task_attachments.created_by = users.id
        WHERE """ + " AND ".join(where) + """
        ORDER BY task_attachments.id
        """,
        tuple(params)
    ).fetchall()


def load_task_details(conn, tasks, room_id=None):
    detailed = []
    for task_row in tasks:
        task = dict(task_row)
        attachments = load_task_attachments(conn, task["id"], room_id)
        task["_attachments"] = attachments
        attachments_by_room = {}
        global_attachments = []
        for attachment in attachments:
            if attachment.get("room_id"):
                attachments_by_room.setdefault(attachment["room_id"], []).append(attachment)
            else:
                global_attachments.append(attachment)
        task["_attachments_by_room"] = attachments_by_room
        task["_global_attachments"] = global_attachments
        task["_supplier"] = None
        task["_supplier_inventory_item"] = None
        if task.get("supplier_id"):
            task["_supplier"] = conn.execute("SELECT * FROM suppliers WHERE id = %s", (task["supplier_id"],)).fetchone()
        if task.get("supplier_inventory_item_id"):
            task["_supplier_inventory_item"] = conn.execute(
                "SELECT * FROM inventory_items WHERE id = %s",
                (task["supplier_inventory_item_id"],)
            ).fetchone()
        task["_supplier_inventory_items"] = conn.execute(
            """
            SELECT inventory_items.*, projects.name AS project_name, rooms.name AS room_name
            FROM task_supplier_items
            JOIN inventory_items ON task_supplier_items.inventory_item_id = inventory_items.id
            LEFT JOIN projects ON inventory_items.project_id = projects.id
            LEFT JOIN rooms ON inventory_items.room_id = rooms.id
            WHERE task_supplier_items.task_id = %s
            ORDER BY task_supplier_items.created_at, inventory_items.id
            """,
            (task["id"],)
        ).fetchall()
        if task["_supplier_inventory_items"] and not task["_supplier_inventory_item"]:
            task["_supplier_inventory_item"] = task["_supplier_inventory_items"][0]
        room_ids = set()
        if room_id:
            room_ids.add(room_id)
        elif task.get("room_id"):
            room_ids.add(task["room_id"])
        for attachment in attachments:
            if attachment.get("room_id"):
                room_ids.add(attachment["room_id"])
        room_statuses = []
        if room_ids:
            room_rows = conn.execute(
                "SELECT id, name FROM rooms WHERE id = ANY(%s) ORDER BY name",
                (list(room_ids),)
            ).fetchall()
            status_rows = conn.execute(
                "SELECT room_id, is_done, updated_at FROM task_room_statuses WHERE task_id = %s AND room_id = ANY(%s)",
                (task["id"], list(room_ids))
            ).fetchall()
            status_by_room = {row["room_id"]: row for row in status_rows}
            for room in room_rows:
                status = status_by_room.get(room["id"])
                room_statuses.append({
                    "room_id": room["id"],
                    "room_name": room["name"],
                    "is_done": bool(status.get("is_done")) if status else False,
                    "updated_at": status.get("updated_at") if status else None,
                })
        task["_room_statuses"] = room_statuses
        detailed.append(task)
    return detailed


def task_related_room_ids(conn, task_id, task=None):
    room_ids = set()
    if task and task.get("room_id"):
        room_ids.add(task["room_id"])
    rows = conn.execute(
        "SELECT DISTINCT room_id FROM task_attachments WHERE task_id = %s AND room_id IS NOT NULL",
        (task_id,)
    ).fetchall()
    for row in rows:
        if row.get("room_id"):
            room_ids.add(row["room_id"])
    rows = conn.execute(
        "SELECT DISTINCT room_id FROM task_room_statuses WHERE task_id = %s",
        (task_id,)
    ).fetchall()
    for row in rows:
        if row.get("room_id"):
            room_ids.add(row["room_id"])
    return room_ids


def all_task_rooms_done(conn, task_id, room_ids):
    if not room_ids:
        return False
    rows = conn.execute(
        "SELECT room_id, is_done FROM task_room_statuses WHERE task_id = %s AND room_id = ANY(%s)",
        (task_id, list(room_ids))
    ).fetchall()
    done_by_room = {row["room_id"]: bool(row["is_done"]) for row in rows}
    return all(done_by_room.get(room_id) for room_id in room_ids)


def task_with_attachments_for_email(conn, task):
    task_copy = dict(task)
    task_copy["_attachments"] = load_task_attachments(conn, task["id"])
    return task_copy


def task_room_attachments(task, room_id):
    if not task or not room_id:
        return []
    room_specific = (task.get("_attachments_by_room") or {}).get(room_id, [])
    if task.get("room_id") == room_id:
        return list(task.get("_global_attachments") or []) + list(room_specific)
    return list(room_specific)


def ensure_project_blueprints(conn, project):
    if not project:
        return
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM project_blueprints WHERE project_id = %s",
            (project["id"],)
        ).fetchone()["c"]
        main_blueprint_id = None
        if count == 0 and project.get("blueprint_file"):
            new_bp = conn.execute(
                "INSERT INTO project_blueprints (project_id, name, blueprint_file, blueprint_preview_file, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    project["id"],
                    "Main Blueprint",
                    project.get("blueprint_file"),
                    project.get("blueprint_preview_file"),
                    datetime.now().isoformat()
                )
            ).fetchone()
            main_blueprint_id = new_bp["id"] if new_bp else None
        else:
            main_bp = conn.execute(
                "SELECT id FROM project_blueprints WHERE project_id = %s ORDER BY id LIMIT 1",
                (project["id"],)
            ).fetchone()
            main_blueprint_id = main_bp["id"] if main_bp else None

        conn.execute(
            "UPDATE rooms SET blueprint_id = NULL WHERE project_id = %s AND COALESCE(polygon_points, '') = ''",
            (project["id"],)
        )
        if main_blueprint_id:
            conn.execute(
                "UPDATE rooms SET blueprint_id = %s WHERE project_id = %s AND blueprint_id IS NULL AND COALESCE(polygon_points, '') <> ''",
                (main_blueprint_id, project["id"])
            )
        conn.commit()
    except Exception as e:
        print("ensure_project_blueprints skipped:", e)


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/mobile"):
                return redirect(url_for("mobile_login"))
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def is_main_admin():
    return session.get("role") == "admin"


def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not is_main_admin():
            flash("Only the main admin can do that.")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper



def default_permissions_for_role(role):
    if role == "admin":
        return {k: True for k in PERMISSION_KEYS}
    if role == "worker":
        return {
            "see_comments": True, "write_comments": True, "edit_comments": False, "delete_comments": False,
            "see_pictures": True, "add_pictures": True, "delete_pictures": False,
            "see_audio": True, "add_audio": True, "delete_audio": False, "create_rooms": False,
            "view_inventory": False, "edit_inventory": False,
        }
    return {
        "see_comments": True, "write_comments": False, "edit_comments": False, "delete_comments": False,
        "see_pictures": True, "add_pictures": False, "delete_pictures": False,
        "see_audio": True, "add_audio": False, "delete_audio": False, "create_rooms": False,
        "view_inventory": False, "edit_inventory": False,
    }


PERMISSION_KEYS = [
    "see_comments", "write_comments", "edit_comments", "delete_comments",
    "see_pictures", "add_pictures", "delete_pictures",
    "see_audio", "add_audio", "delete_audio", "create_rooms",
    "view_inventory", "edit_inventory"
]


def get_user_permissions(user_id=None):
    if session.get("role") == "admin":
        return {k: True for k in PERMISSION_KEYS}
    uid = user_id or session.get("user_id")
    role = session.get("role", "customer")
    perms = default_permissions_for_role(role)
    if not uid:
        return perms
    try:
        conn = db()
        row = conn.execute("SELECT * FROM user_permissions WHERE user_id = %s", (uid,)).fetchone()
        conn.close()
        if row:
            for k in PERMISSION_KEYS:
                perms[k] = bool(row.get(k))
    except Exception as e:
        print("Permission lookup failed:", e)
    return perms


def has_perm(permission):
    if session.get("role") == "admin":
        return True
    return bool(get_user_permissions().get(permission))


def get_app_setting(key, default=""):
    try:
        conn = db()
        row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
        conn.close()
        return row["value"] if row and row.get("value") else default
    except Exception:
        return default


def set_app_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value)
    )
    conn.commit()
    conn.close()


def setting_enabled(key, default=True):
    default_value = "1" if default else "0"
    return get_app_setting(key, default_value) == "1"


def admin_unread_count():
    if session.get("role") != "admin":
        return 0
    try:
        conn = db()
        row = conn.execute("SELECT COUNT(*) AS c FROM login_events WHERE is_read = FALSE AND event_type NOT IN ('login', 'task_assigned')").fetchone()
        conn.close()
        return row["c"] if row else 0
    except Exception:
        return 0


def unread_notification_count():
    if "user_id" not in session:
        return 0
    try:
        conn = db()
        if session.get("role") == "admin":
            row = conn.execute("SELECT COUNT(*) AS c FROM login_events WHERE is_read = FALSE AND event_type NOT IN ('login', 'task_assigned')").fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM login_events
                JOIN tasks ON login_events.task_id = tasks.id
                JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
                WHERE login_events.is_read = FALSE
                  AND login_events.user_id = %s
                  AND login_events.event_type = 'task_assigned'
                """,
                (session.get("user_id"), session.get("user_id"))
            ).fetchone()
        conn.close()
        return row["c"] if row else 0
    except Exception:
        return 0


def notification_summary():
    if "user_id" not in session:
        return {"unread_count": 0, "latest": None}
    conn = db()
    if session.get("role") == "admin":
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM login_events WHERE is_read = FALSE AND event_type NOT IN ('login', 'task_assigned')"
        ).fetchone()
        latest = conn.execute(
            """
            SELECT login_events.id, login_events.event_type, login_events.message, login_events.created_at,
                   login_events.task_id, tasks.task_number, tasks.title AS task_title, projects.name AS project_name
            FROM login_events
            LEFT JOIN tasks ON login_events.task_id = tasks.id
            LEFT JOIN projects ON COALESCE(login_events.project_id, tasks.project_id) = projects.id
            WHERE login_events.is_read = FALSE
              AND login_events.event_type NOT IN ('login', 'task_assigned')
            ORDER BY login_events.id DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        count_row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM login_events
            JOIN tasks ON login_events.task_id = tasks.id
            JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
            WHERE login_events.is_read = FALSE
              AND login_events.user_id = %s
              AND login_events.event_type = 'task_assigned'
            """,
            (session.get("user_id"), session.get("user_id"))
        ).fetchone()
        latest = conn.execute(
            """
            SELECT login_events.id, login_events.event_type, login_events.message, login_events.created_at,
                   login_events.task_id, tasks.task_number, tasks.title AS task_title, projects.name AS project_name
            FROM login_events
            JOIN tasks ON login_events.task_id = tasks.id
            JOIN projects ON tasks.project_id = projects.id
            JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
            WHERE login_events.is_read = FALSE
              AND login_events.user_id = %s
              AND login_events.event_type = 'task_assigned'
            ORDER BY login_events.id DESC
            LIMIT 1
            """,
            (session.get("user_id"), session.get("user_id"))
        ).fetchone()
    conn.close()
    latest_data = None
    if latest:
        latest_url = url_for("notifications")
        if session.get("role") != "admin" and latest.get("task_id"):
            latest_url = url_for("my_tasks") + f"#task-{latest.get('task_id')}"
        latest_data = {
            "id": latest.get("id"),
            "event_type": latest.get("event_type"),
            "message": latest.get("message") or "",
            "task_id": latest.get("task_id"),
            "task_title": task_display_name(latest) if latest.get("task_title") else "",
            "task_number": latest.get("task_number") or "",
            "project_name": latest.get("project_name") or "",
            "created_at": latest.get("created_at") or "",
            "url": latest_url
        }
    return {"unread_count": count_row["c"] if count_row else 0, "latest": latest_data}


def add_notification(conn, user_id, user_name, user_email, role, event_type, project_id=None, task_id=None, message=None):
    conn.execute(
        """
        INSERT INTO login_events
        (user_id, project_id, task_id, user_name, user_email, role, event_type, message, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (user_id, project_id, task_id, user_name, user_email, role, event_type, message, utc_now_iso())
    )


def storage_attachment(path, display_name=None):
    try:
        if not path:
            return None
        data = download_storage_file(path)
        if not data:
            return None
        filename = secure_filename(display_name or "") or os.path.basename(path)
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return (filename, data, mime_type)
    except Exception as e:
        print("Storage attachment skipped:", e)
        return None


def admin_email_rows(conn):
    return conn.execute("SELECT email FROM users WHERE role = 'admin' ORDER BY id").fetchall()


def notify_admins_of_field_note(conn, project, room, comment, photo_file, audio_file, note_date):
    try:
        actor = conn.execute(
            "SELECT name, email, role FROM users WHERE id = %s",
            (session.get("user_id"),)
        ).fetchone() or {}
        actor_name = actor.get("name") or session.get("name")
        actor_email = actor.get("email") or ""
        actor_role = actor.get("role") or session.get("role")
        notification_types = []
        note_parts = []
        if comment:
            notification_types.append("field_comment_added")
            note_parts.append("comment")
        if photo_file:
            notification_types.append("field_picture_added")
            note_parts.append("picture")
        if audio_file:
            notification_types.append("field_audio_added")
            note_parts.append("audio")
        if not notification_types:
            notification_types.append("field_note_added")
            note_parts.append("field note")
        message = f"{actor_name or 'User'} added {', '.join(note_parts)} in {room.get('name') if room else 'room'}."
        project_id = project.get("id") if project else None
        for event_type in notification_types:
            add_notification(conn, session.get("user_id"), actor_name, actor_email, actor_role, event_type, project_id, None, message)
        conn.commit()

        send_comments = setting_enabled("email_note_comments", True)
        send_pictures = setting_enabled("email_note_pictures", True)
        send_audio = setting_enabled("email_note_audio", True)
        wants_email = (comment and send_comments) or (photo_file and send_pictures) or (audio_file and send_audio)
        if not wants_email:
            return True

        admins = admin_email_rows(conn)
        if not admins:
            return True

        attachments = []
        if photo_file and send_pictures:
            attachment = storage_attachment(photo_file)
            if attachment:
                attachments.append(attachment)
        if audio_file and send_audio:
            attachment = storage_attachment(audio_file)
            if attachment:
                attachments.append(attachment)

        lines = [
            "A field update was added in ProjectONus.",
            "",
            f"Project: {project.get('name') if project else '-'}",
            f"Room: {room.get('name') if room else '-'}",
            f"User: {actor_name or 'Unknown user'}",
            f"Email: {actor_email or '-'}",
            f"Date: {note_date}",
            ""
        ]
        if comment and send_comments:
            lines.extend(["Comment:", comment, ""])
        if photo_file and send_pictures:
            lines.append("Picture attached.")
        if audio_file and send_audio:
            lines.append("Audio attached.")
        body = "\n".join(lines)
        subject = f"ProjectONus field update - {room.get('name') if room else 'Room'}"
        email_ok = True
        for admin in admins:
            if admin.get("email"):
                email_ok = send_email(admin["email"], subject, body, attachments=attachments) and email_ok
        return email_ok
    except Exception as e:
        print("Field note admin notification failed:", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def notify_admins_of_attendance(conn, project, event_type, latitude, longitude, address, created_at, event_timezone):
    actor = conn.execute(
        "SELECT name, email, role FROM users WHERE id = %s",
        (session.get("user_id"),)
    ).fetchone() or {}
    actor_name = actor.get("name") or session.get("name")
    actor_email = actor.get("email") or ""
    actor_role = actor.get("role") or session.get("role")
    notification_type = "attendance_check_in" if event_type == "check_in" else "attendance_check_out"
    add_notification(conn, session.get("user_id"), actor_name, actor_email, actor_role, notification_type)
    conn.commit()

    label = "Clock In" if event_type == "check_in" else "Clock Out"
    maps_url = f"https://www.google.com/maps?q={latitude},{longitude}"
    body = "\n".join([
        f"{label} recorded in ProjectONus.",
        "",
        f"Project: {project.get('name') if project else '-'}",
        f"User: {actor_name or 'Unknown user'}",
        f"Email: {actor_email or '-'}",
        f"Time: {format_time(created_at, event_timezone)}",
        f"Date: {format_date(created_at, event_timezone)}",
        f"Time Zone: {event_timezone}",
        f"Location: {address or '-'}",
        f"GPS: {latitude}, {longitude}",
        f"Map: {maps_url}",
    ])
    for admin in admin_email_rows(conn):
        if admin.get("email"):
            send_email(admin["email"], f"ProjectONus {label} - {actor_name or 'User'}", body)


def task_email_body(task, assigned=None, project=None):
    address = task_project_address(task, project)
    lines = [
        "A task was assigned in ProjectONus.",
        "",
        f"Task #: {task.get('task_number') or '-'}",
        f"Task: {task_display_name(task)}",
        f"Project: {(project or task).get('project_name') or (project or task).get('name') or '-'}",
        f"Assigned to: {(assigned or task).get('name') or task.get('assigned_user_name') or '-'}",
        f"Be There: {task_schedule_text(task)}",
        "",
    ]
    if address:
        lines.extend([
            f"Address: {address}",
            f"Google Maps Route: {maps_directions_url(address)}",
            "",
        ])
    if task.get("instructions"):
        lines.extend(["Instructions:", task.get("instructions"), ""])
    lines.extend([
        f"Requires picture: {'Yes' if task.get('require_picture') else 'No'}",
        f"Allows picture upload: {'Yes' if task.get('allow_picture_upload') else 'No'}",
        f"Allows comment: {'Yes' if task.get('allow_comment') else 'No'}",
        f"Allows voice/audio: {'Yes' if task.get('allow_audio') else 'No'}",
        "",
        "You now have access to this project until the admin revokes it on the Project Access page.",
        "Open your ProjectONus app and press Received after you review the task.",
        external_url("my_tasks")
    ])
    return "\n".join(lines)


def send_task_assignment_email(task, assigned, project):
    attachments = []
    seen_paths = set()
    for task_attachment in task.get("_attachments", []) or []:
        path = task_attachment.get("storage_path")
        if path and path not in seen_paths:
            seen_paths.add(path)
            attachment = storage_attachment(path, task_attachment.get("original_filename"))
            if attachment:
                attachments.append(attachment)
    for path in [task.get("task_photo_file"), task.get("task_audio_file")]:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        attachment = storage_attachment(path)
        if attachment:
            attachments.append(attachment)
    if assigned.get("email"):
        send_email(
            assigned["email"],
            f"ProjectONus task assigned - {task_display_name(task)}",
            task_email_body(task, assigned, project),
            attachments=attachments
        )


def send_task_assignment_sms(task, assigned, project):
    if not assigned.get("sms_enabled") or not assigned.get("phone_number"):
        return False
    project_name = project.get("name") if project else task.get("project_name")
    address = task_project_address(task, project)
    route = maps_directions_url(address)
    route_text = f" Route: {route}" if route else ""
    return send_sms(
        assigned["phone_number"],
        f"ProjectONus task assigned: {task_display_name(task)} for {project_name or 'your project'} at {task_schedule_text(task)}.{route_text} Open the app and press Received: {external_url('my_tasks')}"
    )


def notify_admins_task_received(conn, task, actor):
    add_notification(
        conn,
        actor.get("id"),
        actor.get("name"),
        actor.get("email"),
        actor.get("role"),
        "task_received",
        task.get("project_id"),
        task.get("id"),
        f"{actor.get('name') or 'Worker'} confirmed task received: {task_display_name(task)}"
    )
    conn.commit()
    body = "\n".join([
        "A worker marked a task as received in ProjectONus.",
        "",
        f"Worker: {actor.get('name') or 'Unknown user'}",
        f"Email: {actor.get('email') or '-'}",
        f"Task #: {task.get('task_number') or '-'}",
        f"Task: {task_display_name(task)}",
        f"Project: {task.get('project_name') or '-'}",
        f"Received: {format_datetime(task.get('accepted_at') or utc_now_iso())}",
        "",
        external_url("my_tasks")
    ])
    for admin in admin_email_rows(conn):
        if admin.get("email"):
            send_email(admin["email"], f"ProjectONus task received - {task_display_name(task)}", body)


def can_add_notes():
    return has_perm("write_comments") or has_perm("add_pictures") or has_perm("add_audio")


def can_view_inventory():
    return is_main_admin() or has_perm("view_inventory") or has_perm("edit_inventory")


def can_edit_inventory():
    return is_main_admin() or has_perm("edit_inventory")


INVENTORY_STATUS_LABELS = {
    "available": "Available",
    "used": "Used",
    "needs_purchase": "Needs purchase"
}

INVENTORY_LOCATION_LABELS = {
    "storage": "Storage",
    "warehouse": "Warehouse",
    "job_site": "Job site"
}

INVENTORY_CONDITION_LABELS = {
    "new": "New",
    "used": "Used"
}

DTOOLS_CLOUD_DEFAULT_BASE_URL = "https://dtcloudapi.d-tools.cloud/api/v1"
DTOOLS_CLOUD_DEFAULT_AUTH = "Basic RFRDbG91ZEFQSVVzZXI6MyNRdVkrMkR1QCV3Kk15JTU8Yi1aZzlV"


def clean_inventory_status(value):
    value = (value or "available").strip()
    return value if value in INVENTORY_STATUS_LABELS else "available"


def clean_inventory_location(value):
    value = (value or "warehouse").strip()
    return value if value in INVENTORY_LOCATION_LABELS else "warehouse"


def clean_inventory_condition(value):
    value = (value or "new").strip()
    return value if value in INVENTORY_CONDITION_LABELS else "new"


def inventory_status_label(value):
    return INVENTORY_STATUS_LABELS.get(value or "", "Available")


def inventory_location_label(value):
    return INVENTORY_LOCATION_LABELS.get(value or "", "Warehouse")


def inventory_condition_label(value):
    return INVENTORY_CONDITION_LABELS.get(value or "", "New")


def fetch_suppliers(conn):
    return conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()


def supplier_from_task_form(conn):
    if request.form.get("supplier_enabled") != "1":
        return None, ""
    supplier_id = optional_int(request.form.get("supplier_id"))
    new_name = request.form.get("new_supplier_name", "").strip()
    if supplier_id:
        supplier = conn.execute("SELECT * FROM suppliers WHERE id = %s", (supplier_id,)).fetchone()
        return (supplier, "") if supplier else (None, "Choose a valid supplier.")
    if not new_name:
        return None, "Choose an existing supplier or enter a new supplier name."
    street, address, city, state, zip_code = supplier_address_from_form("new_supplier_")
    supplier = conn.execute(
        """
        INSERT INTO suppliers
        (name, contact_name, email, phone, street, address, city, state, zip, website, notes, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            new_name,
            request.form.get("new_supplier_contact_name", "").strip(),
            request.form.get("new_supplier_email", "").strip(),
            request.form.get("new_supplier_phone", "").strip(),
            street,
            address,
            city,
            state,
            zip_code,
            request.form.get("new_supplier_website", "").strip(),
            request.form.get("new_supplier_notes", "").strip(),
            utc_now_iso(),
            utc_now_iso()
        )
    ).fetchone()
    return supplier, ""


def create_supplier_inventory_item(conn, supplier, project_id, room_id):
    if not supplier:
        return None, ""
    item_name = request.form.get("supplier_item_name", "").strip()
    if not item_name:
        return None, "Enter the supplier material/item name."
    try:
        quantity = float(request.form.get("supplier_quantity") or 0)
    except Exception:
        return None, "Enter a valid supplier quantity."
    if quantity <= 0:
        return None, "Enter a supplier quantity greater than zero."
    note_parts = []
    pickup_date = request.form.get("supplier_item_date") or local_now().date().isoformat()
    pickup_time = request.form.get("supplier_pickup_time", "").strip()
    if pickup_date:
        note_parts.append(f"Pickup date: {pickup_date}")
    if pickup_time:
        note_parts.append(f"Pickup time: {pickup_time}")
    purchase_note = request.form.get("supplier_purchase_note", "").strip()
    if purchase_note:
        note_parts.append(purchase_note)
    return conn.execute(
        """
        INSERT INTO inventory_items
        (item_date, quantity, item_name, item_model, brand, item_condition, location_type, location_detail, project_id, room_id, supplier_pickup_time, status, added_by, supplier_id, used_note, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, 'new', 'job_site', %s, %s, %s, %s, 'needs_purchase', %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            pickup_date,
            quantity,
            item_name,
            request.form.get("supplier_model", "").strip(),
            request.form.get("supplier_brand", "").strip(),
            "Needs purchase from supplier",
            project_id,
            room_id,
            pickup_time,
            session.get("user_id"),
            supplier["id"],
            "\n".join(note_parts),
            utc_now_iso(),
            utc_now_iso()
        )
    ).fetchone(), ""


def supplier_items_from_task_form(conn, supplier):
    if not supplier:
        return [], ""
    raw = request.form.get("supplier_items_json", "").strip()
    if not raw:
        item, error = create_supplier_inventory_item(conn, supplier, request.form.get("project_id", type=int), request.form.get("room_id", type=int))
        return ([item] if item else []), error
    try:
        rows = json.loads(raw)
    except Exception:
        return [], "Supplier material list could not be read. Add the items again."
    if not isinstance(rows, list) or not rows:
        return [], "Add at least one supplier material item."
    created = []
    for row in rows:
        project_id = optional_int(row.get("project_id"))
        room_id = optional_int(row.get("room_id"))
        project_id, room_id, error = validate_inventory_allocation(conn, project_id, room_id)
        if error:
            return [], error
        item_name = (row.get("item_name") or "").strip()
        if not item_name:
            return [], "Every supplier material needs an item name."
        try:
            quantity = float(row.get("quantity") or 0)
        except Exception:
            return [], "Every supplier material needs a valid quantity."
        if quantity <= 0:
            return [], "Every supplier material needs a quantity greater than zero."
        pickup_date = (row.get("pickup_date") or local_now().date().isoformat()).strip()
        pickup_time = (row.get("pickup_time") or "").strip()
        note_parts = []
        if pickup_date:
            note_parts.append(f"Pickup date: {pickup_date}")
        if pickup_time:
            note_parts.append(f"Pickup time: {pickup_time}")
        purchase_note = (row.get("purchase_note") or request.form.get("supplier_purchase_note") or "").strip()
        if purchase_note:
            note_parts.append(purchase_note)
        created.append(conn.execute(
            """
            INSERT INTO inventory_items
            (item_date, quantity, item_name, item_model, brand, item_condition, location_type, location_detail, project_id, room_id, supplier_pickup_time, status, added_by, supplier_id, used_note, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'new', 'job_site', %s, %s, %s, %s, 'needs_purchase', %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                pickup_date,
                quantity,
                item_name,
                (row.get("model") or "").strip(),
                (row.get("brand") or "").strip(),
                "Needs purchase from supplier",
                project_id,
                room_id,
                pickup_time,
                session.get("user_id"),
                supplier["id"],
                "\n".join(note_parts),
                utc_now_iso(),
                utc_now_iso()
            )
        ).fetchone())
    return created, ""


def link_supplier_items_to_task(conn, task_id, inventory_items):
    for item in inventory_items or []:
        conn.execute(
            """
            INSERT INTO task_supplier_items (task_id, inventory_item_id, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (task_id, item["id"], utc_now_iso())
        )


def supplier_task_instructions(base_instructions, supplier, inventory_item):
    inventory_items = inventory_item if isinstance(inventory_item, list) else ([inventory_item] if inventory_item else [])
    if not supplier or not inventory_items:
        return base_instructions
    lines = [
        base_instructions.strip(),
        "",
        "Supplier:",
        f"Name: {supplier.get('name') or '-'}",
        f"Contact: {supplier.get('contact_name') or '-'}",
        f"Phone: {supplier.get('phone') or '-'}",
        f"Email: {supplier.get('email') or '-'}",
        f"Address: {supplier.get('address') or '-'}",
        "",
        "Materials:"
    ]
    for idx, item in enumerate(inventory_items, 1):
        lines.extend([
            f"{idx}. {item.get('item_name') or '-'}",
            f"Quantity: {item.get('quantity') or '-'}",
            f"Brand: {item.get('brand') or '-'}",
            f"Model #: {item.get('item_model') or '-'}",
            f"Pickup Date: {item.get('item_date') or '-'}",
            f"Pickup Time: {item.get('supplier_pickup_time') or '-'}",
            f"Pickup / Purchase Note: {item.get('used_note') or '-'}",
            "Inventory status: Needs purchase"
        ])
    return "\n".join(line for line in lines if line is not None).strip()


def task_instruction_text(task):
    instructions = ((task or {}).get("instructions") or "").strip()
    if not instructions:
        return ""
    if instructions.startswith("Supplier:"):
        notes = []
        for match in re.findall(r"Pickup / Purchase Note:\s*(.*?)(?=\s+Inventory status:|\s+\d+\.\s|\Z)", instructions, flags=re.S):
            cleaned = re.sub(r"Pickup date:\s*\S+\s*", "", match).strip()
            cleaned = re.sub(r"Pickup time:\s*\S+\s*", "", cleaned).strip()
            if cleaned and cleaned not in notes:
                notes.append(cleaned)
        return "\n".join(notes).strip()
    for marker in ["\nSupplier:", "\r\nSupplier:", "\n\nSupplier:", "\r\n\r\nSupplier:"]:
        if marker in instructions:
            return instructions.split(marker, 1)[0].strip()
    return instructions


def dtools_cloud_config():
    return {
        "api_key": get_app_setting("dtools_cloud_api_key", os.environ.get("DTOOLS_CLOUD_API_KEY", "")).strip(),
        "base_url": get_app_setting("dtools_cloud_base_url", DTOOLS_CLOUD_DEFAULT_BASE_URL).strip() or DTOOLS_CLOUD_DEFAULT_BASE_URL,
        "auth_header": get_app_setting("dtools_cloud_auth_header", DTOOLS_CLOUD_DEFAULT_AUTH).strip() or DTOOLS_CLOUD_DEFAULT_AUTH,
        "material_path": get_app_setting("dtools_cloud_material_path", "Projects/GetProject").strip() or "Projects/GetProject",
        "id_param": get_app_setting("dtools_cloud_id_param", "Id").strip() or "Id",
    }


def dtools_cloud_configured():
    return bool(dtools_cloud_config().get("api_key"))


def optional_int(value):
    try:
        return int(value) if str(value or "").strip() else None
    except Exception:
        return None


def is_mobile_request():
    user_agent = request.headers.get("User-Agent", "").lower()
    return any(token in user_agent for token in ["mobi", "android", "iphone", "ipad"])


def user_can_access_project(conn, project_id, user_id=None):
    if is_main_admin():
        return True
    uid = user_id or session.get("user_id")
    if not uid or not project_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM project_permissions WHERE user_id = %s AND project_id = %s",
        (uid, project_id)
    ).fetchone()
    return bool(row)


def grant_project_access(conn, user_id, project_id, role=None):
    if not user_id or not project_id or role == "admin":
        return
    conn.execute(
        """
        INSERT INTO project_permissions (user_id, project_id, created_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, project_id) DO NOTHING
        """,
        (user_id, project_id, utc_now_iso())
    )


def fetch_visible_projects(conn, q=""):
    params = []
    join_sql = ""
    if not is_main_admin():
        join_sql = "JOIN project_permissions ON project_permissions.project_id = projects.id AND project_permissions.user_id = %s"
        params.append(session.get("user_id"))

    where_sql = ""
    if q:
        like = f"%{q}%"
        where_sql = """
        WHERE projects.name ILIKE %s
           OR projects.customer_name ILIKE %s
           OR projects.customer_address ILIKE %s
           OR projects.customer_street ILIKE %s
           OR projects.customer_city ILIKE %s
           OR projects.customer_state ILIKE %s
           OR projects.customer_zip ILIKE %s
           OR projects.billing_address ILIKE %s
           OR projects.billing_street ILIKE %s
           OR projects.billing_city ILIKE %s
           OR projects.billing_state ILIKE %s
           OR projects.billing_zip ILIKE %s
        """
        params.extend([like, like, like, like, like, like, like, like, like, like, like, like])

    return conn.execute(
        f"SELECT projects.* FROM projects {join_sql} {where_sql} ORDER BY projects.created_at DESC",
        tuple(params)
    ).fetchall()


def fetch_inventory_projects(conn):
    if is_main_admin():
        return conn.execute("SELECT id, name, customer_name FROM projects ORDER BY name").fetchall()
    return conn.execute(
        """
        SELECT projects.id, projects.name, projects.customer_name
        FROM projects
        JOIN project_permissions ON project_permissions.project_id = projects.id AND project_permissions.user_id = %s
        ORDER BY projects.name
        """,
        (session.get("user_id"),)
    ).fetchall()


def fetch_inventory_rooms(conn, project_id=None):
    params = []
    join_sql = "JOIN projects ON rooms.project_id = projects.id"
    where = []
    if not is_main_admin():
        join_sql += " JOIN project_permissions ON project_permissions.project_id = rooms.project_id AND project_permissions.user_id = %s"
        params.append(session.get("user_id"))
    if project_id:
        where.append("rooms.project_id = %s")
        params.append(project_id)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return conn.execute(
        f"""
        SELECT rooms.id, rooms.name, rooms.project_id, projects.name AS project_name
        FROM rooms
        {join_sql}
        {where_sql}
        ORDER BY projects.name, rooms.name
        """,
        tuple(params)
    ).fetchall()


def inventory_select_query(where_sql):
    return f"""
        SELECT inventory_items.*,
               projects.name AS project_name,
               rooms.name AS room_name,
               suppliers.name AS supplier_name,
               suppliers.address AS supplier_address,
               suppliers.phone AS supplier_phone,
               added_users.name AS added_by_name,
               purchased_users.name AS purchased_by_name,
               used_users.name AS used_by_name
        FROM inventory_items
        LEFT JOIN projects ON inventory_items.project_id = projects.id
        LEFT JOIN rooms ON inventory_items.room_id = rooms.id
        LEFT JOIN suppliers ON inventory_items.supplier_id = suppliers.id
        LEFT JOIN users AS added_users ON inventory_items.added_by = added_users.id
        LEFT JOIN users AS purchased_users ON inventory_items.purchased_by = purchased_users.id
        LEFT JOIN users AS used_users ON inventory_items.used_by = used_users.id
        {where_sql}
        ORDER BY CASE inventory_items.status
                    WHEN 'available' THEN 0
                    WHEN 'needs_purchase' THEN 1
                    WHEN 'used' THEN 2
                    ELSE 3
                 END,
                 inventory_items.item_date DESC,
                 inventory_items.created_at DESC,
                 inventory_items.id DESC
    """


def fetch_inventory_items(conn, filters=None):
    filters = filters or {}
    where = ["1=1"]
    params = []
    if not is_main_admin():
        where.append(
            """
            (
                inventory_items.project_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM project_permissions
                    WHERE project_permissions.project_id = inventory_items.project_id
                      AND project_permissions.user_id = %s
                )
            )
            """
        )
        params.append(session.get("user_id"))
    q = (filters.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where.append(
            """
            (
                inventory_items.item_name ILIKE %s
                OR inventory_items.item_model ILIKE %s
                OR inventory_items.brand ILIKE %s
                OR inventory_items.location_detail ILIKE %s
                OR projects.name ILIKE %s
                OR rooms.name ILIKE %s
                OR suppliers.name ILIKE %s
            )
            """
        )
        params.extend([like, like, like, like, like, like, like])
    status = filters.get("status")
    if status in INVENTORY_STATUS_LABELS:
        where.append("inventory_items.status = %s")
        params.append(status)
    project_id = filters.get("project_id")
    if project_id:
        where.append("inventory_items.project_id = %s")
        params.append(project_id)
    room_id = filters.get("room_id")
    if room_id:
        where.append("inventory_items.room_id = %s")
        params.append(room_id)
    where_sql = "WHERE " + " AND ".join(where)
    return conn.execute(inventory_select_query(where_sql), tuple(params)).fetchall()


def prepare_inventory_form(conn, project_id=None):
    projects = fetch_inventory_projects(conn)
    rooms = fetch_inventory_rooms(conn, project_id)
    return projects, rooms


def inventory_item_access_allowed(conn, item):
    if is_main_admin():
        return True
    if not item.get("project_id"):
        return can_view_inventory()
    return user_can_access_project(conn, item.get("project_id"))


def validate_inventory_allocation(conn, project_id, room_id):
    if room_id:
        room = conn.execute("SELECT id, project_id FROM rooms WHERE id = %s", (room_id,)).fetchone()
        if not room:
            return None, None, "Room not found."
        project_id = project_id or room["project_id"]
        if room["project_id"] != project_id:
            return None, None, "Room does not belong to the selected project."
    if project_id and not user_can_access_project(conn, project_id):
        return None, None, "You do not have access to this project."
    return project_id, room_id, ""


def insert_inventory_item(conn, fixed_project_id=None, fixed_room_id=None):
    project_id = fixed_project_id if fixed_project_id is not None else optional_int(request.form.get("project_id"))
    room_id = fixed_room_id if fixed_room_id is not None else optional_int(request.form.get("room_id"))
    project_id, room_id, error = validate_inventory_allocation(conn, project_id, room_id)
    if error:
        return error
    file = request.files.get("picture") or request.files.get("picture_camera")
    picture_file = upload_file_to_storage(file) if file and file.filename and allowed_photo(file.filename) else None
    status = clean_inventory_status(request.form.get("status"))
    used_by = session.get("user_id") if status == "used" else None
    used_at = utc_now_iso() if status == "used" else None
    item_name = (request.form.get("item_name") or request.form.get("description") or "").strip()
    if not item_name:
        return "Item name is required."
    conn.execute(
        """
        INSERT INTO inventory_items
        (item_date, quantity, item_name, item_model, brand, item_condition, location_type, location_detail, project_id, room_id, status, added_by, used_by, used_at, used_note, picture_file, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            request.form.get("item_date") or local_now().date().isoformat(),
            float(request.form.get("quantity") or 0),
            item_name,
            (request.form.get("item_model") or request.form.get("part_number") or "").strip(),
            request.form.get("brand", "").strip(),
            clean_inventory_condition(request.form.get("item_condition")),
            clean_inventory_location(request.form.get("location_type")),
            request.form.get("location_detail", "").strip(),
            project_id,
            room_id,
            status,
            session.get("user_id"),
            used_by,
            used_at,
            request.form.get("used_note", "").strip(),
            picture_file,
            utc_now_iso(),
            utc_now_iso()
        )
    )
    return ""


def dtools_cloud_fetch_payload(external_ref, endpoint_path=None):
    config = dtools_cloud_config()
    api_key = config["api_key"]
    if not api_key:
        raise RuntimeError("D-Tools Cloud API key is missing. Add it in Settings.")

    path = (endpoint_path or config["material_path"]).strip()
    if not path:
        raise RuntimeError("D-Tools Cloud material endpoint path is missing.")
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        url = config["base_url"].rstrip("/") + "/" + path.lstrip("/")

    ref = (external_ref or "").strip()
    if ref:
        if "{id}" in url:
            url = url.replace("{id}", urllib.parse.quote(ref))
        else:
            separator = "&" if "?" in url else "?"
            url += separator + urllib.parse.urlencode({config["id_param"]: ref})

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-API-Key": api_key,
            "Authorization": config["auth_header"],
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"D-Tools Cloud returned {e.code}: {details or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach D-Tools Cloud: {e.reason}")
    except json.JSONDecodeError:
        raise RuntimeError("D-Tools Cloud returned a response that was not JSON.")


def normalize_lookup_key(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def dtools_scalar(value):
    return isinstance(value, (str, int, float, bool)) and str(value).strip() != ""


def dtools_pick(data, names):
    wanted = {normalize_lookup_key(name) for name in names}

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if normalize_lookup_key(key) in wanted and dtools_scalar(value):
                    return str(value).strip()
            for value in obj.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = walk(value)
                if found:
                    return found
        return ""

    return walk(data)


def dtools_quantity(value):
    text = str(value or "").replace(",", "").strip()
    try:
        qty = float(text)
        return qty if qty > 0 else 1
    except Exception:
        return 1


DTOOLS_ITEM_LIST_KEYS = {
    "items", "lineitems", "quoteitems", "projectitems", "products", "materials",
    "equipment", "productitems", "designitems", "bom", "billofmaterials"
}


def dtools_item_like(item):
    if not isinstance(item, dict):
        return False
    name = dtools_pick(item, ["itemName", "productName", "name", "description", "model", "partNumber"])
    indicator = dtools_pick(item, ["quantity", "qty", "totalQuantity", "model", "partNumber", "manufacturer", "brand", "locationName", "roomName"])
    return bool(name and indicator)


def dtools_collect_item_candidates(payload):
    candidates = []
    seen = set()

    def add_item(item):
        marker = id(item)
        if marker not in seen and dtools_item_like(item):
            seen.add(marker)
            candidates.append(item)

    def walk(obj, parent_key=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_norm = normalize_lookup_key(key)
                if isinstance(value, list) and key_norm in DTOOLS_ITEM_LIST_KEYS:
                    for child in value:
                        if isinstance(child, dict):
                            add_item(child)
                walk(value, key_norm)
        elif isinstance(obj, list):
            if parent_key in DTOOLS_ITEM_LIST_KEYS or sum(1 for child in obj[:12] if dtools_item_like(child)) >= 2:
                for child in obj:
                    if isinstance(child, dict):
                        add_item(child)
            for child in obj:
                walk(child, parent_key)

    walk(payload)
    return candidates


def dtools_normalize_material(item, index, external_ref):
    item_type = dtools_pick(item, ["itemType", "type", "category", "categoryName", "lineType"])
    type_text = item_type.lower()
    name = dtools_pick(item, ["itemName", "productName", "product", "name", "description", "shortDescription", "model", "partNumber"])
    if not name:
        return None
    if any(token in type_text for token in ["labor", "labour", "service", "subscription", "allowance"]):
        return None
    if any(token in name.lower() for token in ["labor", "labour"]) and not dtools_pick(item, ["model", "partNumber", "sku"]):
        return None

    quantity = dtools_quantity(dtools_pick(item, ["totalQuantity", "quantity", "qty", "count"]))
    brand = dtools_pick(item, ["manufacturer", "manufacturerName", "brand", "brandName", "vendor", "vendorName"])
    model = dtools_pick(item, ["model", "modelNumber", "partNumber", "manufacturerPartNumber", "sku"])
    location = dtools_pick(item, ["location", "locationName", "room", "roomName", "sublocation", "subLocation", "area", "areaName"])
    system = dtools_pick(item, ["system", "systemName"])
    phase = dtools_pick(item, ["phase", "phaseName"])
    category = dtools_pick(item, ["category", "categoryName"])
    source_item_id = dtools_pick(item, ["id", "itemId", "lineItemId", "quoteItemId", "projectItemId", "productId", "uuid"])
    if not source_item_id:
        stable = json.dumps(item, sort_keys=True, default=str)[:1200]
        source_item_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{external_ref}:{index}:{stable}").hex

    return {
        "source_item_id": source_item_id,
        "item_name": name,
        "quantity": quantity,
        "brand": brand,
        "model": model,
        "location": location,
        "system": system,
        "phase": phase,
        "category": category,
    }


def dtools_extract_materials(payload, external_ref):
    materials = []
    for index, item in enumerate(dtools_collect_item_candidates(payload), start=1):
        material = dtools_normalize_material(item, index, external_ref)
        if material:
            materials.append(material)
    return materials


def match_dtools_room(room_lookup, location):
    location_key = normalize_lookup_key(location)
    if not location_key:
        return None
    if location_key in room_lookup:
        return room_lookup[location_key]
    for room_key, room_id in room_lookup.items():
        if room_key and (room_key in location_key or location_key in room_key):
            return room_id
    return None


def import_dtools_materials(conn, project_id, external_ref, payload):
    rooms = conn.execute("SELECT id, name FROM rooms WHERE project_id = %s", (project_id,)).fetchall()
    room_lookup = {normalize_lookup_key(room["name"]): room["id"] for room in rooms}
    materials = dtools_extract_materials(payload, external_ref)
    imported = 0
    skipped = 0
    unmatched_rooms = 0
    now = utc_now_iso()

    for material in materials:
        exists = conn.execute(
            """
            SELECT id FROM inventory_items
            WHERE project_id = %s
              AND dtools_cloud_project_ref = %s
              AND dtools_cloud_item_id = %s
            """,
            (project_id, external_ref, material["source_item_id"])
        ).fetchone()
        if exists:
            skipped += 1
            continue

        room_id = match_dtools_room(room_lookup, material.get("location"))
        if material.get("location") and not room_id:
            unmatched_rooms += 1
        detail_parts = []
        for label, key in [("Location", "location"), ("System", "system"), ("Phase", "phase"), ("Category", "category")]:
            if material.get(key):
                detail_parts.append(f"{label}: {material[key]}")
        location_detail = "; ".join(detail_parts) or "Imported from D-Tools Cloud"
        used_note = f"Imported from D-Tools Cloud source {external_ref}. Marked needs purchase."

        conn.execute(
            """
            INSERT INTO inventory_items
            (item_date, quantity, item_name, item_model, brand, item_condition, location_type, location_detail, project_id, room_id, status, added_by, used_note, dtools_cloud_source_id, dtools_cloud_item_id, dtools_cloud_project_ref, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                local_now().date().isoformat(),
                material["quantity"],
                material["item_name"],
                material["model"],
                material["brand"],
                "new",
                "job_site",
                location_detail[:500],
                project_id,
                room_id,
                "needs_purchase",
                session.get("user_id"),
                used_note,
                "dtools_cloud",
                material["source_item_id"],
                external_ref,
                now,
                now
            )
        )
        imported += 1

    conn.execute(
        "UPDATE projects SET dtools_cloud_project_ref = %s WHERE id = %s",
        (external_ref, project_id)
    )
    return {"found": len(materials), "imported": imported, "skipped": skipped, "unmatched_rooms": unmatched_rooms}


def zoneinfo_or_none(name):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def app_timezone():
    return zoneinfo_or_none(APP_TIMEZONE) or timezone(timedelta(hours=-4), "America/New_York")


def clean_timezone_name(name):
    name = (name or "").strip()
    if name and (zoneinfo_or_none(name) or "/" in name or name == "UTC"):
        return name
    return APP_TIMEZONE


def timezone_for_name(name):
    return zoneinfo_or_none(clean_timezone_name(name)) or app_timezone()


def timezone_from_location(latitude, longitude, fallback=None):
    fallback = clean_timezone_name(fallback or APP_TIMEZONE)
    if TIMEZONE_FINDER is None:
        return fallback
    try:
        lat = float(latitude)
        lon = float(longitude)
        found = TIMEZONE_FINDER.timezone_at(lat=lat, lng=lon)
        if not found:
            found = TIMEZONE_FINDER.closest_timezone_at(lat=lat, lng=lon)
        return clean_timezone_name(found or fallback)
    except Exception:
        return fallback


def utc_now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def utc_future_iso(minutes=10):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(tzinfo=None).isoformat()


def local_now():
    return datetime.now(timezone.utc).astimezone(app_timezone())


def parse_iso_datetime(value):
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text or ("T" not in text and " " not in text):
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_datetime(value, timezone_name=None):
    dt = parse_iso_datetime(value)
    if not dt:
        return None
    return dt.astimezone(timezone_for_name(timezone_name) if timezone_name else app_timezone())


def storage_datetime(value, timezone_name=None):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone_for_name(timezone_name) if timezone_name else app_timezone())
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def local_date_text(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%m/%d/%Y")
    except Exception:
        return None


def format_time(value, timezone_name=None):
    dt = local_datetime(value, timezone_name)
    if not dt:
        return value or "-"
    return dt.strftime("%I:%M%p").lstrip("0")


def format_task_time(value):
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ["%H:%M", "%H:%M:%S"]:
        try:
            return datetime.strptime(text, fmt).strftime("%I:%M%p").lstrip("0")
        except Exception:
            pass
    return text


def format_date(value, timezone_name=None):
    date_text = local_date_text(value)
    if date_text:
        return date_text
    dt = local_datetime(value, timezone_name)
    if dt:
        return dt.strftime("%m/%d/%Y")
    return value or "-"


def format_datetime(value, timezone_name=None):
    dt = local_datetime(value, timezone_name)
    if not dt:
        return value or "-"
    return f"{dt.strftime('%m/%d/%Y')} {dt.strftime('%I:%M%p').lstrip('0')}"


def event_timezone_name(event):
    if not event:
        return APP_TIMEZONE
    saved = (event.get("event_timezone") or "").strip()
    if saved:
        return clean_timezone_name(saved)
    return timezone_from_location(event.get("latitude"), event.get("longitude"), APP_TIMEZONE)


def format_event_time(event):
    return format_time(event.get("created_at") if event else None, event_timezone_name(event))


def format_event_date(event):
    return format_date(event.get("created_at") if event else None, event_timezone_name(event))


def format_event_datetime(event):
    return format_datetime(event.get("created_at") if event else None, event_timezone_name(event))


def task_schedule_text(task):
    start_raw = task.get("task_start_date") or task.get("task_date")
    text = format_date(start_raw)
    start_time = format_task_time(task.get("task_start_time"))
    if start_time:
        text += f" at {start_time}"
    end_date = task.get("task_end_date")
    if end_date and end_date != start_raw:
        text += f" to {format_date(end_date)}"
    return text


def task_calendar_start(task):
    start_date = (task.get("task_start_date") or task.get("task_date") or "").strip()
    start_time = (task.get("task_start_time") or "09:00").strip()
    try:
        return datetime.strptime(f"{start_date} {start_time[:5]}", "%Y-%m-%d %H:%M")
    except Exception:
        return None


def ics_escape(value):
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def ics_fold(line):
    if len(line) <= 73:
        return line
    parts = []
    while len(line) > 73:
        parts.append(line[:73])
        line = " " + line[73:]
    parts.append(line)
    return "\r\n".join(parts)


def task_calendar_ics(task):
    start_dt = task_calendar_start(task)
    if not start_dt:
        start_dt = local_now().replace(second=0, microsecond=0)
    tz_name = clean_timezone_name(APP_TIMEZONE)
    uid = f"projectonus-task-{task.get('id')}@projectonus.com"
    address = task_project_address(task)
    description_lines = [
        task.get("instructions") or "",
        "",
        f"Project: {task.get('project_name') or '-'}",
        f"Room: {task.get('room_name') or '-'}",
        f"Task #: {task.get('task_number') or '-'}",
        f"Task: {task_display_name(task)}",
    ]
    if address:
        description_lines.extend(["", f"Address: {address}", f"Route: {maps_directions_url(address)}"])
    description_lines.extend(["", external_url("my_tasks")])
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ProjectONus//Task Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID={tz_name}:{start_dt.strftime('%Y%m%dT%H%M%S')}",
        "DURATION:PT1H",
        f"SUMMARY:{ics_escape('ProjectONus Task - ' + task_display_name(task))}",
        f"DESCRIPTION:{ics_escape(chr(10).join(description_lines))}",
        f"LOCATION:{ics_escape(address)}",
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{ics_escape(task_display_name(task))}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


def maps_directions_url(address):
    address = (address or "").strip()
    if not address:
        return ""
    return "https://www.google.com/maps/dir/?api=1&destination=" + urllib.parse.quote_plus(address)


def task_project_address(task, project=None):
    source = project or task or {}
    return (source.get("customer_address") or source.get("project_address") or "").strip()


def duration_text(start_value, end_value):
    start = parse_iso_datetime(start_value)
    end = parse_iso_datetime(end_value)
    if not start or not end or end < start:
        return "-"
    total_minutes = int((end - start).total_seconds() // 60)
    return minutes_text(total_minutes)


def minutes_text(total_minutes):
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}h {minutes}m"


def duration_minutes(start_value, end_value):
    start = parse_iso_datetime(start_value)
    end = parse_iso_datetime(end_value)
    if not start or not end or end < start:
        return 0
    return int((end - start).total_seconds() // 60)


def attendance_range(period, selected_date, tzinfo=None):
    tzinfo = tzinfo or app_timezone()
    try:
        base = datetime.strptime(selected_date, "%Y-%m-%d").replace(tzinfo=tzinfo)
    except Exception:
        base = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = base - timedelta(days=base.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = base.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    elif period == "year":
        start = base.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1)
    else:
        start = base.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        period = "day"
    return period, start, end


def attendance_event_in_range(event, period, selected_date):
    tzinfo = timezone_for_name(event_timezone_name(event))
    period, start, end = attendance_range(period, selected_date, tzinfo)
    event_dt = local_datetime(event.get("created_at"), event_timezone_name(event))
    return bool(event_dt and start <= event_dt < end)


def task_scheduled_in_range(task, period, selected_date):
    period, start, end = attendance_range(period, selected_date)
    task_date = local_date_text(task.get("task_start_date") or task.get("task_date"))
    if not task_date:
        return False
    try:
        scheduled = datetime.strptime(task_date, "%m/%d/%Y").replace(tzinfo=start.tzinfo)
    except Exception:
        return False
    return start <= scheduled < end


def current_clock_in_event(conn, user_id=None):
    uid = user_id or session.get("user_id")
    if not uid:
        return None
    event = conn.execute(
        """
        SELECT attendance_events.*, projects.name AS project_name
        FROM attendance_events
        LEFT JOIN projects ON attendance_events.project_id = projects.id
        WHERE attendance_events.user_id = %s
        ORDER BY attendance_events.created_at DESC
        LIMIT 1
        """,
        (uid,)
    ).fetchone()
    if event and event.get("event_type") == "check_in":
        return event
    return None


def ensure_worker_location_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS worker_location_pings (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        attendance_event_id INTEGER REFERENCES attendance_events(id) ON DELETE SET NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        accuracy REAL,
        address TEXT,
        event_timezone TEXT,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()


def active_worker_locations(conn):
    ensure_worker_location_tables(conn)
    latest_events = conn.execute(
        """
        SELECT DISTINCT ON (attendance_events.user_id)
            attendance_events.*,
            users.name AS user_name,
            users.email AS user_email,
            users.role AS user_role,
            projects.name AS project_name
        FROM attendance_events
        JOIN users ON attendance_events.user_id = users.id
        LEFT JOIN projects ON attendance_events.project_id = projects.id
        WHERE users.role <> 'admin'
        ORDER BY attendance_events.user_id, attendance_events.created_at DESC
        """
    ).fetchall()

    workers = []
    for event in latest_events:
        if event.get("event_type") != "check_in":
            continue
        ping = conn.execute(
            """
            SELECT * FROM worker_location_pings
            WHERE user_id = %s AND created_at >= %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (event["user_id"], event["created_at"])
        ).fetchone()
        location = ping or event
        if location.get("latitude") is None or location.get("longitude") is None:
            continue
        workers.append({
            "user_id": event.get("user_id"),
            "name": event.get("user_name") or "Unknown user",
            "email": event.get("user_email") or "",
            "role": event.get("user_role") or "",
            "project_id": event.get("project_id"),
            "project_name": event.get("project_name") or "No project",
            "clock_in_time": format_event_datetime(event),
            "last_seen": format_datetime(location.get("created_at"), event_timezone_name(location)),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "accuracy": location.get("accuracy"),
            "address": location.get("address") or event.get("address") or "",
            "timezone": event_timezone_name(location),
            "source": "Live update" if ping else "Clock in"
        })
    return workers


def build_attendance_pairs(events):
    pairs = []
    open_checkins = {}
    for e in events:
        uid = e.get("user_id") or f"missing-{e.get('id')}"
        project_key = e.get("project_id") or "no-project"
        pair_key = f"{uid}:{project_key}"
        if e.get("event_type") == "check_in":
            if pair_key in open_checkins:
                pairs.append({"user": open_checkins[pair_key], "check_in": open_checkins[pair_key], "check_out": None})
            open_checkins[pair_key] = e
        elif e.get("event_type") == "check_out":
            check_in = open_checkins.pop(pair_key, None)
            pairs.append({"user": e, "check_in": check_in, "check_out": e})
    for check_in in open_checkins.values():
        pairs.append({"user": check_in, "check_in": check_in, "check_out": None})
    return pairs


def attendance_pair_sort_key(pair):
    event = pair.get("check_in") or pair.get("check_out") or {}
    dt = parse_iso_datetime(event.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt,
        (event.get("user_name") or "").lower(),
        (event.get("project_name") or "").lower(),
        event.get("id") or 0
    )


@app.context_processor
def utility_processor():
    return dict(
        file_url=file_url,
        is_main_admin=is_main_admin,
        can_add_notes=can_add_notes,
        has_perm=has_perm,
        get_app_setting=get_app_setting,
        format_time=format_time,
        format_task_time=format_task_time,
        format_date=format_date,
        format_datetime=format_datetime,
        task_schedule_text=task_schedule_text,
        task_display_name=task_display_name,
        task_instruction_text=task_instruction_text,
        task_room_attachments=task_room_attachments,
        maps_directions_url=maps_directions_url,
        is_mobile_request=is_mobile_request,
        task_project_address=task_project_address,
        format_event_time=format_event_time,
        format_event_date=format_event_date,
        format_event_datetime=format_event_datetime,
        event_timezone_name=event_timezone_name,
        admin_unread_count=admin_unread_count,
        unread_notification_count=unread_notification_count,
        can_view_inventory=can_view_inventory,
        can_edit_inventory=can_edit_inventory,
        dtools_cloud_config=dtools_cloud_config,
        dtools_cloud_configured=dtools_cloud_configured,
        inventory_status_label=inventory_status_label,
        inventory_location_label=inventory_location_label,
        inventory_condition_label=inventory_condition_label
    )


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    q = request.args.get("q", "").strip()
    conn = db()
    projects = fetch_visible_projects(conn, q)
    conn.close()
    return render_template("index.html", projects=projects, q=q)





@app.route("/desktop")
@login_required
def desktop_home():
    return redirect(url_for("index"))


@app.route("/mobile")
@login_required
def mobile_home():
    conn = db()
    project_count = len(fetch_visible_projects(conn))
    conn.close()
    return render_template("mobile_home.html", project_count=project_count)


@app.route("/mobile/projects")
@login_required
def mobile_projects():
    conn = db()
    projects = fetch_visible_projects(conn)
    conn.close()
    return render_template("mobile_projects.html", projects=projects)


@app.route("/mobile/projects/search")
@login_required
def mobile_project_search():
    q = request.args.get("q", "").strip()
    projects = []
    if q:
        conn = db()
        projects = fetch_visible_projects(conn, q)
        conn.close()
    return render_template("mobile_project_search.html", projects=projects, q=q)


@app.route("/mobile/inventory")
@login_required
def mobile_inventory():
    if not can_view_inventory():
        flash("You do not have permission to view inventory.")
        return redirect(url_for("mobile_home"))
    conn = db()
    selected_project_id = request.args.get("project_id", type=int)
    if selected_project_id and not user_can_access_project(conn, selected_project_id):
        selected_project_id = None
        flash("You do not have access to that project.")
    selected_room_id = request.args.get("room_id", type=int)
    selected_status = request.args.get("status", "")
    if selected_status not in INVENTORY_STATUS_LABELS:
        selected_status = ""
    q = request.args.get("q", "").strip()
    items = fetch_inventory_items(conn, {
        "q": q,
        "status": selected_status,
        "project_id": selected_project_id,
        "room_id": selected_room_id
    })
    projects = fetch_inventory_projects(conn)
    rooms = fetch_inventory_rooms(conn)
    conn.close()
    return render_template(
        "mobile_inventory.html",
        items=items,
        projects=projects,
        rooms=rooms,
        q=q,
        selected_status=selected_status,
        selected_project_id=selected_project_id,
        selected_room_id=selected_room_id,
        status_options=INVENTORY_STATUS_LABELS,
    )


@app.route("/mobile/time-clock", methods=["GET", "POST"])
@login_required
def mobile_time_clock_legacy():
    flash("Open a project before you clock in or clock out.")
    return redirect(url_for("mobile_home"))


@app.route("/mobile/project/<int:project_id>/time-clock", methods=["GET", "POST"])
@login_required
def mobile_time_clock(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("mobile_home"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("mobile_home"))

    if request.method == "POST":
        event_type = request.form.get("event_type")
        if event_type not in ["check_in", "check_out"]:
            conn.close()
            flash("Choose clock in or clock out.")
            return redirect(url_for("mobile_time_clock", project_id=project_id))
        try:
            latitude = float(request.form.get("latitude", ""))
            longitude = float(request.form.get("longitude", ""))
        except Exception:
            conn.close()
            flash("GPS location is required. Turn on Location Services/GPS and try again.")
            return redirect(url_for("mobile_time_clock", project_id=project_id))
        address = request.form.get("address", "").strip() or f"{latitude:.6f}, {longitude:.6f}"
        event_timezone = timezone_from_location(
            latitude,
            longitude,
            request.form.get("event_timezone") or APP_TIMEZONE
        )
        created_at = utc_now_iso()
        conn.execute(
            "INSERT INTO attendance_events (user_id, project_id, event_type, latitude, longitude, address, event_timezone, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (session.get("user_id"), project_id, event_type, latitude, longitude, address, event_timezone, created_at)
        )
        notify_admins_of_attendance(conn, project, event_type, latitude, longitude, address, created_at, event_timezone)
        conn.close()
        flash(("Clock in" if event_type == "check_in" else "Clock out") + " recorded.")
        return redirect(url_for("mobile_time_clock", project_id=project_id))

    events = conn.execute(
        """
        SELECT attendance_events.*, projects.name AS project_name
        FROM attendance_events
        LEFT JOIN projects ON attendance_events.project_id = projects.id
        WHERE attendance_events.user_id = %s AND attendance_events.project_id = %s
        ORDER BY attendance_events.created_at DESC
        LIMIT 10
        """,
        (session.get("user_id"), project_id)
    ).fetchall()
    conn.close()
    return render_template("mobile_time_clock.html", project=project, events=events)


@app.route("/mobile/location/status")
@login_required
def mobile_location_status():
    if is_main_admin():
        return {"active": False}
    conn = db()
    event = current_clock_in_event(conn)
    conn.close()
    if not event:
        return {"active": False}
    return {
        "active": True,
        "project_id": event.get("project_id"),
        "project_name": event.get("project_name") or "",
        "attendance_event_id": event.get("id"),
        "interval_ms": 60000
    }


@app.route("/mobile/location/ping", methods=["POST"])
@login_required
def mobile_location_ping():
    if is_main_admin():
        return {"ok": False, "active": False}
    data = request.get_json(silent=True) or request.form
    try:
        latitude = float(data.get("latitude", ""))
        longitude = float(data.get("longitude", ""))
        accuracy = data.get("accuracy")
        accuracy = float(accuracy) if accuracy not in [None, ""] else None
    except Exception:
        return {"ok": False, "active": True, "message": "GPS location is required."}, 400

    conn = db()
    event = current_clock_in_event(conn)
    if not event:
        conn.close()
        return {"ok": True, "active": False}

    event_timezone = timezone_from_location(
        latitude,
        longitude,
        data.get("event_timezone") or event_timezone_name(event)
    )
    try:
        ensure_worker_location_tables(conn)
    except Exception as e:
        print("Worker location table setup failed:", e)
        conn.close()
        return {"ok": False, "active": True, "message": "Location tracking table is not ready."}, 200
    conn.execute(
        """
        INSERT INTO worker_location_pings
        (user_id, project_id, attendance_event_id, latitude, longitude, accuracy, address, event_timezone, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session.get("user_id"),
            event.get("project_id"),
            event.get("id"),
            latitude,
            longitude,
            accuracy,
            (data.get("address") or "").strip(),
            event_timezone,
            utc_now_iso()
        )
    )
    conn.commit()
    conn.close()
    return {"ok": True, "active": True}



@app.route("/mobile/project/<int:project_id>/materials", methods=["GET", "POST"])
@login_required
def mobile_project_materials(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("mobile_home"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("mobile_home"))
    if not can_view_inventory():
        conn.close()
        flash("You do not have permission to view material inventory.")
        return redirect(url_for("mobile_project", project_id=project_id))

    if request.method == "POST":
        if not can_edit_inventory():
            conn.close()
            flash("You do not have permission to add material inventory.")
            return redirect(url_for("mobile_project_materials", project_id=project_id))

        error = insert_inventory_item(conn, fixed_project_id=project_id)
        if error:
            conn.close()
            flash(error)
            return redirect(url_for("mobile_project_materials", project_id=project_id))
        conn.commit()
        flash("Inventory item added.")

    materials = fetch_inventory_items(conn, {"project_id": project_id})
    rooms = fetch_inventory_rooms(conn, project_id)
    conn.close()
    return render_template(
        "mobile_materials.html",
        project=project,
        materials=materials,
        rooms=rooms,
        today=local_now().date().isoformat(),
        status_options=INVENTORY_STATUS_LABELS,
        location_options=INVENTORY_LOCATION_LABELS,
        condition_options=INVENTORY_CONDITION_LABELS
    )



@app.route("/mobile/project/<int:project_id>")
@login_required
def mobile_project(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("mobile_home"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("mobile_home"))
    ensure_project_blueprints(conn, project)
    blueprints = conn.execute(
        "SELECT * FROM project_blueprints WHERE project_id = %s ORDER BY id",
        (project_id,)
    ).fetchall()
    selected_blueprint_id = request.args.get("blueprint_id", type=int)
    active_blueprint = None
    if selected_blueprint_id:
        active_blueprint = conn.execute(
            "SELECT * FROM project_blueprints WHERE project_id = %s AND id = %s",
            (project_id, selected_blueprint_id)
        ).fetchone()
    rooms = conn.execute("SELECT * FROM rooms WHERE project_id = %s ORDER BY id", (project_id,)).fetchall()
    conn.close()
    return render_template(
        "mobile_project.html",
        project=project,
        rooms=rooms,
        blueprints=blueprints,
        active_blueprint=active_blueprint
    )


@app.route("/mobile/project/<int:project_id>/rooms", methods=["POST"])
@login_required
def mobile_add_room(project_id):
    if not (is_main_admin() or has_perm("create_rooms")):
        flash("You do not have permission to create rooms.")
        return redirect(url_for("mobile_project", project_id=project_id))

    name = request.form.get("name", "").strip()
    if not name:
        flash("Room name is required.")
        return redirect(url_for("mobile_project", project_id=project_id))

    conn = db()
    project = conn.execute("SELECT id FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("mobile_home"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("mobile_home"))

    conn.execute(
        "INSERT INTO rooms (project_id, name, x, y, w, h, polygon_points, category, room_color, created_at) VALUES (%s, %s, 0, 0, 0, 0, '', %s, %s, %s)",
        (
            project_id,
            name,
            request.form.get("category", "general"),
            request.form.get("room_color", "blue"),
            datetime.now().isoformat()
        )
    )
    conn.commit()
    conn.close()
    flash("Room created.")
    return redirect(url_for("mobile_project", project_id=project_id))


@app.route("/mobile/room/<int:room_id>", methods=["GET", "POST"])
@login_required
def mobile_room(room_id):
    conn = db()
    room = conn.execute("SELECT * FROM rooms WHERE id = %s", (room_id,)).fetchone()
    if not room:
        conn.close()
        flash("Room not found.")
        return redirect(url_for("mobile_home"))

    project = conn.execute("SELECT * FROM projects WHERE id = %s", (room["project_id"],)).fetchone()
    if not user_can_access_project(conn, room["project_id"]):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("mobile_home"))
    rooms = conn.execute("SELECT id, name, project_id FROM rooms WHERE project_id = %s ORDER BY id", (room["project_id"],)).fetchall()
    tasks = conn.execute(
        """
        SELECT tasks.*, users.name AS assigned_user_name
        FROM tasks
        LEFT JOIN users ON tasks.assigned_user_id = users.id
        WHERE (tasks.room_id = %s OR EXISTS (SELECT 1 FROM task_attachments WHERE task_attachments.task_id = tasks.id AND task_attachments.room_id = %s))
          AND (tasks.assigned_user_id = %s OR %s = 'admin')
        ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
        """,
        (room_id, room_id, session.get("user_id"), session.get("role"))
    ).fetchall()
    tasks = load_task_details(conn, tasks, room_id)
    room_inventory = fetch_inventory_items(conn, {"room_id": room_id}) if can_view_inventory() else []

    if request.method == "POST":
        if not can_add_notes():
            flash("You can view notes and photos, but you cannot add new ones.")
            return redirect(url_for("mobile_room", room_id=room_id))

        file = request.files.get("photo") or request.files.get("photo_camera")
        audio = request.files.get("audio")
        photo_file = upload_file_to_storage(file) if file and file.filename and allowed_photo(file.filename) else None
        audio_file = upload_file_to_storage(audio) if audio and audio.filename and allowed_audio(audio.filename) else None

        conn.execute(
            "INSERT INTO notes (room_id, user_id, note_date, comment, photo_file, audio_file, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (room_id, session.get("user_id"), request.form["note_date"], request.form["comment"].strip(), photo_file, audio_file, datetime.now().isoformat())
        )
        conn.commit()
        notified = notify_admins_of_field_note(conn, project, room, request.form["comment"].strip(), photo_file, audio_file, request.form["note_date"])
        if notified:
            flash("Comment/photo/audio added.")
        else:
            flash("Comment/photo/audio added. Admin notification or email could not be sent.")

    selected_date = request.args.get("date", "")
    query = "SELECT notes.*, users.name AS user_name FROM notes LEFT JOIN users ON notes.user_id = users.id WHERE room_id = %s"
    params = [room_id]
    if selected_date:
        query += " AND note_date = %s"
        params.append(selected_date)
    query += " ORDER BY note_date DESC, created_at DESC"
    notes = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    return render_template("mobile_room.html", room=room, project=project, rooms=rooms, notes=notes, tasks=tasks, room_inventory=room_inventory, selected_date=selected_date)


@app.route("/routes-check")
def routes_check():
    return "<h1>ProjectONus Routes Active</h1><br>" + "<br>".join(sorted(str(r) for r in app.url_map.iter_rules()))


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = db()
    admin_exists = has_admin_account(conn)
    conn.close()

    if request.method == "POST":
        if not admin_exists:
            flash("Create the first admin account before logging in.")
            return redirect(url_for("admin_setup_request"))
        login_name = request.form["email"].strip().lower()
        password = request.form["password"]
        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE role = 'admin' AND (email = %s OR lower(coalesce(username, '')) = %s)",
            (login_name, login_name)
        ).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session.permanent = False
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            return redirect(url_for("index"))
        flash("Invalid admin login.")
    return render_template("login.html", admin_exists=admin_exists)


@app.route("/mobile/login", methods=["GET", "POST"])
def mobile_login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pin = request.form["pin"].strip()
        stay_logged_in = request.form.get("stay_logged_in") == "on"
        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE email = %s AND role <> 'admin'",
            (email,)
        ).fetchone()
        conn.close()
        if user and user.get("pin_hash") and check_password_hash(user["pin_hash"], pin):
            session.permanent = stay_logged_in
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            return redirect(url_for("mobile_home"))
        flash("Invalid email or PIN.")
        return render_template("mobile_login.html", email=email, stay_logged_in=stay_logged_in)
    return render_template("mobile_login.html", email=request.args.get("email", "").strip().lower(), stay_logged_in=True)


@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup_request():
    conn = db()
    if has_admin_account(conn):
        conn.close()
        flash("An admin account already exists. Use forgot password if you need access.")
        return redirect(url_for("login"))

    setup_link = ""
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        token = new_token()
        existing = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET role = 'admin', setup_token = %s, setup_created_at = %s WHERE id = %s",
                (token, datetime.now().isoformat(), existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO users (name, email, password_hash, role, setup_token, setup_created_at, created_at) VALUES (%s, %s, %s, 'admin', %s, %s, %s)",
                ("Admin", email, unusable_password_hash(), token, datetime.now().isoformat(), datetime.now().isoformat())
            )
        conn.commit()
        setup_link = external_url("admin_create_login", token=token)
        sent = send_email(
            email,
            "Create your ProjectONus admin login",
            "Use this link on the desktop version to create your admin username and password:\n\n" + setup_link
        )
        if sent:
            flash("Admin setup email sent.")
            conn.close()
            return redirect(url_for("login"))
        flash("Email could not be sent because SMTP is not configured or failed.")
    conn.close()
    return render_template("admin_setup.html", setup_link=setup_link)


@app.route("/admin/create-login/<token>", methods=["GET", "POST"])
def admin_create_login(token):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE role = 'admin' AND setup_token = %s", (token,)).fetchone()
    if not user:
        conn.close()
        flash("This admin setup link is invalid or has already been used.")
        return redirect(url_for("login"))

    if request.method == "POST":
        username = request.form["username"].strip().lower()
        name = request.form.get("name", "").strip() or "Admin"
        password = request.form["password"]
        confirm = request.form["confirm_password"]
        if password != confirm:
            flash("Passwords do not match.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        elif conn.execute("SELECT id FROM users WHERE lower(coalesce(username, '')) = %s AND id <> %s", (username, user["id"])).fetchone():
            flash("That username is already taken.")
        else:
            conn.execute(
                "UPDATE users SET name = %s, username = %s, password_hash = %s, setup_token = NULL, setup_created_at = NULL WHERE id = %s",
                (name, username, generate_password_hash(password), user["id"])
            )
            conn.commit()
            conn.close()
            flash("Admin login created. You can sign in now.")
            return redirect(url_for("login"))
    conn.close()
    return render_template("admin_create_login.html", user=user)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    reset_link = ""
    if request.method == "POST":
        login_name = request.form["email"].strip().lower()
        token = new_token()
        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE role = 'admin' AND (email = %s OR lower(coalesce(username, '')) = %s)",
            (login_name, login_name)
        ).fetchone()
        if user:
            conn.execute(
                "UPDATE users SET reset_token = %s, reset_created_at = %s WHERE id = %s",
                (token, datetime.now().isoformat(), user["id"])
            )
            conn.commit()
            reset_link = external_url("reset_password", token=token)
            sent = send_email(
                user["email"],
                "Reset your ProjectONus admin password",
                "Use this link to create a new admin password:\n\n" + reset_link
            )
            if sent:
                flash("Password reset email sent.")
            else:
                flash("Email could not be sent because SMTP is not configured or failed.")
        else:
            flash("If that admin account exists, a reset email will be sent.")
        conn.close()
    return render_template("forgot_password.html", reset_link=reset_link)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE role = 'admin' AND reset_token = %s", (token,)).fetchone()
    if not user:
        conn.close()
        flash("This password reset link is invalid or has already been used.")
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form["password"]
        confirm = request.form["confirm_password"]
        if password != confirm:
            flash("Passwords do not match.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        else:
            conn.execute(
                "UPDATE users SET password_hash = %s, reset_token = NULL, reset_created_at = NULL WHERE id = %s",
                (generate_password_hash(password), user["id"])
            )
            conn.commit()
            conn.close()
            flash("Password updated. You can sign in now.")
            return redirect(url_for("login"))
    conn.close()
    return render_template("reset_password.html", user=user)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():

    conn = db()
    if request.method == "POST":
        try:
            email = request.form["email"].strip().lower()
            role = request.form.get("role", "worker")
            if role not in ["customer", "worker"]:
                role = "worker"
            pin = request.form["pin"].strip()
            phone_number = request.form.get("phone_number", "").strip()
            sms_enabled = "sms_enabled" in request.form
            if len(pin) < 4:
                conn.close()
                flash("PIN must be at least 4 digits.")
                return redirect(url_for("users"))

            invite_token = new_token()
            conn.execute(
                "INSERT INTO users (name, email, phone_number, sms_enabled, password_hash, pin_hash, invite_token, invite_sent_at, role, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    request.form["name"].strip(),
                    email,
                    phone_number,
                    sms_enabled,
                    unusable_password_hash(),
                    generate_password_hash(pin),
                    invite_token,
                    datetime.now().isoformat(),
                    role,
                    datetime.now().isoformat()
                )
            ).fetchone()
            conn.commit()
            invite_link = external_url("mobile_login", email=email, invite=invite_token)
            sent = send_email(
                email,
                "You are invited to ProjectONus",
                "Open this mobile link and sign in with your email and the PIN provided by the admin:\n\n" + invite_link
            )
            if sent:
                flash("User added and mobile invitation email sent. Share the PIN with the user.")
            else:
                flash("User added. Email could not be sent, so share the mobile link and PIN with the user: " + invite_link)
        except Exception:
            conn.rollback()
            flash("That email may already exist.")

    users = conn.execute("SELECT id, name, email, phone_number, sms_enabled, role, created_at, invite_token FROM users ORDER BY name").fetchall()
    conn.close()
    return render_template("users.html", users=users)


@app.route("/users/<int:user_id>/pin", methods=["POST"])
@admin_required
def update_user_pin(user_id):
    pin = request.form.get("pin", "").strip()
    if len(pin) < 4:
        flash("PIN must be at least 4 digits.")
        return redirect(url_for("users"))
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = %s AND role <> 'admin'", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("User not found.")
        return redirect(url_for("users"))
    invite_token = user.get("invite_token") or new_token()
    conn.execute(
        "UPDATE users SET pin_hash = %s, invite_token = %s, invite_sent_at = %s WHERE id = %s",
        (generate_password_hash(pin), invite_token, datetime.now().isoformat(), user_id)
    )
    conn.commit()
    invite_link = external_url("mobile_login", email=user["email"], invite=invite_token)
    sent = send_email(
        user["email"],
        "Your ProjectONus mobile invitation",
        "Open this mobile link and sign in with your email and the PIN provided by the admin:\n\n" + invite_link
    )
    conn.close()
    if sent:
        flash("PIN updated and invitation email sent. Share the PIN with the user.")
    else:
        flash("PIN updated. Email could not be sent, so share this mobile link with the user: " + invite_link)
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/phone", methods=["POST"])
@admin_required
def update_user_phone(user_id):
    conn = db()
    user = conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("User not found.")
        return redirect(url_for("users"))
    conn.execute(
        "UPDATE users SET phone_number = %s, sms_enabled = %s WHERE id = %s",
        (
            request.form.get("phone_number", "").strip(),
            "sms_enabled" in request.form,
            user_id
        )
    )
    conn.commit()
    conn.close()
    flash("Text message settings updated.")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/sms", methods=["POST"])
@admin_required
def send_user_sms(user_id):
    message = request.form.get("message", "").strip()
    if not message:
        flash("Write a text message before sending.")
        return redirect(url_for("users"))
    conn = db()
    user = conn.execute("SELECT name, phone_number, sms_enabled FROM users WHERE id = %s", (user_id,)).fetchone()
    conn.close()
    if not user or not user.get("phone_number"):
        flash("This user does not have a cellphone number saved.")
        return redirect(url_for("users"))
    if not user.get("sms_enabled"):
        flash("Text messages are not enabled for this user.")
        return redirect(url_for("users"))
    sent, sms_error = send_sms(user["phone_number"], f"ProjectONus: {message}", return_error=True)
    if sent:
        flash(f"Text message sent to {user.get('name') or 'user'}.")
    else:
        flash("Text message could not be sent. " + (sms_error or "Check Twilio settings on Render."))
    return redirect(url_for("users"))




@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    if user_id == session.get("user_id"):
        flash("You cannot delete your own admin account while logged in.")
        return redirect(url_for("users"))

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("User not found.")
        return redirect(url_for("users"))

    conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()

    flash("User deleted.")
    return redirect(url_for("users"))


@app.route("/projects/new", methods=["GET", "POST"])
@admin_required
def new_project():
    if request.method == "POST":
        name = request.form["name"].strip()
        customer_name = request.form.get("customer_name", "").strip()
        customer_address_parts = project_address_from_form()
        customer_street, customer_address, customer_city, customer_state, customer_zip = customer_address_parts
        billing_same_as_customer, billing_street, billing_address, billing_city, billing_state, billing_zip = billing_address_from_form(customer_address_parts)
        customer_phone = request.form.get("customer_phone", "").strip()
        customer_email = request.form.get("customer_email", "").strip()
        file = request.files.get("blueprint")
        blueprint_file = None
        blueprint_preview_file = None

        if file and allowed_blueprint(file.filename):
            raw = file.read()
            blueprint_file = upload_bytes_to_storage(raw, file.filename, file.content_type or "application/octet-stream")
            # PDF blueprints are rendered in the browser with PDF.js for sharp vector quality.
            # Do not rasterize large PDFs on Render server because it can crash due to memory limits.
            blueprint_preview_file = None if is_pdf(file.filename) else blueprint_file

        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO projects
            (name, customer_name, customer_street, customer_address, customer_city, customer_state, customer_zip, billing_street, billing_address, billing_city, billing_state, billing_zip, billing_same_as_customer, customer_phone, customer_email, blueprint_file, blueprint_preview_file, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                name,
                customer_name,
                customer_street,
                customer_address,
                customer_city,
                customer_state,
                customer_zip,
                billing_street,
                billing_address,
                billing_city,
                billing_state,
                billing_zip,
                billing_same_as_customer,
                customer_phone,
                customer_email,
                blueprint_file,
                blueprint_preview_file,
                datetime.now().isoformat()
            )
        )
        project_id = cur.fetchone()["id"]
        conn.commit()
        conn.close()
        return redirect(url_for("project", project_id=project_id))

    return render_template("new_project.html")


@app.route("/project/<int:project_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_project(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            conn.close()
            flash("Project name is required.")
            return redirect(url_for("edit_project", project_id=project_id))
        customer_address_parts = project_address_from_form()
        customer_street, customer_address, customer_city, customer_state, customer_zip = customer_address_parts
        billing_same_as_customer, billing_street, billing_address, billing_city, billing_state, billing_zip = billing_address_from_form(customer_address_parts)

        conn.execute(
            """
            UPDATE projects
            SET name = %s,
                customer_name = %s,
                customer_street = %s,
                customer_address = %s,
                customer_city = %s,
                customer_state = %s,
                customer_zip = %s,
                billing_street = %s,
                billing_address = %s,
                billing_city = %s,
                billing_state = %s,
                billing_zip = %s,
                billing_same_as_customer = %s,
                customer_phone = %s,
                customer_email = %s
            WHERE id = %s
            """,
            (
                name,
                request.form.get("customer_name", "").strip(),
                customer_street,
                customer_address,
                customer_city,
                customer_state,
                customer_zip,
                billing_street,
                billing_address,
                billing_city,
                billing_state,
                billing_zip,
                billing_same_as_customer,
                request.form.get("customer_phone", "").strip(),
                request.form.get("customer_email", "").strip(),
                project_id
            )
        )
        conn.commit()
        conn.close()
        flash("Project updated.")
        return redirect(url_for("project", project_id=project_id))

    conn.close()
    return render_template("edit_project.html", project=project)


@app.route("/suppliers", methods=["GET", "POST"])
@admin_required
def suppliers():
    conn = db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            conn.close()
            flash("Supplier name is required.")
            return redirect(url_for("suppliers"))
        street, address, city, state, zip_code = supplier_address_from_form("")
        supplier_id = request.form.get("supplier_id", type=int)
        values = (
            name,
            request.form.get("contact_name", "").strip(),
            request.form.get("email", "").strip(),
            request.form.get("phone", "").strip(),
            street,
            address,
            city,
            state,
            zip_code,
            request.form.get("website", "").strip(),
            request.form.get("notes", "").strip(),
            utc_now_iso()
        )
        if supplier_id:
            conn.execute(
                """
                UPDATE suppliers
                SET name = %s, contact_name = %s, email = %s, phone = %s, street = %s, address = %s,
                    city = %s, state = %s, zip = %s, website = %s, notes = %s, updated_at = %s
                WHERE id = %s
                """,
                (*values, supplier_id)
            )
            flash("Supplier updated.")
        else:
            conn.execute(
                """
                INSERT INTO suppliers
                (name, contact_name, email, phone, street, address, city, state, zip, website, notes, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (*values[:-1], utc_now_iso(), values[-1])
            )
            flash("Supplier added.")
        conn.commit()
        conn.close()
        return redirect(url_for("suppliers"))

    supplier_rows = fetch_suppliers(conn)
    conn.close()
    return render_template("suppliers.html", suppliers=supplier_rows)


@app.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
@admin_required
def delete_supplier(supplier_id):
    conn = db()
    conn.execute("DELETE FROM suppliers WHERE id = %s", (supplier_id,))
    conn.commit()
    conn.close()
    flash("Supplier deleted.")
    return redirect(url_for("suppliers"))


@app.route("/inventory", methods=["GET", "POST"])
@login_required
def inventory():
    if not can_view_inventory():
        flash("You do not have permission to view inventory.")
        return redirect(url_for("index"))

    conn = db()
    if request.method == "POST":
        if not can_edit_inventory():
            conn.close()
            flash("You do not have permission to add inventory.")
            return redirect(url_for("inventory"))
        error = insert_inventory_item(conn)
        if error:
            conn.close()
            flash(error)
            return redirect(url_for("inventory"))
        conn.commit()
        conn.close()
        flash("Inventory item added.")
        return redirect(url_for("inventory"))

    selected_project_id = request.args.get("project_id", type=int)
    if selected_project_id and not user_can_access_project(conn, selected_project_id):
        selected_project_id = None
        flash("You do not have access to that project.")
    selected_room_id = request.args.get("room_id", type=int)
    selected_status = request.args.get("status", "")
    if selected_status not in INVENTORY_STATUS_LABELS:
        selected_status = ""
    q = request.args.get("q", "").strip()
    items = fetch_inventory_items(conn, {
        "q": q,
        "status": selected_status,
        "project_id": selected_project_id,
        "room_id": selected_room_id
    })
    projects = fetch_inventory_projects(conn)
    rooms = fetch_inventory_rooms(conn)
    conn.close()
    return render_template(
        "inventory.html",
        items=items,
        projects=projects,
        rooms=rooms,
        q=q,
        selected_status=selected_status,
        selected_project_id=selected_project_id,
        selected_room_id=selected_room_id,
        today=local_now().date().isoformat(),
        status_options=INVENTORY_STATUS_LABELS,
        location_options=INVENTORY_LOCATION_LABELS,
        condition_options=INVENTORY_CONDITION_LABELS
    )


@app.route("/inventory/<int:item_id>/status", methods=["POST"])
@login_required
def update_inventory_status(item_id):
    if not can_edit_inventory():
        flash("You do not have permission to update inventory.")
        return redirect(safe_next_url("inventory"))
    new_status = clean_inventory_status(request.form.get("status") or request.form.get("material_status"))
    posted_project = "project_id" in request.form
    posted_room = "room_id" in request.form
    project_id = optional_int(request.form.get("project_id")) if posted_project else None
    room_id = optional_int(request.form.get("room_id")) if posted_room else None

    conn = db()
    item = conn.execute("SELECT * FROM inventory_items WHERE id = %s", (item_id,)).fetchone()
    if not item:
        conn.close()
        flash("Inventory item not found.")
        return redirect(safe_next_url("inventory"))
    if not inventory_item_access_allowed(conn, item):
        conn.close()
        flash("You do not have access to that inventory item.")
        return redirect(url_for("inventory"))
    project_id = project_id if posted_project else item.get("project_id")
    room_id = room_id if posted_room else item.get("room_id")
    project_id, room_id, error = validate_inventory_allocation(conn, project_id, room_id)
    if error:
        conn.close()
        flash(error)
        return redirect(safe_next_url("inventory"))

    now = utc_now_iso()
    used_by = session.get("user_id") if new_status == "used" else None
    used_at = now if new_status == "used" else None
    purchased_by = item.get("purchased_by")
    purchased_at = item.get("purchased_at")
    if new_status in ["available", "used"] and item.get("status") == "needs_purchase" and not purchased_at:
        purchased_by = session.get("user_id")
        purchased_at = now
    conn.execute(
        """
        UPDATE inventory_items
        SET status = %s,
            project_id = %s,
            room_id = %s,
            location_type = %s,
            location_detail = %s,
            purchased_by = %s,
            purchased_at = %s,
            used_by = %s,
            used_at = %s,
            used_note = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (
            new_status,
            project_id,
            room_id,
            clean_inventory_location(request.form.get("location_type") or item.get("location_type")),
            request.form.get("location_detail", item.get("location_detail") or "").strip(),
            purchased_by,
            purchased_at,
            used_by,
            used_at,
            request.form.get("used_note", item.get("used_note") or "").strip(),
            now,
            item_id
        )
    )
    conn.commit()
    conn.close()
    flash("Inventory item updated.")
    return redirect(safe_next_url("inventory"))


@app.route("/inventory/<int:item_id>/delete", methods=["POST"])
@admin_required
def delete_inventory_item(item_id):
    conn = db()
    conn.execute("DELETE FROM inventory_items WHERE id = %s", (item_id,))
    conn.commit()
    conn.close()
    flash("Inventory item deleted.")
    return redirect(safe_next_url("inventory"))




@app.route("/project/<int:project_id>/materials", methods=["GET", "POST"])
@login_required
def project_materials(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    if not can_view_inventory():
        conn.close()
        flash("You do not have permission to view material inventory.")
        return redirect(url_for("index"))

    if request.method == "POST":
        if not can_edit_inventory():
            conn.close()
            flash("You do not have permission to add material inventory.")
            return redirect(url_for("project_materials", project_id=project_id))

        error = insert_inventory_item(conn, fixed_project_id=project_id)
        if error:
            conn.close()
            flash(error)
            return redirect(url_for("project_materials", project_id=project_id))
        conn.commit()
        flash("Inventory item added.")

    materials = fetch_inventory_items(conn, {"project_id": project_id})
    rooms = fetch_inventory_rooms(conn, project_id)
    conn.close()
    return render_template(
        "materials.html",
        project=project,
        materials=materials,
        rooms=rooms,
        today=local_now().date().isoformat(),
        status_options=INVENTORY_STATUS_LABELS,
        location_options=INVENTORY_LOCATION_LABELS,
        condition_options=INVENTORY_CONDITION_LABELS
    )


@app.route("/project/<int:project_id>/materials/import-dtools", methods=["POST"])
@admin_required
def import_dtools_inventory(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))

    external_ref = request.form.get("dtools_ref", "").strip()
    endpoint_path = request.form.get("dtools_endpoint_path", "").strip()
    if not external_ref:
        conn.close()
        flash("Enter the D-Tools Cloud Project or Quote ID.")
        return redirect(url_for("project_materials", project_id=project_id))
    try:
        payload = dtools_cloud_fetch_payload(external_ref, endpoint_path)
        result = import_dtools_materials(conn, project_id, external_ref, payload)
        conn.commit()
        message = f"D-Tools import complete: {result['imported']} item(s) added as Needs Purchase."
        if result["skipped"]:
            message += f" {result['skipped']} duplicate item(s) skipped."
        if result["unmatched_rooms"]:
            message += f" {result['unmatched_rooms']} item(s) did not match a room name and were placed in Project general."
        if result["found"] == 0:
            message = "D-Tools connected, but no material items were found in that response. Check the endpoint path in Settings."
        flash(message)
    except Exception as e:
        conn.rollback()
        flash(str(e))
    conn.close()
    return redirect(url_for("project_materials", project_id=project_id))


@app.route("/project/<int:project_id>/materials/<int:material_id>/status", methods=["POST"])
@login_required
def update_material_status(project_id, material_id):
    if not can_edit_inventory():
        flash("You do not have permission to update material status.")
        return redirect(url_for("project_materials", project_id=project_id))

    legacy_status = request.form.get("material_status", "")
    status_map = {"in_stock": "available", "not_in_stock": "needs_purchase", "used": "used"}
    new_status = clean_inventory_status(request.form.get("status") or status_map.get(legacy_status, legacy_status))

    conn = db()
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    item = conn.execute("SELECT * FROM inventory_items WHERE id = %s AND project_id = %s", (material_id, project_id)).fetchone()
    if not item:
        conn.close()
        flash("Inventory item not found.")
        return redirect(url_for("project_materials", project_id=project_id))
    conn.execute(
        """
        UPDATE inventory_items
        SET status = %s,
            room_id = %s,
            used_by = %s,
            used_at = %s,
            used_note = %s,
            updated_at = %s
        WHERE id = %s AND project_id = %s
        """,
        (
            new_status,
            optional_int(request.form.get("room_id")) or item.get("room_id"),
            session.get("user_id") if new_status == "used" else None,
            utc_now_iso() if new_status == "used" else None,
            request.form.get("used_note", item.get("used_note") or "").strip(),
            utc_now_iso(),
            material_id,
            project_id
        )
    )
    conn.commit()
    conn.close()
    flash("Inventory item updated.")
    if "/mobile/" in (request.referrer or ""):
        return redirect(url_for("mobile_project_materials", project_id=project_id))
    return redirect(url_for("project_materials", project_id=project_id))


@app.route("/project/<int:project_id>/materials/<int:material_id>/delete", methods=["POST"])
@admin_required
def delete_material(project_id, material_id):
    conn = db()
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    conn.execute("DELETE FROM inventory_items WHERE id = %s AND project_id = %s", (material_id, project_id))
    conn.commit()
    conn.close()
    flash("Inventory item deleted.")
    return redirect(url_for("project_materials", project_id=project_id))



@app.route("/project/<int:project_id>")
@login_required
def project(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))

    ensure_project_blueprints(conn, project)

    blueprints = conn.execute(
        "SELECT * FROM project_blueprints WHERE project_id = %s ORDER BY id",
        (project_id,)
    ).fetchall()

    selected_id = request.args.get("blueprint_id", type=int)
    active_blueprint = None

    if selected_id:
        active_blueprint = conn.execute(
            "SELECT * FROM project_blueprints WHERE project_id = %s AND id = %s",
            (project_id, selected_id)
        ).fetchone()

    if not active_blueprint and blueprints:
        active_blueprint = blueprints[0]

    if active_blueprint:
        rooms = conn.execute(
            "SELECT * FROM rooms WHERE project_id = %s AND (blueprint_id = %s OR blueprint_id IS NULL) ORDER BY id",
            (project_id, active_blueprint["id"])
        ).fetchall()
    else:
        rooms = conn.execute(
            "SELECT * FROM rooms WHERE project_id = %s ORDER BY id",
            (project_id,)
        ).fetchall()

    conn.close()
    return render_template(
        "project.html",
        project=project,
        rooms=rooms,
        blueprints=blueprints,
        active_blueprint=active_blueprint
    )




@app.route("/project/<int:project_id>/blueprints/add", methods=["POST"])
@admin_required
def add_project_blueprint(project_id):
    name = request.form.get("name", "").strip() or "Blueprint"
    file = request.files.get("blueprint")

    if not file or not file.filename:
        flash("Please choose a blueprint PDF or image.")
        return redirect(url_for("project", project_id=project_id))

    if not allowed_blueprint(file.filename):
        flash("Blueprint must be PDF, JPG, PNG, or WEBP.")
        return redirect(url_for("project", project_id=project_id))

    raw = file.read()
    if not raw:
        flash("The selected blueprint file was empty. Please choose the file again.")
        return redirect(url_for("project", project_id=project_id))

    blueprint_file = upload_bytes_to_storage(
        raw,
        file.filename,
        file.content_type or "application/octet-stream"
    )

    blueprint_preview_file = None if is_pdf(file.filename) else blueprint_file

    conn = db()
    project = conn.execute("SELECT id FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))

    new_bp = conn.execute(
        "INSERT INTO project_blueprints (project_id, name, blueprint_file, blueprint_preview_file, created_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (
            project_id,
            name,
            blueprint_file,
            blueprint_preview_file,
            datetime.now().isoformat()
        )
    ).fetchone()
    conn.commit()
    conn.close()

    flash("Blueprint sheet added.")
    return redirect(url_for("project", project_id=project_id, blueprint_id=new_bp["id"], v=uuid.uuid4().hex))


@app.route("/project/<int:project_id>/blueprints/<int:blueprint_id>/delete", methods=["POST"])
@admin_required
def delete_project_blueprint(project_id, blueprint_id):
    conn = db()

    # Keep rooms, only unlink them from this blueprint.
    conn.execute(
        "UPDATE rooms SET blueprint_id = NULL WHERE project_id = %s AND blueprint_id = %s",
        (project_id, blueprint_id)
    )

    conn.execute(
        "DELETE FROM project_blueprints WHERE project_id = %s AND id = %s",
        (project_id, blueprint_id)
    )
    conn.commit()

    next_bp = conn.execute(
        "SELECT id FROM project_blueprints WHERE project_id = %s ORDER BY id LIMIT 1",
        (project_id,)
    ).fetchone()

    conn.close()
    flash("Blueprint sheet deleted. Rooms were kept.")

    if next_bp:
        return redirect(url_for("project", project_id=project_id, blueprint_id=next_bp["id"]))
    return redirect(url_for("project", project_id=project_id))



@app.route("/project/<int:project_id>/rooms", methods=["POST"])
@login_required
def add_room(project_id):
    if not (is_main_admin() or has_perm("create_rooms")):
        flash("You do not have permission to create rooms.")
        return redirect(url_for("project", project_id=project_id))

    polygon_points = request.form.get("polygon_points", "").strip()
    blueprint_id = request.form.get("blueprint_id") or None
    room_action = request.form.get("room_action", "create")
    name = request.form.get("name", "").strip()
    existing_room_id = request.form.get("existing_room_id", type=int)
    if room_action == "link" and not existing_room_id:
        flash("Choose an existing room to link this trace.")
        if blueprint_id:
            return redirect(url_for("project", project_id=project_id, blueprint_id=blueprint_id))
        return redirect(url_for("project", project_id=project_id))
    if room_action != "link" and not name:
        flash("Room name is required.")
        if blueprint_id:
            return redirect(url_for("project", project_id=project_id, blueprint_id=blueprint_id))
        return redirect(url_for("project", project_id=project_id))

    room_blueprint_id = blueprint_id if polygon_points else None

    conn = db()
    project = conn.execute("SELECT id FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    if room_action == "link":
        existing_room = conn.execute(
            "SELECT id, name FROM rooms WHERE id = %s AND project_id = %s",
            (existing_room_id, project_id)
        ).fetchone()
        if not existing_room:
            conn.close()
            flash("Existing room not found in this project.")
            if blueprint_id:
                return redirect(url_for("project", project_id=project_id, blueprint_id=blueprint_id))
            return redirect(url_for("project", project_id=project_id))
        conn.execute(
            """
            UPDATE rooms
            SET blueprint_id = %s,
                x = %s,
                y = %s,
                w = %s,
                h = %s,
                polygon_points = %s,
                category = %s,
                room_color = %s
            WHERE id = %s AND project_id = %s
            """,
            (
                room_blueprint_id,
                float(request.form.get("x") or 0),
                float(request.form.get("y") or 0),
                float(request.form.get("w") or 0),
                float(request.form.get("h") or 0),
                polygon_points,
                request.form.get("category", "general"),
                request.form.get("room_color", "blue"),
                existing_room_id,
                project_id
            )
        )
        conn.commit()
        conn.close()
        flash(f"Trace linked to existing room: {existing_room['name']}.")
        if blueprint_id:
            return redirect(url_for("project", project_id=project_id, blueprint_id=blueprint_id))
        return redirect(url_for("project", project_id=project_id))
    conn.execute(
        "INSERT INTO rooms (project_id, blueprint_id, name, x, y, w, h, polygon_points, category, room_color, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project_id,
            room_blueprint_id,
            name,
            float(request.form.get("x") or 0),
            float(request.form.get("y") or 0),
            float(request.form.get("w") or 0),
            float(request.form.get("h") or 0),
            polygon_points,
            request.form.get("category", "general"),
            request.form.get("room_color", "blue"),
            datetime.now().isoformat()
        )
    )
    conn.commit()
    conn.close()

    flash("Room added.")
    if blueprint_id:
        return redirect(url_for("project", project_id=project_id, blueprint_id=blueprint_id))
    return redirect(url_for("project", project_id=project_id))



@app.route("/room/<int:room_id>", methods=["GET", "POST"])
@login_required
def room(room_id):
    conn = db()
    room = conn.execute("SELECT * FROM rooms WHERE id = %s", (room_id,)).fetchone()
    if not room:
        conn.close()
        flash("Room not found.")
        return redirect(url_for("index"))
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (room["project_id"],)).fetchone()
    if not user_can_access_project(conn, room["project_id"]):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    project_rooms = conn.execute("SELECT id, name, project_id FROM rooms WHERE project_id = %s ORDER BY id", (room["project_id"],)).fetchall()
    users = conn.execute(
        "SELECT id, name, email, role FROM users ORDER BY CASE WHEN role = 'admin' THEN 0 ELSE 1 END, name"
    ).fetchall() if is_main_admin() else []
    suppliers = fetch_suppliers(conn) if is_main_admin() else []
    tasks = conn.execute(
        """
        SELECT tasks.*, users.name AS assigned_user_name
        FROM tasks
        LEFT JOIN users ON tasks.assigned_user_id = users.id
        WHERE (tasks.room_id = %s OR EXISTS (SELECT 1 FROM task_attachments WHERE task_attachments.task_id = tasks.id AND task_attachments.room_id = %s))
          AND (tasks.assigned_user_id = %s OR %s = 'admin')
        ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
        """,
        (room_id, room_id, session.get("user_id"), session.get("role"))
    ).fetchall()
    tasks = load_task_details(conn, tasks, room_id)
    room_inventory = fetch_inventory_items(conn, {"room_id": room_id}) if can_view_inventory() else []

    if request.method == "POST":
        file = request.files.get("photo") or request.files.get("photo_camera")
        audio = request.files.get("audio")
        wants_comment = bool(request.form.get("comment", "").strip())
        wants_photo = bool(file and file.filename)
        wants_audio = bool(audio and audio.filename)
        if wants_comment and not has_perm("write_comments"):
            flash("You do not have permission to write comments.")
            return redirect(url_for("room", room_id=room_id))
        if wants_photo and not has_perm("add_pictures"):
            flash("You do not have permission to add pictures.")
            return redirect(url_for("room", room_id=room_id))
        if wants_audio and not has_perm("add_audio"):
            flash("You do not have permission to add audio.")
            return redirect(url_for("room", room_id=room_id))

        photo_file = upload_file_to_storage(file) if wants_photo and allowed_photo(file.filename) else None
        audio_file = upload_file_to_storage(audio) if wants_audio and allowed_audio(audio.filename) else None
        conn.execute(
            "INSERT INTO notes (room_id, user_id, note_date, comment, photo_file, audio_file, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (room_id, session.get("user_id"), request.form["note_date"], request.form["comment"].strip(), photo_file, audio_file, datetime.now().isoformat())
        )
        conn.commit()
        notified = notify_admins_of_field_note(conn, project, room, request.form["comment"].strip(), photo_file, audio_file, request.form["note_date"])
        if notified:
            flash("Comment/photo added.")
        else:
            flash("Comment/photo added. Admin notification or email could not be sent.")

    selected_date = request.args.get("date", "")
    query = "SELECT notes.*, users.name AS user_name FROM notes LEFT JOIN users ON notes.user_id = users.id WHERE room_id = %s"
    params = [room_id]
    if selected_date:
        query += " AND note_date = %s"
        params.append(selected_date)
    query += " ORDER BY note_date DESC, created_at DESC"
    notes = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    return render_template("room.html", room=room, project=project, rooms=project_rooms, notes=notes, tasks=tasks, room_inventory=room_inventory, users=users, suppliers=suppliers, selected_date=selected_date, today=local_now().date().isoformat())


@app.route("/project/<int:project_id>/timeline")
@login_required
def project_timeline(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    selected_date = request.args.get("date", "")
    query = """
        SELECT notes.*, rooms.name AS room_name, rooms.category AS room_category, users.name AS user_name
        FROM notes
        JOIN rooms ON notes.room_id = rooms.id
        LEFT JOIN users ON notes.user_id = users.id
        WHERE rooms.project_id = %s
    """
    params = [project_id]
    if selected_date:
        query += " AND notes.note_date = %s"
        params.append(selected_date)
    query += " ORDER BY notes.note_date DESC, notes.created_at DESC"
    notes = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    return render_template("timeline.html", project=project, notes=notes, selected_date=selected_date)



@app.route("/project/<int:project_id>/delete", methods=["POST"])
@admin_required
def delete_project(project_id):
    conn = db()
    project = conn.execute("SELECT id, name FROM projects WHERE id = %s", (project_id,)).fetchone()
    admin = conn.execute("SELECT id, name, email FROM users WHERE id = %s AND role = 'admin'", (session.get("user_id"),)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))
    if not admin or not admin.get("email"):
        conn.close()
        flash("Your admin account needs an email before a delete PIN can be sent.")
        return redirect(url_for("project", project_id=project_id))

    pin = f"{secrets.randbelow(1000000):06d}"
    conn.execute("DELETE FROM project_delete_codes WHERE project_id = %s AND admin_id = %s", (project_id, admin["id"]))
    conn.execute(
        """
        INSERT INTO project_delete_codes (project_id, admin_id, pin_hash, expires_at, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (project_id, admin["id"], generate_password_hash(pin), utc_future_iso(10), utc_now_iso())
    )
    conn.commit()
    sent = send_email(
        admin["email"],
        "ProjectONus delete project PIN",
        "\n".join([
            f"Your 6-digit PIN to delete project '{project['name']}' is:",
            "",
            pin,
            "",
            "This PIN expires in 10 minutes.",
            "If you did not request this, ignore this email."
        ])
    )
    if not sent:
        conn.execute("DELETE FROM project_delete_codes WHERE project_id = %s AND admin_id = %s", (project_id, admin["id"]))
        conn.commit()
        conn.close()
        flash("Delete PIN could not be sent. Check SMTP email settings first.")
        return redirect(url_for("project", project_id=project_id))
    conn.close()
    flash("A 6-digit delete PIN was sent to your admin email.")
    return redirect(url_for("confirm_delete_project", project_id=project_id))


@app.route("/project/<int:project_id>/delete/confirm", methods=["GET", "POST"])
@admin_required
def confirm_delete_project(project_id):
    conn = db()
    project = conn.execute("SELECT id, name FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))

    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        code = conn.execute(
            """
            SELECT * FROM project_delete_codes
            WHERE project_id = %s AND admin_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id, session.get("user_id"))
        ).fetchone()
        expires_at = parse_iso_datetime(code.get("expires_at")) if code else None
        if not code or not expires_at or expires_at < datetime.now(timezone.utc):
            conn.close()
            flash("Delete PIN expired. Press Delete Project again to get a new PIN.")
            return redirect(url_for("project", project_id=project_id))
        if not check_password_hash(code["pin_hash"], pin):
            conn.close()
            flash("Invalid delete PIN.")
            return redirect(url_for("confirm_delete_project", project_id=project_id))

        conn.execute("DELETE FROM project_delete_codes WHERE project_id = %s", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        conn.commit()
        conn.close()
        flash("Project deleted.")
        return redirect(url_for("index"))

    conn.close()
    return render_template("delete_project_confirm.html", project=project)


@app.route("/room/<int:room_id>/delete", methods=["POST"])
@admin_required
def delete_room(room_id):
    conn = db()
    room = conn.execute(
        """
        SELECT rooms.id, rooms.name, rooms.project_id, projects.name AS project_name
        FROM rooms
        JOIN projects ON rooms.project_id = projects.id
        WHERE rooms.id = %s
        """,
        (room_id,)
    ).fetchone()
    admin = conn.execute("SELECT id, name, email FROM users WHERE id = %s AND role = 'admin'", (session.get("user_id"),)).fetchone()
    if not room:
        conn.close()
        flash("Room not found.")
        return redirect(url_for("index"))
    next_url = safe_next_url("project", project_id=room["project_id"])
    if not admin or not admin.get("email"):
        conn.close()
        flash("Your admin account needs an email before a delete PIN can be sent.")
        return redirect(next_url)

    pin = f"{secrets.randbelow(1000000):06d}"
    conn.execute("DELETE FROM room_delete_codes WHERE room_id = %s AND admin_id = %s", (room_id, admin["id"]))
    conn.execute(
        """
        INSERT INTO room_delete_codes (room_id, admin_id, pin_hash, expires_at, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (room_id, admin["id"], generate_password_hash(pin), utc_future_iso(10), utc_now_iso())
    )
    conn.commit()
    sent = send_email(
        admin["email"],
        "ProjectONus delete room PIN",
        "\n".join([
            f"Your 6-digit PIN to delete room '{room['name']}' is:",
            "",
            pin,
            "",
            f"Project: {room.get('project_name') or '-'}",
            "This PIN expires in 10 minutes.",
            "If you did not request this, ignore this email."
        ])
    )
    if not sent:
        conn.execute("DELETE FROM room_delete_codes WHERE room_id = %s AND admin_id = %s", (room_id, admin["id"]))
        conn.commit()
        conn.close()
        flash("Delete PIN could not be sent. Check SMTP email settings first.")
        return redirect(next_url)
    conn.close()
    flash("A 6-digit delete PIN was sent to your admin email.")
    return redirect(url_for("confirm_delete_room", room_id=room_id, next=next_url))


@app.route("/room/<int:room_id>/delete/confirm", methods=["GET", "POST"])
@admin_required
def confirm_delete_room(room_id):
    conn = db()
    room = conn.execute(
        """
        SELECT rooms.id, rooms.name, rooms.project_id, projects.name AS project_name
        FROM rooms
        JOIN projects ON rooms.project_id = projects.id
        WHERE rooms.id = %s
        """,
        (room_id,)
    ).fetchone()
    if not room:
        conn.close()
        flash("Room not found.")
        return redirect(url_for("index"))
    next_url = safe_next_url("project", project_id=room["project_id"])

    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        code = conn.execute(
            """
            SELECT * FROM room_delete_codes
            WHERE room_id = %s AND admin_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (room_id, session.get("user_id"))
        ).fetchone()
        expires_at = parse_iso_datetime(code.get("expires_at")) if code else None
        if not code or not expires_at or expires_at < datetime.now(timezone.utc):
            conn.close()
            flash("Delete PIN expired. Press Delete Room again to get a new PIN.")
            return redirect(next_url)
        if not check_password_hash(code["pin_hash"], pin):
            conn.close()
            flash("Invalid delete PIN.")
            return redirect(url_for("confirm_delete_room", room_id=room_id, next=next_url))

        conn.execute("DELETE FROM room_delete_codes WHERE room_id = %s", (room_id,))
        conn.execute("DELETE FROM rooms WHERE id = %s", (room_id,))
        conn.commit()
        conn.close()
        flash("Room deleted.")
        return redirect(next_url)

    conn.close()
    return render_template("delete_room_confirm.html", room=room, next_url=next_url)


@app.route("/note/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id):
    conn = db()
    note = conn.execute("SELECT notes.*, rooms.project_id FROM notes JOIN rooms ON notes.room_id = rooms.id WHERE notes.id = %s", (note_id,)).fetchone()
    if not note:
        conn.close()
        flash("Comment/photo not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, note["project_id"]):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))

    if not (is_main_admin() or has_perm("delete_comments") or has_perm("delete_pictures") or has_perm("delete_audio")):
        conn.close()
        flash("You do not have permission to delete this item.")
        return redirect(url_for("room", room_id=note["room_id"]))

    room_id = note["room_id"]
    conn.execute("DELETE FROM notes WHERE id = %s", (note_id,))
    conn.commit()
    conn.close()
    flash("Comment/photo deleted.")
    return redirect(url_for("room", room_id=room_id))


@app.route("/room/<int:room_id>/tasks", methods=["POST"])
@admin_required
def create_task(room_id):
    conn = db()
    room = conn.execute("SELECT * FROM rooms WHERE id = %s", (room_id,)).fetchone()
    if not room:
        conn.close()
        flash("Room not found.")
        return redirect(url_for("index"))

    assigned_user_id = request.form.get("assigned_user_id", type=int)
    assigned = conn.execute("SELECT id, name, email, phone_number, sms_enabled, role FROM users WHERE id = %s", (assigned_user_id,)).fetchone()
    title = request.form.get("title", "").strip()
    task_start_time = request.form.get("task_start_time", "").strip()
    if not assigned or not title or not task_start_time:
        conn.close()
        flash("Choose a user, enter a task title, and choose the be-there time.")
        return redirect(url_for("room", room_id=room_id))
    grant_project_access(conn, assigned_user_id, room["project_id"], assigned.get("role"))
    attachment_error, attachment_uploads, attachment_room_ids = collect_task_attachment_uploads(conn, room["project_id"], room_id)
    if attachment_error:
        conn.close()
        flash(attachment_error)
        return redirect(url_for("room", room_id=room_id))
    supplier, supplier_error = supplier_from_task_form(conn)
    if supplier_error:
        conn.close()
        flash(supplier_error)
        return redirect(url_for("room", room_id=room_id))
    supplier_inventory_item, supplier_inventory_error = create_supplier_inventory_item(conn, supplier, room["project_id"], room_id)
    if supplier_inventory_error:
        conn.close()
        flash(supplier_inventory_error)
        return redirect(url_for("room", room_id=room_id))
    task_date = request.form.get("task_date") or local_now().date().isoformat()
    task_instructions = request.form.get("instructions", "").strip()
    created_at = utc_now_iso()
    task_number = next_task_number(conn, created_at)

    task = conn.execute(
        """
        INSERT INTO tasks
        (task_number, project_id, room_id, assigned_user_id, created_by, task_date, task_start_date, task_start_time, task_end_date, title, instructions, task_photo_file, supplier_id, supplier_inventory_item_id, require_picture, allow_picture_upload, allow_comment, allow_audio, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            task_number,
            room["project_id"],
            room_id,
            assigned_user_id,
            session.get("user_id"),
            task_date,
            task_date,
            task_start_time,
            task_date,
            title,
            task_instructions,
            None,
            supplier["id"] if supplier else None,
            supplier_inventory_item["id"] if supplier_inventory_item else None,
            "require_picture" in request.form,
            "allow_picture_upload" in request.form,
            "allow_comment" in request.form,
            "allow_audio" in request.form,
            created_at
        )
    ).fetchone()
    inserted_attachments, first_photo, first_audio, saved_room_ids = insert_task_attachments(conn, task["id"], attachment_uploads)
    task = apply_task_legacy_media(conn, task, first_photo, first_audio)
    task["_attachments"] = inserted_attachments
    add_notification(
        conn,
        assigned["id"],
        assigned["name"],
        assigned["email"],
        assigned["role"],
        "task_assigned",
        task.get("project_id"),
        task.get("id"),
        f"New task assigned: {task_display_name(task)}. Be there {task_schedule_text(task)}. Project access granted."
    )
    conn.commit()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (room["project_id"],)).fetchone()
    send_task_assignment_email(task, assigned, project)
    send_task_assignment_sms(task, assigned, project)
    conn.close()
    flash("Task assigned, project access granted, and user notified.")
    return redirect(url_for("room", room_id=room_id))


@app.route("/tasks/create", methods=["GET", "POST"])
@admin_required
def create_global_task():
    conn = db()
    if request.method == "POST":
        project_id = request.form.get("project_id", type=int)
        supplier_mode = request.form.get("supplier_enabled") == "1"
        user_ids = []
        for value in request.form.getlist("user_ids"):
            try:
                user_ids.append(int(value))
            except Exception:
                pass
        title = request.form.get("title", "").strip()
        start_time = request.form.get("task_start_time", "").strip()
        if not project_id or not user_ids or (not supplier_mode and (not title or not start_time)):
            conn.close()
            flash("Choose a project, at least one worker, enter a task, and choose the be-there time.")
            return redirect(url_for("create_global_task"))

        project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
        selected_ids = set(user_ids)
        selected_users = [
            u for u in conn.execute("SELECT id, name, email, phone_number, sms_enabled, role FROM users WHERE role <> 'admin' ORDER BY name").fetchall()
            if u["id"] in selected_ids
        ]
        if not project or not selected_users:
            conn.close()
            flash("Project or workers not found.")
            return redirect(url_for("create_global_task"))

        requested_room_id = request.form.get("room_id", "")
        room_id = project_room_id_or_none(conn, project_id, requested_room_id)
        if requested_room_id and not room_id:
            conn.close()
            flash("Choose a room that belongs to this project.")
            return redirect(url_for("create_global_task"))
        attachment_error, attachment_uploads, attachment_room_ids = collect_task_attachment_uploads(conn, project_id, room_id)
        if attachment_error:
            conn.close()
            flash(attachment_error)
            return redirect(url_for("create_global_task"))
        supplier, supplier_error = supplier_from_task_form(conn)
        if supplier_error:
            conn.close()
            flash(supplier_error)
            return redirect(url_for("create_global_task"))
        supplier_inventory_items, supplier_inventory_error = supplier_items_from_task_form(conn, supplier)
        if supplier_inventory_error:
            conn.close()
            flash(supplier_inventory_error)
            return redirect(url_for("create_global_task"))
        if supplier_inventory_items:
            project_id = supplier_inventory_items[0].get("project_id") or project_id
            room_id = supplier_inventory_items[0].get("room_id")
        if supplier_mode and supplier_inventory_items:
            title = f"Supplier pickup - {supplier.get('name') or 'Supplier'}"
            start_date = supplier_inventory_items[0].get("item_date") or local_now().date().isoformat()
            start_time = supplier_inventory_items[0].get("supplier_pickup_time") or "08:00"
        else:
            start_date = request.form.get("task_start_date") or datetime.now().date().isoformat()
        end_date = request.form.get("task_end_date") or start_date
        task_instructions = request.form.get("instructions", "").strip()
        created_tasks = []

        for assigned in selected_users:
            grant_project_access(conn, assigned["id"], project_id, assigned.get("role"))
            created_at = utc_now_iso()
            task_number = next_task_number(conn, created_at)
            task = conn.execute(
                """
                INSERT INTO tasks
                (task_number, project_id, room_id, assigned_user_id, created_by, task_date, task_start_date, task_start_time, task_end_date, title, instructions, task_photo_file, task_audio_file, supplier_id, supplier_inventory_item_id, require_picture, allow_picture_upload, allow_comment, allow_audio, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    task_number,
                    project_id,
                    room_id,
                    assigned["id"],
                    session.get("user_id"),
                    start_date,
                    start_date,
                    start_time,
                    end_date,
                    title,
                    task_instructions,
                    None,
                    None,
                    supplier["id"] if supplier else None,
                    supplier_inventory_items[0]["id"] if supplier_inventory_items else None,
                    "require_picture" in request.form,
                    "allow_picture_upload" in request.form,
                    "allow_comment" in request.form,
                    "allow_audio" in request.form,
                    created_at
                )
            ).fetchone()
            inserted_attachments, first_photo, first_audio, saved_room_ids = insert_task_attachments(conn, task["id"], attachment_uploads)
            link_supplier_items_to_task(conn, task["id"], supplier_inventory_items)
            task = apply_task_legacy_media(conn, task, first_photo, first_audio)
            task["_attachments"] = inserted_attachments
            add_notification(
                conn,
                assigned["id"],
                assigned["name"],
                assigned["email"],
                assigned["role"],
                "task_assigned",
                task.get("project_id"),
                task.get("id"),
                f"New task assigned: {task_display_name(task)}. Be there {task_schedule_text(task)}. Project access granted."
            )
            created_tasks.append((task, assigned))

        conn.commit()
        for task, assigned in created_tasks:
            send_task_assignment_email(task, assigned, project)
            send_task_assignment_sms(task, assigned, project)
        conn.close()
        flash(f"Task sent to {len(created_tasks)} worker(s). Project access was granted.")
        return redirect(url_for("my_tasks"))

    projects = conn.execute("SELECT id, name, customer_name FROM projects ORDER BY name").fetchall()
    rooms = conn.execute(
        """
        SELECT rooms.id, rooms.name, rooms.project_id, projects.name AS project_name
        FROM rooms
        JOIN projects ON rooms.project_id = projects.id
        ORDER BY projects.name, rooms.name
        """
    ).fetchall()
    users = conn.execute("SELECT id, name, email, phone_number, sms_enabled, role FROM users WHERE role <> 'admin' ORDER BY name").fetchall()
    suppliers = fetch_suppliers(conn)
    conn.close()
    return render_template("create_task.html", projects=projects, users=users, rooms=rooms, suppliers=suppliers, today=local_now().date().isoformat())


@app.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_task(task_id):
    conn = db()
    task = conn.execute(
        """
        SELECT tasks.*, projects.name AS project_name, rooms.name AS room_name
        FROM tasks
        JOIN projects ON tasks.project_id = projects.id
        LEFT JOIN rooms ON tasks.room_id = rooms.id
        WHERE tasks.id = %s
        """,
        (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        flash("Task not found.")
        return redirect(url_for("my_tasks"))
    next_url = safe_next_url("my_tasks", project_id=task["project_id"])
    users = conn.execute("SELECT id, name, email, phone_number, sms_enabled, role FROM users WHERE role <> 'admin' ORDER BY name").fetchall()
    rooms = conn.execute("SELECT id, name, project_id FROM rooms WHERE project_id = %s ORDER BY name", (task["project_id"],)).fetchall()

    if request.method == "POST":
        assigned_user_id = request.form.get("assigned_user_id", type=int)
        assigned = None
        for user in users:
            if user["id"] == assigned_user_id:
                assigned = user
                break
        title = request.form.get("title", "").strip()
        start_date = request.form.get("task_start_date") or request.form.get("task_date") or task.get("task_start_date") or task.get("task_date") or local_now().date().isoformat()
        start_time = request.form.get("task_start_time", "").strip()
        end_date = request.form.get("task_end_date") or start_date
        if not assigned or not title or not start_time:
            flash("Choose a worker, enter a task title, and choose the be-there time.")
            conn.close()
            return redirect(url_for("edit_task", task_id=task_id, next=next_url))

        requested_room_id = request.form.get("room_id", "")
        room_id = project_room_id_or_none(conn, task["project_id"], requested_room_id)
        if requested_room_id and not room_id:
            conn.close()
            flash("Choose a room that belongs to this project.")
            return redirect(url_for("edit_task", task_id=task_id, next=next_url))
        attachment_error, attachment_uploads, attachment_room_ids = collect_task_attachment_uploads(conn, task["project_id"], room_id)
        if attachment_error:
            conn.close()
            flash(attachment_error)
            return redirect(url_for("edit_task", task_id=task_id, next=next_url))

        assigned_changed = assigned_user_id != task.get("assigned_user_id")
        reset_received = assigned_changed and task.get("status") != "done"
        grant_project_access(conn, assigned_user_id, task["project_id"], assigned.get("role"))
        conn.execute(
            """
            UPDATE tasks
            SET assigned_user_id = %s,
                room_id = %s,
                task_date = %s,
                task_start_date = %s,
                task_start_time = %s,
                task_end_date = %s,
                title = %s,
                instructions = %s,
                require_picture = %s,
                allow_picture_upload = %s,
                allow_comment = %s,
                allow_audio = %s,
                accepted_at = CASE WHEN %s THEN NULL ELSE accepted_at END
            WHERE id = %s
            """,
            (
                assigned_user_id,
                room_id,
                start_date,
                start_date,
                start_time,
                end_date,
                title,
                request.form.get("instructions", "").strip(),
                "require_picture" in request.form,
                "allow_picture_upload" in request.form,
                "allow_comment" in request.form,
                "allow_audio" in request.form,
                reset_received,
                task_id
            )
        )
        if reset_received:
            conn.execute(
                "UPDATE login_events SET is_read = TRUE WHERE task_id = %s AND event_type = 'task_assigned'",
                (task_id,)
            )
        updated_task = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
        inserted_attachments, first_photo, first_audio, saved_room_ids = insert_task_attachments(conn, task_id, attachment_uploads)
        updated_task = apply_task_legacy_media(conn, updated_task, first_photo, first_audio)
        updated_task = task_with_attachments_for_email(conn, updated_task)
        add_notification(
            conn,
            assigned["id"],
            assigned["name"],
            assigned["email"],
            assigned["role"],
            "task_assigned",
            updated_task.get("project_id"),
            updated_task.get("id"),
            f"Task updated: {task_display_name(updated_task)}. Be there {task_schedule_text(updated_task)}."
        )
        conn.commit()
        project = conn.execute("SELECT * FROM projects WHERE id = %s", (task["project_id"],)).fetchone()
        send_task_assignment_email(updated_task, assigned, project)
        send_task_assignment_sms(updated_task, assigned, project)
        conn.close()
        flash("Task updated and worker notified.")
        return redirect(next_url)

    task = load_task_details(conn, [task])[0]
    conn.close()
    return render_template("edit_task.html", task=task, users=users, rooms=rooms, next_url=next_url)


@app.route("/tasks/<int:task_id>/room-status/<int:room_id>", methods=["POST"])
@login_required
def update_task_room_status(task_id, room_id):
    conn = db()
    task = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    if not task:
        conn.close()
        flash("Task not found.")
        return redirect(url_for("my_tasks"))
    if not user_can_access_project(conn, task["project_id"]):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    if not (is_main_admin() or task.get("assigned_user_id") == session.get("user_id")):
        conn.close()
        flash("This task is assigned to another user.")
        return redirect(url_for("my_tasks"))
    room = conn.execute(
        "SELECT id FROM rooms WHERE id = %s AND project_id = %s",
        (room_id, task["project_id"])
    ).fetchone()
    if not room:
        conn.close()
        flash("Room not found for this task.")
        return redirect(safe_next_url("my_tasks", project_id=task["project_id"]))
    is_done = request.form.get("is_done") == "1"
    conn.execute(
        """
        INSERT INTO task_room_statuses (task_id, room_id, is_done, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (task_id, room_id) DO UPDATE SET
            is_done = EXCLUDED.is_done,
            updated_by = EXCLUDED.updated_by,
            updated_at = EXCLUDED.updated_at
        """,
        (task_id, room_id, is_done, session.get("user_id"), utc_now_iso())
    )
    conn.commit()
    conn.close()
    flash("Task room checklist updated.")
    return redirect(safe_next_url("my_tasks", project_id=task["project_id"]))


@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
@login_required
def complete_task(task_id):
    conn = db()
    task = conn.execute(
        """
        SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, users.name AS assigned_user_name
        FROM tasks
        LEFT JOIN rooms ON tasks.room_id = rooms.id
        JOIN projects ON tasks.project_id = projects.id
        LEFT JOIN users ON tasks.assigned_user_id = users.id
        WHERE tasks.id = %s
        """,
        (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        flash("Task not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, task["project_id"]):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    if not (is_main_admin() or task["assigned_user_id"] == session.get("user_id")):
        conn.close()
        flash("This task is assigned to another user.")
        if task.get("room_id"):
            return redirect(url_for("room", room_id=task["room_id"]))
        return redirect(url_for("my_tasks"))

    completion_room_id = project_room_id_or_none(conn, task["project_id"], request.form.get("completion_room_id"))
    if request.form.get("completion_room_id") and not completion_room_id:
        conn.close()
        flash("Choose a room that belongs to this project.")
        return redirect(safe_next_url("my_tasks", project_id=task["project_id"]))
    upload_error, completion_uploads = collect_completion_uploads(conn, task["project_id"], completion_room_id)
    if upload_error:
        conn.close()
        flash(upload_error)
        return redirect(safe_next_url("my_tasks", project_id=task["project_id"]))
    wants_photo = any(item.get("file_type") == "photo" for item in completion_uploads)
    if task.get("require_picture") and not wants_photo and not task.get("completion_photo_file"):
        conn.close()
        flash("This task requires a picture before it can be completed.")
        if task.get("room_id"):
            return redirect(url_for("room", room_id=task["room_id"]))
        return redirect(url_for("my_tasks"))

    inserted_attachments, first_photo, first_audio, saved_room_ids = insert_task_attachments(conn, task_id, completion_uploads)
    photo_file = first_photo or task.get("completion_photo_file")
    audio_file = first_audio or task.get("completion_audio_file")
    completed_at = datetime.now().isoformat()
    mark_entire_task_done = True
    if completion_room_id:
        conn.execute(
            """
            INSERT INTO task_room_statuses (task_id, room_id, is_done, updated_by, updated_at)
            VALUES (%s, %s, TRUE, %s, %s)
            ON CONFLICT (task_id, room_id) DO UPDATE SET
                is_done = TRUE,
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at
            """,
            (task_id, completion_room_id, session.get("user_id"), utc_now_iso())
        )
        related_room_ids = task_related_room_ids(conn, task_id, task)
        related_room_ids.add(completion_room_id)
        mark_entire_task_done = all_task_rooms_done(conn, task_id, related_room_ids)
    update_fields = [
        "completion_comment = %s",
        "completion_photo_file = %s",
        "completion_audio_file = %s",
    ]
    params = [
        request.form.get("completion_comment", "").strip(),
        photo_file,
        audio_file,
    ]
    if mark_entire_task_done:
        update_fields.extend(["status = 'done'", "completed_at = %s"])
        params.append(completed_at)
    params.append(task_id)
    conn.execute(
        f"UPDATE tasks SET {', '.join(update_fields)} WHERE id = %s",
        tuple(params)
    )
    if task.get("supplier_id") and mark_entire_task_done:
        conn.execute(
            """
            UPDATE inventory_items
            SET status = 'available',
                purchased_by = COALESCE(purchased_by, %s),
                purchased_at = COALESCE(purchased_at, %s),
                updated_at = %s
            WHERE id IN (
                SELECT inventory_item_id FROM task_supplier_items WHERE task_id = %s
                UNION
                SELECT supplier_inventory_item_id FROM tasks WHERE id = %s AND supplier_inventory_item_id IS NOT NULL
            )
              AND status = 'needs_purchase'
            """,
            (session.get("user_id"), utc_now_iso(), utc_now_iso(), task_id, task_id)
        )
    conn.commit()
    notification_ok = True
    try:
        add_notification(
            conn,
            session.get("user_id"),
            session.get("name"),
            "",
            session.get("role"),
            "task_completed",
            task.get("project_id"),
            task.get("id"),
            f"Task completed: {task_display_name(task)}"
        )
        conn.commit()
    except Exception as e:
        print("Task completion notification failed:", e)
        conn.rollback()
        notification_ok = False
    conn.close()
    if notification_ok:
        flash("Task marked done. Admin was notified." if mark_entire_task_done else "Room marked done. Admin was notified.")
    else:
        flash("Task updated. Admin notification could not be sent.")
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    if task.get("room_id") and "/mobile/" in (request.referrer or ""):
        return redirect(url_for("mobile_room", room_id=task["room_id"]))
    if task.get("room_id"):
        return redirect(url_for("room", room_id=task["room_id"]))
    return redirect(url_for("my_tasks"))


@app.route("/tasks/<int:task_id>/received", methods=["POST"])
@login_required
def receive_task(task_id):
    conn = db()
    task = conn.execute(
        """
        SELECT tasks.*, projects.name AS project_name, projects.customer_address AS project_address, users.name AS assigned_user_name
        FROM tasks
        JOIN projects ON tasks.project_id = projects.id
        LEFT JOIN users ON tasks.assigned_user_id = users.id
        WHERE tasks.id = %s
        """,
        (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        flash("Task not found.")
        return redirect(url_for("my_tasks"))
    if not (is_main_admin() or task["assigned_user_id"] == session.get("user_id")):
        conn.close()
        flash("This task is assigned to another user.")
        return redirect(url_for("my_tasks"))
    if not user_can_access_project(conn, task["project_id"]):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("my_tasks"))
    if task.get("accepted_at"):
        conn.close()
        flash("Task was already marked received.")
        return redirect(url_for("my_tasks"))

    accepted_at = utc_now_iso()
    conn.execute("UPDATE tasks SET accepted_at = %s WHERE id = %s", (accepted_at, task_id))
    conn.execute(
        """
        UPDATE login_events
        SET is_read = TRUE
        WHERE user_id = %s AND task_id = %s AND event_type = 'task_assigned'
        """,
        (session.get("user_id"), task_id)
    )
    task["accepted_at"] = accepted_at
    actor = conn.execute("SELECT id, name, email, role FROM users WHERE id = %s", (session.get("user_id"),)).fetchone() or {}
    notify_admins_task_received(conn, task, actor)
    conn.close()
    flash("Task marked received. Admin was notified.")
    calendar_args = {"calendar_task": task_id} if not is_main_admin() else {}
    if task.get("room_id") and "/mobile/" in (request.referrer or ""):
        return redirect(url_for("mobile_room", room_id=task["room_id"], **calendar_args))
    if task.get("room_id"):
        return redirect(url_for("room", room_id=task["room_id"], **calendar_args))
    return redirect(url_for("my_tasks", **calendar_args))


@app.route("/tasks/<int:task_id>/calendar.ics")
@login_required
def task_calendar_file(task_id):
    conn = db()
    task = conn.execute(
        """
        SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, projects.customer_address AS project_address
        FROM tasks
        LEFT JOIN rooms ON tasks.room_id = rooms.id
        JOIN projects ON tasks.project_id = projects.id
        WHERE tasks.id = %s
        """,
        (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        return Response("Task not found.", status=404)
    if not (is_main_admin() or task["assigned_user_id"] == session.get("user_id")):
        conn.close()
        return Response("This task is assigned to another user.", status=403)
    if not user_can_access_project(conn, task["project_id"]):
        conn.close()
        return Response("You do not have access to this project.", status=403)
    conn.close()

    filename = secure_filename(f"ProjectONus_{task_display_name(task)}.ics") or "ProjectONus_task.ics"
    if not filename.lower().endswith(".ics"):
        filename += ".ics"
    return Response(
        task_calendar_ics(task),
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.route("/tasks")
@login_required
def my_tasks():
    conn = db()
    selected_project_id = request.args.get("project_id", type=int)
    selected_room_id = request.args.get("room_id", type=int)
    selected_supplier_id = request.args.get("supplier_id", type=int)
    selected_user_id = request.args.get("user_id", type=int)
    task_mode = request.args.get("mode", "")
    if (selected_project_id or selected_room_id or selected_supplier_id or selected_user_id) and not task_mode:
        task_mode = "search"
    has_filter_selection = bool(selected_project_id or selected_room_id or selected_supplier_id or selected_user_id)
    task_period = request.args.get("period", "day")
    if task_period not in ["day", "week", "month"]:
        task_period = "day"
    task_date_arg = request.args.get("date")
    task_date = task_date_arg or local_now().date().isoformat()
    task_date_filter = bool(task_date_arg) or (task_mode == "search" and has_filter_selection)
    projects = []
    project_rooms = []
    suppliers = []
    task_users = []
    if selected_project_id:
        if is_main_admin():
            project_rooms = conn.execute(
                "SELECT id, name FROM rooms WHERE project_id = %s ORDER BY name, id",
                (selected_project_id,)
            ).fetchall()
        else:
            project_rooms = conn.execute(
                """
                SELECT rooms.id, rooms.name
                FROM rooms
                JOIN project_permissions ON project_permissions.project_id = rooms.project_id AND project_permissions.user_id = %s
                WHERE rooms.project_id = %s
                ORDER BY rooms.name, rooms.id
                """,
                (session.get("user_id"), selected_project_id)
            ).fetchall()
        if selected_room_id and not any(r["id"] == selected_room_id for r in project_rooms):
            selected_room_id = None
    if is_main_admin():
        projects = conn.execute("SELECT id, name, customer_name FROM projects ORDER BY name").fetchall()
        suppliers = fetch_suppliers(conn)
        task_users = conn.execute("SELECT id, name, email FROM users WHERE role <> 'admin' ORDER BY name").fetchall()
        should_show_search = selected_project_id or selected_room_id or selected_supplier_id or selected_user_id or task_date_filter
        apply_task_date_filter = task_mode == "search" and should_show_search
        if task_mode == "search" and should_show_search:
            where = []
            params = []
            if selected_project_id:
                where.append("tasks.project_id = %s")
                params.append(selected_project_id)
            if selected_supplier_id:
                where.append("tasks.supplier_id = %s")
                params.append(selected_supplier_id)
            if selected_room_id:
                where.append("(tasks.room_id = %s OR EXISTS (SELECT 1 FROM task_attachments WHERE task_attachments.task_id = tasks.id AND task_attachments.room_id = %s))")
                params.extend([selected_room_id, selected_room_id])
            if selected_user_id:
                where.append("tasks.assigned_user_id = %s")
                params.append(selected_user_id)
            where_sql = " AND ".join(where) if where else "1=1"
            tasks = conn.execute(
                f"""
                SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, projects.customer_address AS project_address, users.name AS assigned_user_name, users.email AS assigned_user_email
                FROM tasks
                LEFT JOIN rooms ON tasks.room_id = rooms.id
                LEFT JOIN projects ON tasks.project_id = projects.id
                LEFT JOIN users ON tasks.assigned_user_id = users.id
                WHERE {where_sql}
                ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
                """,
                tuple(params)
            ).fetchall()
            if apply_task_date_filter:
                tasks = [t for t in tasks if task_scheduled_in_range(t, task_period, task_date)]
        else:
            tasks = []
    else:
        projects = conn.execute(
            """
            SELECT projects.id, projects.name, projects.customer_name
            FROM projects
            JOIN project_permissions ON project_permissions.project_id = projects.id AND project_permissions.user_id = %s
            ORDER BY projects.name
            """,
            (session.get("user_id"),)
        ).fetchall()
        suppliers = conn.execute(
            """
            SELECT DISTINCT suppliers.*
            FROM suppliers
            JOIN tasks ON tasks.supplier_id = suppliers.id
            JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
            WHERE tasks.assigned_user_id = %s
            ORDER BY suppliers.name
            """,
            (session.get("user_id"), session.get("user_id"))
        ).fetchall()
        should_show_search = selected_project_id or selected_supplier_id or task_date_filter
        apply_task_date_filter = task_mode == "search" and should_show_search
        if task_mode == "search" and should_show_search:
            where = ["tasks.assigned_user_id = %s"]
            params = [session.get("user_id")]
            if selected_project_id:
                where.append("tasks.project_id = %s")
                params.append(selected_project_id)
            if selected_supplier_id:
                where.append("tasks.supplier_id = %s")
                params.append(selected_supplier_id)
            if selected_room_id:
                where.append("(tasks.room_id = %s OR EXISTS (SELECT 1 FROM task_attachments WHERE task_attachments.task_id = tasks.id AND task_attachments.room_id = %s))")
                params.extend([selected_room_id, selected_room_id])
            tasks = conn.execute(
                """
                SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, projects.customer_address AS project_address, users.name AS assigned_user_name
                FROM tasks
                LEFT JOIN rooms ON tasks.room_id = rooms.id
                LEFT JOIN projects ON tasks.project_id = projects.id
                LEFT JOIN users ON tasks.assigned_user_id = users.id
                JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
                WHERE """ + " AND ".join(where) + """
                ORDER BY tasks.task_date DESC, tasks.created_at DESC
                """,
                tuple([session.get("user_id")] + params)
            ).fetchall()
            if apply_task_date_filter:
                tasks = [t for t in tasks if task_scheduled_in_range(t, task_period, task_date)]
        elif task_mode == "search":
            tasks = []
        else:
            tasks = conn.execute(
                """
                SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, projects.customer_address AS project_address, users.name AS assigned_user_name
                FROM tasks
                LEFT JOIN rooms ON tasks.room_id = rooms.id
                LEFT JOIN projects ON tasks.project_id = projects.id
                LEFT JOIN users ON tasks.assigned_user_id = users.id
                JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
                WHERE tasks.assigned_user_id = %s AND tasks.status <> 'done'
                ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
                """,
                (session.get("user_id"), session.get("user_id"))
            ).fetchall()
    tasks = load_task_details(conn, tasks, selected_room_id)
    tasks_by_room = {}
    if task_mode == "search" and selected_project_id:
        for room in project_rooms:
            room_tasks = []
            for task in tasks:
                status_rooms = [status.get("room_id") for status in task.get("_room_statuses", [])]
                if task.get("room_id") == room["id"] or room["id"] in status_rooms:
                    room_tasks.append(task)
            tasks_by_room[room["id"]] = room_tasks
    conn.close()
    return render_template(
        "tasks.html",
        tasks=tasks,
        projects=projects,
        task_users=task_users,
        suppliers=suppliers,
        selected_project_id=selected_project_id,
        selected_room_id=selected_room_id,
        selected_supplier_id=selected_supplier_id,
        selected_user_id=selected_user_id,
        project_rooms=project_rooms,
        tasks_by_room=tasks_by_room,
        task_mode=task_mode,
        task_period=task_period,
        task_date=task_date,
        task_date_filter=task_date_filter
    )


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
@admin_required
def delete_task(task_id):
    conn = db()
    task = conn.execute(
        """
        SELECT tasks.id, tasks.task_number, tasks.title, tasks.project_id, tasks.accepted_at, projects.name AS project_name
        FROM tasks
        LEFT JOIN projects ON tasks.project_id = projects.id
        WHERE tasks.id = %s
        """,
        (task_id,)
    ).fetchone()
    admin = conn.execute("SELECT id, name, email FROM users WHERE id = %s AND role = 'admin'", (session.get("user_id"),)).fetchone()
    if not task:
        conn.close()
        flash("Task not found.")
        return redirect(url_for("my_tasks"))
    next_url = safe_next_url("my_tasks", project_id=task["project_id"])
    if not task.get("accepted_at"):
        conn.execute("DELETE FROM login_events WHERE task_id = %s", (task_id,))
        conn.execute("DELETE FROM task_delete_codes WHERE task_id = %s", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        conn.commit()
        conn.close()
        flash("Task deleted.")
        return redirect(next_url)
    if is_mobile_request():
        conn.close()
        flash("This task was already received by the worker. Delete it from the desktop version with an email PIN.")
        return redirect(next_url)
    if not admin or not admin.get("email"):
        conn.close()
        flash("Your admin account needs an email before a delete PIN can be sent.")
        return redirect(next_url)

    pin = f"{secrets.randbelow(1000000):06d}"
    conn.execute("DELETE FROM task_delete_codes WHERE task_id = %s AND admin_id = %s", (task_id, admin["id"]))
    conn.execute(
        """
        INSERT INTO task_delete_codes (task_id, admin_id, pin_hash, expires_at, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (task_id, admin["id"], generate_password_hash(pin), utc_future_iso(10), utc_now_iso())
    )
    conn.commit()
    sent = send_email(
        admin["email"],
        "ProjectONus delete task PIN",
        "\n".join([
            f"Your 6-digit PIN to delete task '{task_display_name(task)}' is:",
            "",
            pin,
            "",
            f"Project: {task.get('project_name') or '-'}",
            "This PIN expires in 10 minutes.",
            "If you did not request this, ignore this email."
        ])
    )
    if not sent:
        conn.execute("DELETE FROM task_delete_codes WHERE task_id = %s AND admin_id = %s", (task_id, admin["id"]))
        conn.commit()
        conn.close()
        flash("Delete PIN could not be sent. Check SMTP email settings first.")
        return redirect(next_url)
    conn.close()
    flash("A 6-digit delete PIN was sent to your admin email.")
    return redirect(url_for("confirm_delete_task", task_id=task_id, next=next_url))


@app.route("/tasks/<int:task_id>/delete/confirm", methods=["GET", "POST"])
@admin_required
def confirm_delete_task(task_id):
    conn = db()
    task = conn.execute(
        """
        SELECT tasks.id, tasks.task_number, tasks.title, tasks.project_id, tasks.accepted_at, projects.name AS project_name
        FROM tasks
        LEFT JOIN projects ON tasks.project_id = projects.id
        WHERE tasks.id = %s
        """,
        (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        flash("Task not found.")
        return redirect(url_for("my_tasks"))
    next_url = safe_next_url("my_tasks", project_id=task["project_id"])
    if is_mobile_request():
        conn.close()
        flash("This task was already received by the worker. Delete it from the desktop version with an email PIN.")
        return redirect(next_url)

    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        code = conn.execute(
            """
            SELECT * FROM task_delete_codes
            WHERE task_id = %s AND admin_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (task_id, session.get("user_id"))
        ).fetchone()
        expires_at = parse_iso_datetime(code.get("expires_at")) if code else None
        if not code or not expires_at or expires_at < datetime.now(timezone.utc):
            conn.close()
            flash("Delete PIN expired. Press Delete Task again to get a new PIN.")
            return redirect(next_url)
        if not check_password_hash(code["pin_hash"], pin):
            conn.close()
            flash("Invalid delete PIN.")
            return redirect(url_for("confirm_delete_task", task_id=task_id, next=next_url))

        conn.execute("DELETE FROM login_events WHERE task_id = %s", (task_id,))
        conn.execute("DELETE FROM task_delete_codes WHERE task_id = %s", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        conn.commit()
        conn.close()
        flash("Task deleted.")
        return redirect(next_url)

    conn.close()
    return render_template("delete_task_confirm.html", task=task, next_url=next_url)


def task_report_status(task):
    if task.get("status") == "done":
        return "Done"
    if task.get("accepted_at"):
        return "In Progress"
    return "Not Seen"


def task_in_report_range(task, period, selected_date):
    period, start, end = attendance_range(period, selected_date)
    task_date = local_date_text(task.get("task_start_date") or task.get("task_date"))
    if not task_date:
        return False
    try:
        scheduled = datetime.strptime(task_date, "%m/%d/%Y").replace(tzinfo=start.tzinfo)
    except Exception:
        return False
    return start <= scheduled < end


def task_report_data(period, selected_date, selected_project_id=None, selected_user_id=None):
    period, start, end = attendance_range(period, selected_date)
    conn = db()
    projects = conn.execute("SELECT id, name, customer_name FROM projects ORDER BY name").fetchall()
    users = conn.execute("SELECT id, name, email, role FROM users WHERE role <> 'admin' ORDER BY name").fetchall()
    query = """
        SELECT tasks.*,
               projects.name AS project_name,
               rooms.name AS room_name,
               assigned.name AS assigned_user_name,
               assigned.email AS assigned_user_email,
               creator.name AS created_by_name
        FROM tasks
        LEFT JOIN projects ON tasks.project_id = projects.id
        LEFT JOIN rooms ON tasks.room_id = rooms.id
        LEFT JOIN users assigned ON tasks.assigned_user_id = assigned.id
        LEFT JOIN users creator ON tasks.created_by = creator.id
        WHERE COALESCE(tasks.task_start_date, tasks.task_date) >= %s
          AND COALESCE(tasks.task_start_date, tasks.task_date) < %s
    """
    params = [
        (start - timedelta(days=1)).date().isoformat(),
        (end + timedelta(days=1)).date().isoformat()
    ]
    if selected_project_id:
        query += " AND tasks.project_id = %s"
        params.append(selected_project_id)
    if selected_user_id:
        query += " AND tasks.assigned_user_id = %s"
        params.append(selected_user_id)
    query += " ORDER BY projects.name, tasks.task_number DESC NULLS LAST, tasks.created_at DESC, tasks.id DESC"
    tasks = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    tasks = [t for t in tasks if task_in_report_range(t, period, selected_date)]
    return {
        "period": period,
        "start": start,
        "end": end,
        "projects": projects,
        "users": users,
        "tasks": tasks
    }


@app.route("/tasks/report")
@admin_required
def task_report():
    period = request.args.get("period", "day")
    selected_date = request.args.get("date") or local_now().date().isoformat()
    selected_project_id = request.args.get("project_id", type=int)
    selected_user_id = request.args.get("user_id", type=int)
    report = task_report_data(period, selected_date, selected_project_id, selected_user_id)
    return render_template(
        "task_report.html",
        report=report,
        period=report["period"],
        selected_date=selected_date,
        selected_project_id=selected_project_id,
        selected_user_id=selected_user_id,
        task_report_status=task_report_status
    )


@app.route("/tasks/report/export")
@admin_required
def task_report_export():
    period = request.args.get("period", "day")
    selected_date = request.args.get("date") or local_now().date().isoformat()
    selected_project_id = request.args.get("project_id", type=int)
    selected_user_id = request.args.get("user_id", type=int)
    report = task_report_data(period, selected_date, selected_project_id, selected_user_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Task #", "Project", "Room", "Task", "Assigned Worker", "Worker Email", "Created By",
        "Created Date", "Scheduled Date", "Be There Time", "End Date", "Seen By Worker", "Received At",
        "Done", "Completed At", "Status", "Instructions"
    ])
    for task in report["tasks"]:
        writer.writerow([
            task.get("task_number") or "",
            task.get("project_name") or "",
            task.get("room_name") or "",
            task.get("title") or "",
            task.get("assigned_user_name") or "",
            task.get("assigned_user_email") or "",
            task.get("created_by_name") or "",
            format_datetime(task.get("created_at")),
            format_date(task.get("task_start_date") or task.get("task_date")),
            format_task_time(task.get("task_start_time")),
            format_date(task.get("task_end_date") or task.get("task_date")),
            "Yes" if task.get("accepted_at") else "No",
            format_datetime(task.get("accepted_at")) if task.get("accepted_at") else "",
            "Yes" if task.get("status") == "done" else "No",
            format_datetime(task.get("completed_at")) if task.get("completed_at") else "",
            task_report_status(task),
            task_instruction_text(task)
        ])
    filename = f"projectonus_task_report_{report['period']}_{selected_date}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/team-map")
@admin_required
def team_map():
    try:
        return render_template("team_map.html")
    except Exception as e:
        print("Team map page failed:", e)
        try:
            return render_template("team_map_fallback.html", team_map_error=str(e))
        except Exception as fallback_error:
            print("Team map fallback failed:", fallback_error)
            return Response(
                """
                <!doctype html>
                <html>
                <head>
                    <title>Where Is My Team - ProjectONus</title>
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    <style>
                        body{font-family:Arial,sans-serif;margin:0;background:#eef3f8;color:#172033}
                        nav{position:fixed;inset:0 auto 0 0;width:264px;background:#102137;color:white;padding:22px 16px}
                        nav strong{display:block;font-size:24px;margin-bottom:18px}
                        nav a{display:block;color:#dbe7f6;text-decoration:none;font-weight:700;padding:10px 12px;border-radius:7px}
                        nav a:hover{background:#183657;color:white}
                        main{margin-left:264px;padding:24px 30px}
                        .card{background:white;border:1px solid #d6dee9;border-radius:8px;padding:18px;box-shadow:0 1px 3px rgba(16,33,55,.08)}
                        .btn{display:inline-block;background:#0b73b9;color:white;text-decoration:none;font-weight:800;padding:10px 14px;border-radius:6px;margin-right:8px}
                        .muted{color:#687689}
                    </style>
                </head>
                <body>
                    <nav>
                        <strong>ProjectONus</strong>
                        <a href="/">Home</a>
                        <a href="/tasks">Tasks</a>
                        <a href="/notifications">Notifications</a>
                        <a href="/users">Users</a>
                        <a href="/settings">Settings</a>
                        <a href="/attendance/report">Time Report</a>
                        <a href="/tasks/report">Task Report</a>
                        <a href="/team-map">Where Is My Team</a>
                        <a href="/backup">Backup</a>
                    </nav>
                    <main>
                        <div class="card">
                            <h1>Where Is My Team</h1>
                            <p>The team map could not load, but the navigation is still available.</p>
                            <p class="muted">Please check the Render logs for the printed team map error.</p>
                            <a class="btn" href="/team-map/data">Open Team Data</a>
                            <a class="btn" href="/">Home</a>
                        </div>
                    </main>
                </body>
                </html>
                """,
                mimetype="text/html"
            )


@app.route("/team-map/data")
@admin_required
def team_map_data():
    conn = None
    try:
        conn = db()
        workers = active_worker_locations(conn)
        return {"workers": workers, "updated_at": format_datetime(utc_now_iso()), "error": ""}
    except Exception as e:
        print("Team map data failed:", e)
        return {
            "workers": [],
            "updated_at": format_datetime(utc_now_iso()),
            "error": "Team locations are temporarily unavailable while the database finishes updating."
        }
    finally:
        if conn:
            conn.close()


@app.route("/attendance/report")
@admin_required
def attendance_report():
    period = request.args.get("period", "day")
    selected_date = request.args.get("date") or local_now().date().isoformat()
    selected_user_id = request.args.get("user_id", type=int)
    report = attendance_report_data(period, selected_date, selected_user_id)
    return render_template(
        "attendance_report.html",
        users=report["users"],
        pairs=report["pairs"],
        summary=report["summary"].values(),
        period=report["period"],
        selected_date=selected_date,
        selected_user_id=selected_user_id,
        start=report["start"],
        end=report["end"],
        duration_text=duration_text,
        minutes_text=minutes_text,
        format_time=format_time,
        format_date=format_date
    )


@app.route("/my-time-report")
@login_required
def my_time_report():
    period = request.args.get("period", "day")
    if period not in ["day", "week", "month"]:
        period = "day"
    selected_date = request.args.get("date") or local_now().date().isoformat()
    report = attendance_report_data(period, selected_date, session.get("user_id"))
    return render_template(
        "attendance_report.html",
        users=[],
        pairs=report["pairs"],
        summary=report["summary"].values(),
        period=report["period"],
        selected_date=selected_date,
        selected_user_id=session.get("user_id"),
        start=report["start"],
        end=report["end"],
        duration_text=duration_text,
        minutes_text=minutes_text,
        format_time=format_time,
        format_date=format_date,
        my_report=True
    )


def attendance_report_data(period, selected_date, selected_user_id=None):
    period, start, end = attendance_range(period, selected_date)
    conn = db()
    users = conn.execute("SELECT id, name, email, role FROM users ORDER BY name").fetchall()
    query = """
        SELECT attendance_events.*, users.name AS user_name, users.email AS user_email, projects.name AS project_name
        FROM attendance_events
        LEFT JOIN users ON attendance_events.user_id = users.id
        LEFT JOIN projects ON attendance_events.project_id = projects.id
        WHERE attendance_events.created_at >= %s AND attendance_events.created_at < %s
    """
    params = [
        storage_datetime(start - timedelta(days=1)).isoformat(),
        storage_datetime(end + timedelta(days=1)).isoformat()
    ]
    if selected_user_id:
        query += " AND attendance_events.user_id = %s"
        params.append(selected_user_id)
    query += " ORDER BY attendance_events.created_at ASC, attendance_events.user_id, attendance_events.project_id, attendance_events.id"
    events = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    events = [e for e in events if attendance_event_in_range(e, period, selected_date)]
    pairs = build_attendance_pairs(events)
    pairs.sort(key=attendance_pair_sort_key)
    summary = {}
    for p in pairs:
        ci = p.get("check_in")
        co = p.get("check_out")
        if not ci or not co:
            continue
        uid = (p.get("user") or {}).get("user_id") or "unknown"
        if uid not in summary:
            summary[uid] = {
                "name": (p.get("user") or {}).get("user_name") or "Unknown user",
                "email": (p.get("user") or {}).get("user_email") or "",
                "minutes": 0
            }
        summary[uid]["minutes"] += duration_minutes(ci.get("created_at"), co.get("created_at"))
    return {"users": users, "pairs": pairs, "summary": summary, "period": period, "start": start, "end": end}


@app.route("/attendance/report/export")
@admin_required
def attendance_report_export():
    period = request.args.get("period", "day")
    selected_date = request.args.get("date") or local_now().date().isoformat()
    selected_user_id = request.args.get("user_id", type=int)
    report = attendance_report_data(period, selected_date, selected_user_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["User", "Email", "Project", "Date", "Time Zone", "Clock In", "Clock In Location", "Clock Out", "Clock Out Location", "Total Minutes", "Total"])
    for p in report["pairs"]:
        ci = p.get("check_in")
        co = p.get("check_out")
        u = p.get("user") or {}
        event = ci or co or {}
        writer.writerow([
            u.get("user_name") or "Unknown user",
            u.get("user_email") or "",
            event.get("project_name") or "No project",
            format_event_date(event),
            event_timezone_name(event),
            format_event_time(ci) if ci else "",
            ci.get("address") if ci else "",
            format_event_time(co) if co else "",
            co.get("address") if co else "",
            duration_minutes(ci.get("created_at"), co.get("created_at")) if ci and co else "",
            duration_text(ci.get("created_at"), co.get("created_at")) if ci and co else ""
        ])
    filename = f"projectonus_time_report_{report['period']}_{selected_date}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/attendance/<int:event_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_attendance_event(event_id):
    conn = db()
    event = conn.execute(
        """
        SELECT attendance_events.*, users.name AS user_name, users.email AS user_email, projects.name AS project_name
        FROM attendance_events
        LEFT JOIN users ON attendance_events.user_id = users.id
        LEFT JOIN projects ON attendance_events.project_id = projects.id
        WHERE attendance_events.id = %s
        """,
        (event_id,)
    ).fetchone()
    if not event:
        conn.close()
        flash("Clock record not found.")
        return redirect(url_for("attendance_report"))

    return_url = request.values.get("return_url", "")
    if not return_url.startswith("/attendance/report"):
        return_url = url_for("attendance_report", date=local_now().date().isoformat())

    if request.method == "POST":
        event_type = request.form.get("event_type", "")
        if event_type not in ["check_in", "check_out"]:
            conn.close()
            flash("Choose Clock In or Clock Out.")
            return redirect(url_for("edit_attendance_event", event_id=event_id, return_url=return_url))

        user_id = request.form.get("user_id", type=int)
        project_id = request.form.get("project_id", type=int)
        if not conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone():
            conn.close()
            flash("Choose a valid user.")
            return redirect(url_for("edit_attendance_event", event_id=event_id, return_url=return_url))
        if not conn.execute("SELECT id FROM projects WHERE id = %s", (project_id,)).fetchone():
            conn.close()
            flash("Choose a valid project.")
            return redirect(url_for("edit_attendance_event", event_id=event_id, return_url=return_url))

        latitude = None
        longitude = None
        try:
            lat_text = request.form.get("latitude", "").strip()
            lon_text = request.form.get("longitude", "").strip()
            latitude = float(lat_text) if lat_text else None
            longitude = float(lon_text) if lon_text else None
        except Exception:
            conn.close()
            flash("GPS latitude and longitude must be numbers.")
            return redirect(url_for("edit_attendance_event", event_id=event_id, return_url=return_url))

        event_timezone = request.form.get("event_timezone", "").strip()
        if latitude is not None and longitude is not None:
            event_timezone = timezone_from_location(latitude, longitude, event_timezone or APP_TIMEZONE)
        else:
            event_timezone = clean_timezone_name(event_timezone or event_timezone_name(event))

        try:
            local_value = datetime.strptime(
                request.form.get("event_date", "") + " " + request.form.get("event_time", ""),
                "%Y-%m-%d %H:%M"
            )
            created_at = storage_datetime(local_value, event_timezone).isoformat()
        except Exception:
            conn.close()
            flash("Enter a valid date and time.")
            return redirect(url_for("edit_attendance_event", event_id=event_id, return_url=return_url))

        conn.execute(
            """
            UPDATE attendance_events
            SET user_id = %s, project_id = %s, event_type = %s, latitude = %s, longitude = %s, address = %s, event_timezone = %s, created_at = %s
            WHERE id = %s
            """,
            (
                user_id,
                project_id,
                event_type,
                latitude,
                longitude,
                request.form.get("address", "").strip(),
                event_timezone,
                created_at,
                event_id
            )
        )
        conn.commit()
        conn.close()
        flash("Clock record updated.")
        return redirect(return_url)

    users = conn.execute("SELECT id, name, email, role FROM users ORDER BY name").fetchall()
    projects = conn.execute("SELECT id, name, customer_name FROM projects ORDER BY name").fetchall()
    conn.close()
    selected_timezone = event_timezone_name(event)
    event_dt = local_datetime(event.get("created_at"), selected_timezone) or local_now()
    return render_template(
        "edit_attendance.html",
        event=event,
        users=users,
        projects=projects,
        selected_timezone=selected_timezone,
        event_date=event_dt.date().isoformat(),
        event_time=event_dt.strftime("%H:%M"),
        common_timezones=COMMON_TIMEZONES,
        return_url=return_url
    )



@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        redirect_tab = None
        if action == "logo":
            logo = request.files.get("company_logo")
            if logo and logo.filename and allowed_logo(logo.filename):
                logo_path = upload_file_to_storage(logo)
                set_app_setting("company_logo", logo_path)
                flash("Company logo updated.")
            else:
                flash("Please upload a valid logo file: PNG, JPG, WEBP, GIF, or SVG.")
        elif action == "email_notifications":
            set_app_setting("email_note_comments", "1" if "email_note_comments" in request.form else "0")
            set_app_setting("email_note_pictures", "1" if "email_note_pictures" in request.form else "0")
            set_app_setting("email_note_audio", "1" if "email_note_audio" in request.form else "0")
            flash("Email notification preferences updated.")
        elif action == "dtools_cloud":
            set_app_setting("dtools_cloud_base_url", request.form.get("dtools_cloud_base_url", DTOOLS_CLOUD_DEFAULT_BASE_URL).strip() or DTOOLS_CLOUD_DEFAULT_BASE_URL)
            set_app_setting("dtools_cloud_auth_header", request.form.get("dtools_cloud_auth_header", DTOOLS_CLOUD_DEFAULT_AUTH).strip() or DTOOLS_CLOUD_DEFAULT_AUTH)
            set_app_setting("dtools_cloud_material_path", request.form.get("dtools_cloud_material_path", "Projects/GetProject").strip() or "Projects/GetProject")
            set_app_setting("dtools_cloud_id_param", request.form.get("dtools_cloud_id_param", "Id").strip() or "Id")
            api_key = request.form.get("dtools_cloud_api_key", "").strip()
            if "dtools_cloud_clear_key" in request.form:
                set_app_setting("dtools_cloud_api_key", "")
                flash("D-Tools Cloud API settings saved and API key cleared.")
            else:
                if api_key:
                    set_app_setting("dtools_cloud_api_key", api_key)
                flash("D-Tools Cloud API settings saved.")
        elif action == "permissions":
            user_id = int(request.form.get("user_id"))
            values = {k: (k in request.form) for k in PERMISSION_KEYS}
            conn.execute(
                """
                INSERT INTO user_permissions
                (user_id, see_comments, write_comments, edit_comments, delete_comments, see_pictures, add_pictures, delete_pictures, see_audio, add_audio, delete_audio, create_rooms, view_inventory, edit_inventory)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    see_comments = EXCLUDED.see_comments,
                    write_comments = EXCLUDED.write_comments,
                    edit_comments = EXCLUDED.edit_comments,
                    delete_comments = EXCLUDED.delete_comments,
                    see_pictures = EXCLUDED.see_pictures,
                    add_pictures = EXCLUDED.add_pictures,
                    delete_pictures = EXCLUDED.delete_pictures,
                    see_audio = EXCLUDED.see_audio,
                    add_audio = EXCLUDED.add_audio,
                    delete_audio = EXCLUDED.delete_audio,
                    create_rooms = EXCLUDED.create_rooms,
                    view_inventory = EXCLUDED.view_inventory,
                    edit_inventory = EXCLUDED.edit_inventory
                """,
                (user_id, *[values[k] for k in PERMISSION_KEYS])
            )
            conn.commit()
            flash("User permissions updated.")
        elif action == "project_access":
            redirect_tab = "project_access"
            user_id = int(request.form.get("user_id"))
            user = conn.execute("SELECT id, role FROM users WHERE id = %s", (user_id,)).fetchone()
            if not user:
                flash("User not found.")
            elif user.get("role") == "admin":
                flash("Admin accounts can already see every project.")
            else:
                allowed_project_ids = {
                    row["id"] for row in conn.execute("SELECT id FROM projects").fetchall()
                }
                selected_project_ids = []
                for value in request.form.getlist("project_ids"):
                    try:
                        project_id = int(value)
                    except Exception:
                        continue
                    if project_id in allowed_project_ids:
                        selected_project_ids.append(project_id)

                conn.execute("DELETE FROM project_permissions WHERE user_id = %s", (user_id,))
                for project_id in selected_project_ids:
                    conn.execute(
                        """
                        INSERT INTO project_permissions (user_id, project_id, created_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id, project_id) DO NOTHING
                        """,
                        (user_id, project_id, datetime.now().isoformat())
                    )
                conn.commit()
                flash("Project access updated.")
        if redirect_tab:
            return redirect(url_for("settings", tab=redirect_tab))
        return redirect(url_for("settings"))

    active_tab = request.args.get("tab", "permissions")
    if active_tab not in ["permissions", "project_access"]:
        active_tab = "permissions"
    users = conn.execute("SELECT id, name, email, role FROM users ORDER BY name").fetchall()
    projects = conn.execute("SELECT id, name, customer_name, customer_address FROM projects ORDER BY name").fetchall()
    permissions = conn.execute("SELECT * FROM user_permissions").fetchall()
    project_permissions = conn.execute("SELECT user_id, project_id FROM project_permissions").fetchall()
    conn.close()
    perm_map = {p["user_id"]: p for p in permissions}
    project_access_map = {}
    for row in project_permissions:
        project_access_map.setdefault(row["user_id"], set()).add(row["project_id"])
    return render_template(
        "settings.html",
        users=users,
        projects=projects,
        perm_map=perm_map,
        project_access_map=project_access_map,
        active_tab=active_tab,
        permission_keys=PERMISSION_KEYS
    )


@app.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    conn = db()
    if request.method == "POST":
        if is_main_admin():
            conn.execute("UPDATE login_events SET is_read = TRUE WHERE is_read = FALSE AND event_type NOT IN ('login', 'task_assigned')")
        else:
            conn.execute(
                "UPDATE login_events SET is_read = TRUE WHERE is_read = FALSE AND user_id = %s AND event_type = 'task_assigned'",
                (session.get("user_id"),)
            )
        conn.commit()
        flash("Notifications marked as read.")
    if is_main_admin():
        events = conn.execute(
            """
            SELECT login_events.*, tasks.task_number, tasks.title AS task_title, tasks.accepted_at AS task_accepted_at,
                   tasks.status AS task_status, projects.name AS project_name
            FROM login_events
            LEFT JOIN tasks ON login_events.task_id = tasks.id
            LEFT JOIN projects ON COALESCE(login_events.project_id, tasks.project_id) = projects.id
            WHERE login_events.event_type NOT IN ('login', 'task_assigned')
            ORDER BY login_events.created_at DESC
            LIMIT 100
            """
        ).fetchall()
    else:
        events = conn.execute(
            """
            SELECT login_events.*, tasks.task_number, tasks.title AS task_title, tasks.accepted_at AS task_accepted_at,
                   tasks.status AS task_status, projects.name AS project_name
            FROM login_events
            LEFT JOIN tasks ON login_events.task_id = tasks.id
            LEFT JOIN projects ON COALESCE(login_events.project_id, tasks.project_id) = projects.id
            JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
            WHERE login_events.user_id = %s AND login_events.event_type = 'task_assigned'
            ORDER BY login_events.created_at DESC
            LIMIT 100
            """,
            (session.get("user_id"), session.get("user_id"))
        ).fetchall()
    conn.close()
    return render_template("notifications.html", events=events)


@app.route("/notifications/live")
@login_required
def notifications_live():
    try:
        response = Response(json.dumps(notification_summary()), mimetype="application/json")
    except Exception as e:
        print("Live notification check failed:", e)
        response = Response(
            json.dumps({"unread_count": unread_notification_count(), "latest": None}),
            mimetype="application/json"
        )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/note/<int:note_id>/edit", methods=["GET", "POST"])
@login_required
def edit_note(note_id):
    if not (is_main_admin() or has_perm("edit_comments")):
        flash("You do not have permission to edit comments.")
        return redirect(url_for("index"))
    conn = db()
    note = conn.execute("SELECT notes.*, rooms.name AS room_name, rooms.project_id FROM notes JOIN rooms ON notes.room_id = rooms.id WHERE notes.id = %s", (note_id,)).fetchone()
    if not note:
        conn.close()
        flash("Comment not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, note["project_id"]):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    if request.method == "POST":
        conn.execute("UPDATE notes SET comment = %s, note_date = %s WHERE id = %s", (request.form["comment"].strip(), request.form["note_date"], note_id))
        conn.commit()
        room_id = note["room_id"]
        conn.close()
        flash("Comment updated.")
        return redirect(url_for("room", room_id=room_id))
    conn.close()
    return render_template("edit_note.html", note=note)


@app.route("/backup")
@admin_required
def backup():
    conn = db()
    tables = {}
    backup_warnings = []
    backup_tables = [
        ("users", "id"),
        ("projects", "id"),
        ("project_blueprints", "id"),
        ("rooms", "id"),
        ("notes", "id"),
        ("tasks", "id"),
        ("task_attachments", "id"),
        ("task_room_statuses", "task_id, room_id"),
        ("material_inventory", "id"),
        ("inventory_items", "id"),
        ("attendance_events", "id"),
        ("worker_location_pings", "id"),
        ("login_events", "id"),
        ("task_number_counters", "month_key"),
        ("task_delete_codes", "id"),
        ("user_permissions", "user_id"),
        ("project_permissions", "user_id, project_id"),
        ("app_settings", "key"),
        ("push_subscriptions", "id"),
    ]
    for table, order_by in backup_tables:
        try:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
            tables[f"{table}.json"] = json.dumps([dict(row) for row in rows], indent=2, default=str)
        except Exception as e:
            conn.rollback()
            backup_warnings.append(f"{table}.json could not be exported: {e}")

    try:
        projects = conn.execute("SELECT blueprint_file, blueprint_preview_file FROM projects").fetchall()
    except Exception as e:
        conn.rollback()
        projects = []
        backup_warnings.append(f"Project blueprint files could not be listed: {e}")
    try:
        project_blueprints = conn.execute("SELECT blueprint_file, blueprint_preview_file FROM project_blueprints").fetchall()
    except Exception as e:
        conn.rollback()
        project_blueprints = []
        backup_warnings.append(f"Blueprint sheet files could not be listed: {e}")
    try:
        notes = conn.execute("SELECT photo_file, audio_file FROM notes WHERE photo_file IS NOT NULL OR audio_file IS NOT NULL").fetchall()
    except Exception as e:
        conn.rollback()
        notes = []
        backup_warnings.append(f"Note files could not be listed: {e}")
    try:
        material_pictures = conn.execute("SELECT picture_file FROM material_inventory WHERE picture_file IS NOT NULL").fetchall()
    except Exception as e:
        conn.rollback()
        material_pictures = []
        backup_warnings.append(f"Material pictures could not be listed: {e}")
    try:
        inventory_pictures = conn.execute("SELECT picture_file FROM inventory_items WHERE picture_file IS NOT NULL").fetchall()
    except Exception as e:
        conn.rollback()
        inventory_pictures = []
        backup_warnings.append(f"Inventory pictures could not be listed: {e}")
    try:
        task_files = conn.execute(
            """
            SELECT task_photo_file, task_audio_file, completion_photo_file, completion_audio_file
            FROM tasks
            WHERE task_photo_file IS NOT NULL
               OR task_audio_file IS NOT NULL
               OR completion_photo_file IS NOT NULL
               OR completion_audio_file IS NOT NULL
            """
        ).fetchall()
    except Exception as e:
        conn.rollback()
        task_files = []
        backup_warnings.append(f"Task files could not be listed: {e}")
    try:
        task_attachment_files = conn.execute("SELECT storage_path FROM task_attachments WHERE storage_path IS NOT NULL").fetchall()
    except Exception as e:
        conn.rollback()
        task_attachment_files = []
        backup_warnings.append(f"Task attachment files could not be listed: {e}")
    conn.close()

    backup_name = f"blueprint_room_log_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    backup_path = os.path.join(tempfile.gettempdir(), backup_name)

    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, content in tables.items():
            z.writestr(filename, content)

        def add_storage_file(storage_path, folder):
            if not storage_path:
                return
            try:
                data = download_storage_file(storage_path)
                if data:
                    z.writestr(f"{folder}/{os.path.basename(storage_path)}", data)
                else:
                    backup_warnings.append(f"{storage_path} could not be downloaded from storage.")
            except Exception as e:
                backup_warnings.append(f"{storage_path} could not be added to backup: {e}")

        for p in list(projects) + list(project_blueprints):
            for key, folder in [("blueprint_file", "blueprints"), ("blueprint_preview_file", "blueprints/previews")]:
                add_storage_file(p.get(key), folder)
        for n in notes:
            add_storage_file(n.get("photo_file"), "photos")
            add_storage_file(n.get("audio_file"), "audio")
        for m in material_pictures:
            add_storage_file(m.get("picture_file"), "material_pictures")
        for item in inventory_pictures:
            add_storage_file(item.get("picture_file"), "inventory_pictures")
        for task in task_files:
            add_storage_file(task.get("task_photo_file"), "task_files")
            add_storage_file(task.get("task_audio_file"), "task_files")
            add_storage_file(task.get("completion_photo_file"), "task_completion_files")
            add_storage_file(task.get("completion_audio_file"), "task_completion_files")
        for attachment in task_attachment_files:
            add_storage_file(attachment.get("storage_path"), "task_attachments")
        z.writestr("README_BACKUP.txt", "Portable backup: JSON table exports plus uploaded files.")
        if backup_warnings:
            z.writestr("BACKUP_WARNINGS.txt", "\n".join(backup_warnings))

    return Response(open(backup_path, "rb").read(), mimetype="application/zip", headers={"Content-Disposition": f"attachment; filename={backup_name}"})



@app.route("/storage_file/<path:storage_path>")
@login_required
def storage_file(storage_path):
    """
    Serve files from Supabase Storage through Flask.
    This avoids browser/public-url problems and makes PDF/image display more reliable.
    """
    conn = db()
    owner = conn.execute(
        """
        SELECT id AS project_id FROM projects WHERE blueprint_file = %s OR blueprint_preview_file = %s
        UNION
        SELECT project_id FROM project_blueprints WHERE blueprint_file = %s OR blueprint_preview_file = %s
        UNION
        SELECT rooms.project_id FROM notes JOIN rooms ON notes.room_id = rooms.id WHERE notes.photo_file = %s OR notes.audio_file = %s
        UNION
        SELECT project_id FROM material_inventory WHERE picture_file = %s
        UNION
        SELECT project_id FROM inventory_items WHERE picture_file = %s
        UNION
        SELECT project_id FROM tasks WHERE task_photo_file = %s OR task_audio_file = %s OR completion_photo_file = %s OR completion_audio_file = %s
        UNION
        SELECT tasks.project_id FROM task_attachments JOIN tasks ON task_attachments.task_id = tasks.id WHERE task_attachments.storage_path = %s
        LIMIT 1
        """,
        (
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path,
            storage_path
        )
    ).fetchone()
    if owner and owner.get("project_id") and not user_can_access_project(conn, owner["project_id"]):
        conn.close()
        return "You do not have access to this project file.", 403
    conn.close()

    data = download_storage_file(storage_path)
    if not data:
        return "File not found or storage permission denied.", 404

    mime_type = mimetypes.guess_type(storage_path)[0] or "application/octet-stream"
    response = Response(data, mimetype=mime_type)
    response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/project/<int:project_id>/regenerate-preview", methods=["POST"])
@login_required
def regenerate_preview(project_id):
    """
    Rebuild the PNG preview from the stored PDF blueprint.
    Useful if a PDF was uploaded before preview conversion was fixed.
    """
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()

    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))

    blueprint_file = project.get("blueprint_file")
    if not blueprint_file or not blueprint_file.lower().endswith(".pdf"):
        conn.close()
        flash("This project does not have a PDF blueprint.")
        return redirect(url_for("project", project_id=project_id))

    pdf_data = download_storage_file(blueprint_file)
    if not pdf_data:
        conn.close()
        flash("Could not download the PDF from storage. Check Supabase Storage permissions.")
        return redirect(url_for("project", project_id=project_id))

    preview_path = create_pdf_preview_from_bytes(pdf_data)
    if not preview_path:
        conn.close()
        flash("Could not create PDF preview. Check Render logs for 'PDF preview conversion failed'.")
        return redirect(url_for("project", project_id=project_id))

    conn.execute(
        "UPDATE projects SET blueprint_preview_file = %s WHERE id = %s",
        (preview_path, project_id)
    )
    conn.commit()
    conn.close()

    flash("PDF preview regenerated successfully.")
    return redirect(url_for("project", project_id=project_id))


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    sub = request.get_data(as_text=True)
    if not sub:
        return {"ok": False}, 400
    conn = db()
    conn.execute("INSERT INTO push_subscriptions (user_id, subscription_json, created_at) VALUES (%s, %s, %s)", (session.get("user_id"), sub, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"ok": True}



@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/health")
def health():
    return "ok"


@app.route("/ui/pro-test")
@login_required
def pro_test_ui():
    session["ui_theme"] = "pro_test"
    flash("Pro Test UI is on for this browser session. Use Classic UI to go back.")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("index"))


@app.route("/ui/classic")
@login_required
def classic_ui():
    session.pop("ui_theme", None)
    flash("Classic UI restored.")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("index"))


try:
    init_db()
except Exception as e:
    print("Database initialization failed:", e)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
