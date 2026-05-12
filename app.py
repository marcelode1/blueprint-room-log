from flask import Flask, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os, uuid, zipfile, tempfile, json, mimetypes, smtplib, ssl
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
        file_storage.content_type or "application/octet-stream"
    )


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
        customer_address TEXT,
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
        user_name TEXT,
        user_email TEXT,
        role TEXT,
        event_type TEXT NOT NULL DEFAULT 'login',
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
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
        assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        task_date TEXT NOT NULL,
        title TEXT NOT NULL,
        instructions TEXT,
        require_picture BOOLEAN NOT NULL DEFAULT FALSE,
        allow_picture_upload BOOLEAN NOT NULL DEFAULT TRUE,
        allow_comment BOOLEAN NOT NULL DEFAULT TRUE,
        allow_audio BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL DEFAULT 'open',
        completion_comment TEXT,
        completion_photo_file TEXT,
        completion_audio_file TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL
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

    conn.commit()

    # Safe migrations for older deployments
    migrations = [
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_name TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_address TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_phone TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_email TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",
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
        "CREATE TABLE IF NOT EXISTS tasks (id SERIAL PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL, assigned_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_by INTEGER REFERENCES users(id) ON DELETE SET NULL, task_date TEXT NOT NULL, title TEXT NOT NULL, instructions TEXT, require_picture BOOLEAN NOT NULL DEFAULT FALSE, allow_picture_upload BOOLEAN NOT NULL DEFAULT TRUE, allow_comment BOOLEAN NOT NULL DEFAULT TRUE, allow_audio BOOLEAN NOT NULL DEFAULT TRUE, status TEXT NOT NULL DEFAULT 'open', completion_comment TEXT, completion_photo_file TEXT, completion_audio_file TEXT, completion_at TEXT, created_at TEXT NOT NULL)",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at TEXT",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completion_audio_file TEXT",
        "ALTER TABLE tasks DROP COLUMN IF EXISTS completion_at",
        "CREATE TABLE IF NOT EXISTS attendance_events (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL, event_type TEXT NOT NULL, latitude REAL, longitude REAL, address TEXT, event_timezone TEXT, created_at TEXT NOT NULL)",
        "ALTER TABLE attendance_events ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL",
        "ALTER TABLE attendance_events ADD COLUMN IF NOT EXISTS event_timezone TEXT",
        "CREATE TABLE IF NOT EXISTS project_blueprints (id SERIAL PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL, blueprint_file TEXT NOT NULL, blueprint_preview_file TEXT, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS login_events (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, user_name TEXT, user_email TEXT, role TEXT, event_type TEXT NOT NULL DEFAULT 'login', is_read BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL)",
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

    conn.close()



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
        row = conn.execute("SELECT COUNT(*) AS c FROM login_events WHERE is_read = FALSE AND event_type <> 'login'").fetchone()
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
            row = conn.execute("SELECT COUNT(*) AS c FROM login_events WHERE is_read = FALSE AND event_type <> 'login'").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM login_events WHERE is_read = FALSE AND user_id = %s AND event_type = 'task_assigned'",
                (session.get("user_id"),)
            ).fetchone()
        conn.close()
        return row["c"] if row else 0
    except Exception:
        return 0


def add_notification(conn, user_id, user_name, user_email, role, event_type):
    conn.execute(
        "INSERT INTO login_events (user_id, user_name, user_email, role, event_type, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, user_name, user_email, role, event_type, utc_now_iso())
    )


def storage_attachment(path):
    if not path:
        return None
    data = download_storage_file(path)
    if not data:
        return None
    filename = os.path.basename(path)
    mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return (filename, data, mime_type)


def admin_email_rows(conn):
    return conn.execute("SELECT email FROM users WHERE role = 'admin' ORDER BY id").fetchall()


def notify_admins_of_field_note(conn, project, room, comment, photo_file, audio_file, note_date):
    actor = conn.execute(
        "SELECT name, email, role FROM users WHERE id = %s",
        (session.get("user_id"),)
    ).fetchone() or {}
    actor_name = actor.get("name") or session.get("name")
    actor_email = actor.get("email") or ""
    actor_role = actor.get("role") or session.get("role")
    notification_types = []
    if comment:
        notification_types.append("field_comment_added")
    if photo_file:
        notification_types.append("field_picture_added")
    if audio_file:
        notification_types.append("field_audio_added")
    if not notification_types:
        notification_types.append("field_note_added")
    for event_type in notification_types:
        add_notification(conn, session.get("user_id"), actor_name, actor_email, actor_role, event_type)
    conn.commit()

    send_comments = setting_enabled("email_note_comments", True)
    send_pictures = setting_enabled("email_note_pictures", True)
    send_audio = setting_enabled("email_note_audio", True)
    wants_email = (comment and send_comments) or (photo_file and send_pictures) or (audio_file and send_audio)
    if not wants_email:
        return

    admins = admin_email_rows(conn)
    if not admins:
        return

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
        "A field update was added in Projectonus.",
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
    subject = f"Projectonus field update - {room.get('name') if room else 'Room'}"
    for admin in admins:
        if admin.get("email"):
            send_email(admin["email"], subject, body, attachments=attachments)


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
        f"{label} recorded in Projectonus.",
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
            send_email(admin["email"], f"Projectonus {label} - {actor_name or 'User'}", body)


def can_add_notes():
    return has_perm("write_comments") or has_perm("add_pictures") or has_perm("add_audio")


def can_view_inventory():
    return is_main_admin() or has_perm("view_inventory") or has_perm("edit_inventory")


def can_edit_inventory():
    return is_main_admin() or has_perm("edit_inventory")


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


def fetch_visible_projects(conn, q=""):
    params = []
    join_sql = ""
    if not is_main_admin():
        join_sql = "JOIN project_permissions ON project_permissions.project_id = projects.id AND project_permissions.user_id = %s"
        params.append(session.get("user_id"))

    where_sql = ""
    if q:
        like = f"%{q}%"
        where_sql = "WHERE projects.name ILIKE %s OR projects.customer_name ILIKE %s OR projects.customer_address ILIKE %s"
        params.extend([like, like, like])

    return conn.execute(
        f"SELECT projects.* FROM projects {join_sql} {where_sql} ORDER BY projects.created_at DESC",
        tuple(params)
    ).fetchall()


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


@app.context_processor
def utility_processor():
    return dict(
        file_url=file_url,
        is_main_admin=is_main_admin,
        can_add_notes=can_add_notes,
        has_perm=has_perm,
        get_app_setting=get_app_setting,
        format_time=format_time,
        format_date=format_date,
        format_datetime=format_datetime,
        format_event_time=format_event_time,
        format_event_date=format_event_date,
        format_event_datetime=format_event_datetime,
        event_timezone_name=event_timezone_name,
        admin_unread_count=admin_unread_count,
        unread_notification_count=unread_notification_count,
        can_view_inventory=can_view_inventory,
        can_edit_inventory=can_edit_inventory
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
    q = request.args.get("q", "").strip()
    conn = db()
    projects = fetch_visible_projects(conn, q)
    conn.close()
    return render_template("mobile_home.html", projects=projects, q=q)


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
            flash("You do not have permission to add material inventory.")
            return redirect(url_for("mobile_project_materials", project_id=project_id))

        file = request.files.get("picture") or request.files.get("picture_camera")
        picture_file = upload_file_to_storage(file) if file and file.filename and allowed_photo(file.filename) else None

        conn.execute(
            "INSERT INTO material_inventory (project_id, user_id, item_date, quantity, part_number, description, material_status, picture_file, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                project_id,
                session.get("user_id"),
                request.form.get("item_date"),
                float(request.form.get("quantity") or 0),
                request.form.get("part_number", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("material_status", "not_in_stock"),
                picture_file,
                datetime.now().isoformat()
            )
        )
        conn.commit()
        flash("Material inventory item added.")

    materials = conn.execute(
        """
        SELECT material_inventory.*, users.name AS user_name
        FROM material_inventory
        LEFT JOIN users ON material_inventory.user_id = users.id
        WHERE project_id = %s
        ORDER BY item_date DESC, created_at DESC
        """,
        (project_id,)
    ).fetchall()
    conn.close()
    return render_template("mobile_materials.html", project=project, materials=materials)



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
    rooms = conn.execute("SELECT * FROM rooms WHERE project_id = %s ORDER BY id", (project_id,)).fetchall()
    conn.close()
    return render_template("mobile_project.html", project=project, rooms=rooms)


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
    rooms = conn.execute("SELECT id, name FROM rooms WHERE project_id = %s ORDER BY id", (room["project_id"],)).fetchall()
    tasks = conn.execute(
        """
        SELECT tasks.*, users.name AS assigned_user_name
        FROM tasks
        LEFT JOIN users ON tasks.assigned_user_id = users.id
        WHERE tasks.room_id = %s AND (tasks.assigned_user_id = %s OR %s = 'admin')
        ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
        """,
        (room_id, session.get("user_id"), session.get("role"))
    ).fetchall()

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
        notify_admins_of_field_note(conn, project, room, request.form["comment"].strip(), photo_file, audio_file, request.form["note_date"])
        conn.commit()
        flash("Comment/photo/audio added.")

    selected_date = request.args.get("date", "")
    query = "SELECT notes.*, users.name AS user_name FROM notes LEFT JOIN users ON notes.user_id = users.id WHERE room_id = %s"
    params = [room_id]
    if selected_date:
        query += " AND note_date = %s"
        params.append(selected_date)
    query += " ORDER BY note_date DESC, created_at DESC"
    notes = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    return render_template("mobile_room.html", room=room, project=project, rooms=rooms, notes=notes, tasks=tasks, selected_date=selected_date)


@app.route("/routes-check")
def routes_check():
    return "<h1>Projectonus Routes Active</h1><br>" + "<br>".join(sorted(str(r) for r in app.url_map.iter_rules()))


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
        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE email = %s AND role <> 'admin'",
            (email,)
        ).fetchone()
        conn.close()
        if user and user.get("pin_hash") and check_password_hash(user["pin_hash"], pin):
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            return redirect(url_for("mobile_home"))
        flash("Invalid email or PIN.")
    return render_template("mobile_login.html", email=request.args.get("email", "").strip().lower())


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
            "Create your Projectonus admin login",
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
                "Reset your Projectonus admin password",
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
            if len(pin) < 4:
                conn.close()
                flash("PIN must be at least 4 digits.")
                return redirect(url_for("users"))

            invite_token = new_token()
            conn.execute(
                "INSERT INTO users (name, email, password_hash, pin_hash, invite_token, invite_sent_at, role, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    request.form["name"].strip(),
                    email,
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
                "You are invited to Projectonus",
                "Open this mobile link and sign in with your email and the PIN provided by the admin:\n\n" + invite_link
            )
            if sent:
                flash("User added and mobile invitation email sent. Share the PIN with the user.")
            else:
                flash("User added. Email could not be sent, so share the mobile link and PIN with the user: " + invite_link)
        except Exception:
            conn.rollback()
            flash("That email may already exist.")

    users = conn.execute("SELECT id, name, email, role, created_at, invite_token FROM users ORDER BY name").fetchall()
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
        "Your Projectonus mobile invitation",
        "Open this mobile link and sign in with your email and the PIN provided by the admin:\n\n" + invite_link
    )
    conn.close()
    if sent:
        flash("PIN updated and invitation email sent. Share the PIN with the user.")
    else:
        flash("PIN updated. Email could not be sent, so share this mobile link with the user: " + invite_link)
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
        customer_address = request.form.get("customer_address", "").strip()
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
            "INSERT INTO projects (name, customer_name, customer_address, customer_phone, customer_email, blueprint_file, blueprint_preview_file, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (name, customer_name, customer_address, customer_phone, customer_email, blueprint_file, blueprint_preview_file, datetime.now().isoformat())
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

        conn.execute(
            """
            UPDATE projects
            SET name = %s, customer_name = %s, customer_address = %s, customer_phone = %s, customer_email = %s
            WHERE id = %s
            """,
            (
                name,
                request.form.get("customer_name", "").strip(),
                request.form.get("customer_address", "").strip(),
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
            flash("You do not have permission to add material inventory.")
            return redirect(url_for("project_materials", project_id=project_id))

        file = request.files.get("picture") or request.files.get("picture_camera")
        picture_file = upload_file_to_storage(file) if file and file.filename and allowed_photo(file.filename) else None

        conn.execute(
            "INSERT INTO material_inventory (project_id, user_id, item_date, quantity, part_number, description, material_status, picture_file, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                project_id,
                session.get("user_id"),
                request.form.get("item_date"),
                float(request.form.get("quantity") or 0),
                request.form.get("part_number", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("material_status", "not_in_stock"),
                picture_file,
                datetime.now().isoformat()
            )
        )
        conn.commit()
        flash("Material inventory item added.")

    materials = conn.execute(
        """
        SELECT material_inventory.*, users.name AS user_name
        FROM material_inventory
        LEFT JOIN users ON material_inventory.user_id = users.id
        WHERE project_id = %s
        ORDER BY item_date DESC, created_at DESC
        """,
        (project_id,)
    ).fetchall()
    conn.close()
    return render_template("materials.html", project=project, materials=materials)


@app.route("/project/<int:project_id>/materials/<int:material_id>/status", methods=["POST"])
@login_required
def update_material_status(project_id, material_id):
    if not can_edit_inventory():
        flash("You do not have permission to update material status.")
        return redirect(url_for("project_materials", project_id=project_id))

    new_status = request.form.get("material_status", "not_in_stock")
    if new_status not in ["in_stock", "not_in_stock", "used"]:
        new_status = "not_in_stock"

    conn = db()
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    conn.execute(
        "UPDATE material_inventory SET material_status = %s WHERE id = %s AND project_id = %s",
        (new_status, material_id, project_id)
    )
    conn.commit()
    conn.close()
    flash("Material status updated.")
    if "/mobile/" in (request.referrer or ""):
        return redirect(url_for("mobile_project_materials", project_id=project_id))
    return redirect(url_for("project_materials", project_id=project_id))


@app.route("/project/<int:project_id>/materials/<int:material_id>/delete", methods=["POST"])
@login_required
def delete_material(project_id, material_id):
    if not can_edit_inventory():
        flash("You do not have permission to delete material inventory.")
        return redirect(url_for("project_materials", project_id=project_id))

    conn = db()
    if not user_can_access_project(conn, project_id):
        conn.close()
        flash("You do not have access to this project.")
        return redirect(url_for("index"))
    conn.execute("DELETE FROM material_inventory WHERE id = %s AND project_id = %s", (material_id, project_id))
    conn.commit()
    conn.close()
    flash("Material inventory item deleted.")
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
    name = request.form.get("name", "").strip()
    if not name:
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
    project_rooms = conn.execute("SELECT id, name FROM rooms WHERE project_id = %s ORDER BY id", (room["project_id"],)).fetchall()
    users = conn.execute(
        """
        SELECT DISTINCT users.id, users.name, users.email, users.role
        FROM users
        LEFT JOIN project_permissions ON project_permissions.user_id = users.id AND project_permissions.project_id = %s
        WHERE users.role = 'admin' OR project_permissions.project_id IS NOT NULL
        ORDER BY users.name
        """,
        (room["project_id"],)
    ).fetchall() if is_main_admin() else []
    tasks = conn.execute(
        """
        SELECT tasks.*, users.name AS assigned_user_name
        FROM tasks
        LEFT JOIN users ON tasks.assigned_user_id = users.id
        WHERE tasks.room_id = %s AND (tasks.assigned_user_id = %s OR %s = 'admin')
        ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
        """,
        (room_id, session.get("user_id"), session.get("role"))
    ).fetchall()

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
        notify_admins_of_field_note(conn, project, room, request.form["comment"].strip(), photo_file, audio_file, request.form["note_date"])
        conn.commit()
        flash("Comment/photo added.")

    selected_date = request.args.get("date", "")
    query = "SELECT notes.*, users.name AS user_name FROM notes LEFT JOIN users ON notes.user_id = users.id WHERE room_id = %s"
    params = [room_id]
    if selected_date:
        query += " AND note_date = %s"
        params.append(selected_date)
    query += " ORDER BY note_date DESC, created_at DESC"
    notes = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    return render_template("room.html", room=room, project=project, rooms=project_rooms, notes=notes, tasks=tasks, users=users, selected_date=selected_date)


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
    conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
    conn.commit()
    conn.close()
    flash("Project deleted.")
    return redirect(url_for("index"))


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
    assigned = conn.execute("SELECT id, name, email, role FROM users WHERE id = %s", (assigned_user_id,)).fetchone()
    title = request.form.get("title", "").strip()
    if not assigned or not title:
        conn.close()
        flash("Choose a user and enter a task title.")
        return redirect(url_for("room", room_id=room_id))
    if assigned.get("role") != "admin" and not conn.execute(
        "SELECT 1 FROM project_permissions WHERE user_id = %s AND project_id = %s",
        (assigned_user_id, room["project_id"])
    ).fetchone():
        conn.close()
        flash("That user does not have access to this project. Authorize the project in Settings first.")
        return redirect(url_for("room", room_id=room_id))

    conn.execute(
        """
        INSERT INTO tasks
        (project_id, room_id, assigned_user_id, created_by, task_date, title, instructions, require_picture, allow_picture_upload, allow_comment, allow_audio, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            room["project_id"],
            room_id,
            assigned_user_id,
            session.get("user_id"),
            request.form.get("task_date") or datetime.now().date().isoformat(),
            title,
            request.form.get("instructions", "").strip(),
            "require_picture" in request.form,
            "allow_picture_upload" in request.form,
            "allow_comment" in request.form,
            "allow_audio" in request.form,
            datetime.now().isoformat()
        )
    )
    add_notification(conn, assigned["id"], assigned["name"], assigned["email"], assigned["role"], "task_assigned")
    conn.commit()
    conn.close()
    flash("Task assigned and user notified.")
    return redirect(url_for("room", room_id=room_id))


@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
@login_required
def complete_task(task_id):
    conn = db()
    task = conn.execute(
        """
        SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, users.name AS assigned_user_name
        FROM tasks
        JOIN rooms ON tasks.room_id = rooms.id
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
        return redirect(url_for("room", room_id=task["room_id"]))

    file = request.files.get("completion_photo") or request.files.get("completion_camera")
    audio = request.files.get("completion_audio")
    wants_photo = bool(file and file.filename)
    if task.get("require_picture") and not wants_photo and not task.get("completion_photo_file"):
        conn.close()
        flash("This task requires a picture before it can be completed.")
        return redirect(url_for("room", room_id=task["room_id"]))

    photo_file = upload_file_to_storage(file) if wants_photo and allowed_photo(file.filename) else task.get("completion_photo_file")
    audio_file = upload_file_to_storage(audio) if audio and audio.filename and allowed_audio(audio.filename) else task.get("completion_audio_file")
    conn.execute(
        """
        UPDATE tasks
        SET status = 'done', completion_comment = %s, completion_photo_file = %s, completion_audio_file = %s, completed_at = %s
        WHERE id = %s
        """,
        (
            request.form.get("completion_comment", "").strip(),
            photo_file,
            audio_file,
            datetime.now().isoformat(),
            task_id
        )
    )
    add_notification(
        conn,
        session.get("user_id"),
        session.get("name"),
        "",
        session.get("role"),
        "task_completed"
    )
    conn.commit()
    conn.close()
    flash("Task marked done. Admin was notified.")
    if "/mobile/" in (request.referrer or ""):
        return redirect(url_for("mobile_room", room_id=task["room_id"]))
    return redirect(url_for("room", room_id=task["room_id"]))


@app.route("/tasks")
@login_required
def my_tasks():
    conn = db()
    if is_main_admin():
        tasks = conn.execute(
            """
            SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, users.name AS assigned_user_name
            FROM tasks
            LEFT JOIN rooms ON tasks.room_id = rooms.id
            LEFT JOIN projects ON tasks.project_id = projects.id
            LEFT JOIN users ON tasks.assigned_user_id = users.id
            ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
            """
        ).fetchall()
    else:
        tasks = conn.execute(
            """
            SELECT tasks.*, rooms.name AS room_name, projects.name AS project_name, users.name AS assigned_user_name
            FROM tasks
            LEFT JOIN rooms ON tasks.room_id = rooms.id
            LEFT JOIN projects ON tasks.project_id = projects.id
            LEFT JOIN users ON tasks.assigned_user_id = users.id
            JOIN project_permissions ON project_permissions.project_id = tasks.project_id AND project_permissions.user_id = %s
            WHERE tasks.assigned_user_id = %s
            ORDER BY CASE WHEN tasks.status = 'open' THEN 0 ELSE 1 END, tasks.task_date DESC, tasks.created_at DESC
            """,
            (session.get("user_id"), session.get("user_id"))
        ).fetchall()
    conn.close()
    return render_template("tasks.html", tasks=tasks)


@app.route("/attendance/report")
@admin_required
def attendance_report():
    period = request.args.get("period", "day")
    selected_date = request.args.get("date") or local_now().date().isoformat()
    selected_user_id = request.args.get("user_id", type=int)
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
    query += " ORDER BY attendance_events.user_id, attendance_events.project_id, attendance_events.created_at"
    events = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    events = [e for e in events if attendance_event_in_range(e, period, selected_date)]
    pairs = build_attendance_pairs(events)
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
    return render_template(
        "attendance_report.html",
        users=users,
        pairs=pairs,
        summary=summary.values(),
        period=period,
        selected_date=selected_date,
        selected_user_id=selected_user_id,
        start=start,
        end=end,
        duration_text=duration_text,
        minutes_text=minutes_text,
        format_time=format_time,
        format_date=format_date
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
            conn.execute("UPDATE login_events SET is_read = TRUE WHERE is_read = FALSE AND event_type <> 'login'")
        else:
            conn.execute(
                "UPDATE login_events SET is_read = TRUE WHERE is_read = FALSE AND user_id = %s AND event_type = 'task_assigned'",
                (session.get("user_id"),)
            )
        conn.commit()
        flash("Notifications marked as read.")
    if is_main_admin():
        events = conn.execute("SELECT * FROM login_events WHERE event_type <> 'login' ORDER BY created_at DESC LIMIT 100").fetchall()
    else:
        events = conn.execute(
            "SELECT * FROM login_events WHERE user_id = %s AND event_type = 'task_assigned' ORDER BY created_at DESC LIMIT 100",
            (session.get("user_id"),)
        ).fetchall()
    conn.close()
    return render_template("notifications.html", events=events)


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
    for table in ["users", "projects", "rooms", "notes", "material_inventory", "attendance_events", "user_permissions", "project_permissions"]:
        tables[f"{table}.json"] = json.dumps(conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall(), indent=2, default=str)

    projects = conn.execute("SELECT blueprint_file, blueprint_preview_file FROM projects").fetchall()
    notes = conn.execute("SELECT photo_file, audio_file FROM notes WHERE photo_file IS NOT NULL OR audio_file IS NOT NULL").fetchall()
    material_pictures = conn.execute("SELECT picture_file FROM material_inventory WHERE picture_file IS NOT NULL").fetchall()
    conn.close()

    backup_name = f"blueprint_room_log_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    backup_path = os.path.join(tempfile.gettempdir(), backup_name)

    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, content in tables.items():
            z.writestr(filename, content)
        for p in projects:
            for key, folder in [("blueprint_file", "blueprints"), ("blueprint_preview_file", "blueprints/previews")]:
                if p.get(key):
                    data = download_storage_file(p[key])
                    if data:
                        z.writestr(f"{folder}/{os.path.basename(p[key])}", data)
        for n in notes:
            if n.get("photo_file"):
                data = download_storage_file(n["photo_file"])
                if data:
                    z.writestr(f"photos/{os.path.basename(n['photo_file'])}", data)
        for m in material_pictures:
            if m.get("picture_file"):
                data = download_storage_file(m["picture_file"])
                if data:
                    z.writestr(f"material_pictures/{os.path.basename(m['picture_file'])}", data)
            if n.get("audio_file"):
                data = download_storage_file(n["audio_file"])
                if data:
                    z.writestr(f"audio/{os.path.basename(n['audio_file'])}", data)
        z.writestr("README_BACKUP.txt", "Portable backup: JSON table exports plus uploaded files.")

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
        SELECT project_id FROM tasks WHERE completion_photo_file = %s OR completion_audio_file = %s
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
            storage_path
        )
    ).fetchone()
    if owner and not user_can_access_project(conn, owner["project_id"]):
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


try:
    init_db()
except Exception as e:
    print("Database initialization failed:", e)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
