from flask import Flask, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os, uuid, zipfile, tempfile, json, mimetypes
import psycopg
from psycopg.rows import dict_row
from supabase import create_client

try:
    import fitz
except Exception:
    fitz = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "blueprint-files")

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
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
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
        blueprint_id INTEGER REFERENCES project_blueprints(id) ON DELETE SET NULL,
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

    conn.commit()

    # Safe migrations for older deployments
    migrations = [
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_name TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_address TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_phone TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS customer_email TEXT",
        "ALTER TABLE notes ADD COLUMN IF NOT EXISTS audio_file TEXT",
        "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS blueprint_id INTEGER REFERENCES project_blueprints(id) ON DELETE SET NULL",
        "CREATE TABLE IF NOT EXISTS project_blueprints (id SERIAL PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL, blueprint_file TEXT NOT NULL, blueprint_preview_file TEXT, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS login_events (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, user_name TEXT, user_email TEXT, role TEXT, event_type TEXT NOT NULL DEFAULT 'login', is_read BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS user_permissions (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, see_comments BOOLEAN NOT NULL DEFAULT TRUE, write_comments BOOLEAN NOT NULL DEFAULT FALSE, edit_comments BOOLEAN NOT NULL DEFAULT FALSE, delete_comments BOOLEAN NOT NULL DEFAULT FALSE, see_pictures BOOLEAN NOT NULL DEFAULT TRUE, add_pictures BOOLEAN NOT NULL DEFAULT FALSE, delete_pictures BOOLEAN NOT NULL DEFAULT FALSE, see_audio BOOLEAN NOT NULL DEFAULT TRUE, add_audio BOOLEAN NOT NULL DEFAULT FALSE, delete_audio BOOLEAN NOT NULL DEFAULT FALSE)"
    ]
    for sql in migrations:
        try:
            cur.execute(sql)
        except Exception as e:
            print("Migration skipped:", sql, e)
    conn.commit()

    cur.execute("SELECT COUNT(*) AS c FROM users")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (%s, %s, %s, %s, %s)",
            ("Admin", "admin@example.com", generate_password_hash("admin123"), "admin", datetime.now().isoformat())
        )
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
        if count == 0 and project.get("blueprint_file"):
            conn.execute(
                "INSERT INTO project_blueprints (project_id, name, blueprint_file, blueprint_preview_file, created_at) VALUES (%s, %s, %s, %s, %s)",
                (
                    project["id"],
                    "Main Blueprint",
                    project.get("blueprint_file"),
                    project.get("blueprint_preview_file"),
                    datetime.now().isoformat()
                )
            )
            conn.commit()
    except Exception as e:
        print("ensure_project_blueprints skipped:", e)


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
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
            "see_audio": True, "add_audio": True, "delete_audio": False,
        }
    return {
        "see_comments": True, "write_comments": False, "edit_comments": False, "delete_comments": False,
        "see_pictures": True, "add_pictures": False, "delete_pictures": False,
        "see_audio": True, "add_audio": False, "delete_audio": False,
    }


PERMISSION_KEYS = [
    "see_comments", "write_comments", "edit_comments", "delete_comments",
    "see_pictures", "add_pictures", "delete_pictures",
    "see_audio", "add_audio", "delete_audio"
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


def admin_unread_count():
    if session.get("role") != "admin":
        return 0
    try:
        conn = db()
        row = conn.execute("SELECT COUNT(*) AS c FROM login_events WHERE is_read = FALSE").fetchone()
        conn.close()
        return row["c"] if row else 0
    except Exception:
        return 0


def can_add_notes():
    return has_perm("write_comments") or has_perm("add_pictures") or has_perm("add_audio")


@app.context_processor
def utility_processor():
    return dict(
        file_url=file_url,
        is_main_admin=is_main_admin,
        can_add_notes=can_add_notes,
        has_perm=has_perm,
        get_app_setting=get_app_setting,
        admin_unread_count=admin_unread_count
    )


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    q = request.args.get("q", "").strip()
    conn = db()
    if q:
        like = f"%{q}%"
        projects = conn.execute(
            "SELECT * FROM projects WHERE name ILIKE %s OR customer_name ILIKE %s OR customer_address ILIKE %s ORDER BY created_at DESC",
            (like, like, like)
        ).fetchall()
    else:
        projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
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
    if q:
        like = f"%{q}%"
        projects = conn.execute(
            "SELECT * FROM projects WHERE name ILIKE %s OR customer_name ILIKE %s OR customer_address ILIKE %s ORDER BY created_at DESC",
            (like, like, like)
        ).fetchall()
    else:
        projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("mobile_home.html", projects=projects, q=q)



@app.route("/mobile/project/<int:project_id>/materials", methods=["GET", "POST"])
@login_required
def mobile_project_materials(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("mobile_home"))

    if request.method == "POST":
        if not can_add_notes():
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
    rooms = conn.execute("SELECT * FROM rooms WHERE project_id = %s ORDER BY id", (project_id,)).fetchall()
    conn.close()
    if not project:
        flash("Project not found.")
        return redirect(url_for("mobile_home"))
    return render_template("mobile_project.html", project=project, rooms=rooms)


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
    rooms = conn.execute("SELECT id, name FROM rooms WHERE project_id = %s ORDER BY id", (room["project_id"],)).fetchall()

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
    return render_template("mobile_room.html", room=room, project=project, rooms=rooms, notes=notes, selected_date=selected_date)


@app.route("/routes-check")
def routes_check():
    return "<h1>Projectonus Routes Active</h1><br>" + "<br>".join(sorted(str(r) for r in app.url_map.iter_rules()))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            try:
                conn = db()
                if user["role"] != "admin":
                    conn.execute(
                        "INSERT INTO login_events (user_id, user_name, user_email, role, event_type, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                        (user["id"], user["name"], user["email"], user["role"], "login", datetime.now().isoformat())
                    )
                    conn.commit()
                conn.close()
            except Exception as e:
                print("Login event insert failed:", e)
            return redirect(url_for("index"))
        flash("Invalid login.")
    return render_template("login.html")


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
            conn.execute(
                "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (%s, %s, %s, %s, %s)",
                (
                    request.form["name"].strip(),
                    request.form["email"].strip().lower(),
                    generate_password_hash(request.form["password"]),
                    request.form["role"],
                    datetime.now().isoformat()
                )
            )
            conn.commit()
            flash("User added.")
        except Exception:
            conn.rollback()
            flash("That email may already exist.")

    users = conn.execute("SELECT id, name, email, role, created_at FROM users ORDER BY name").fetchall()
    conn.close()
    return render_template("users.html", users=users)




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




@app.route("/project/<int:project_id>/materials", methods=["GET", "POST"])
@login_required
def project_materials(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))

    if request.method == "POST":
        if not is_main_admin() and session.get("role") not in ["worker", "admin"]:
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
    if not is_main_admin() and session.get("role") not in ["worker", "admin"]:
        flash("You do not have permission to update material status.")
        return redirect(url_for("project_materials", project_id=project_id))

    new_status = request.form.get("material_status", "not_in_stock")
    if new_status not in ["in_stock", "not_in_stock", "used"]:
        new_status = "not_in_stock"

    conn = db()
    conn.execute(
        "UPDATE material_inventory SET material_status = %s WHERE id = %s AND project_id = %s",
        (new_status, material_id, project_id)
    )
    conn.commit()
    conn.close()
    flash("Material status updated.")
    return redirect(url_for("project_materials", project_id=project_id))


@app.route("/project/<int:project_id>/materials/<int:material_id>/delete", methods=["POST"])
@admin_required
def delete_material(project_id, material_id):
    conn = db()
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
    blueprint_file = upload_bytes_to_storage(
        raw,
        file.filename,
        file.content_type or "application/octet-stream"
    )

    blueprint_preview_file = None if is_pdf(file.filename) else blueprint_file

    conn = db()
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
    return redirect(url_for("project", project_id=project_id, blueprint_id=new_bp["id"]))


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
@admin_required
def add_room(project_id):
    polygon_points = request.form.get("polygon_points", "").strip()
    blueprint_id = request.form.get("blueprint_id") or None

    if not polygon_points:
        flash("Please trace the room before saving.")
        if blueprint_id:
            return redirect(url_for("project", project_id=project_id, blueprint_id=blueprint_id))
        return redirect(url_for("project", project_id=project_id))

    conn = db()
    conn.execute(
        "INSERT INTO rooms (project_id, blueprint_id, name, x, y, w, h, polygon_points, category, room_color, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project_id,
            blueprint_id,
            request.form["name"].strip(),
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

    conn = db()
    conn.execute(
        "INSERT INTO rooms (project_id, name, x, y, w, h, polygon_points, category, room_color, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project_id,
            request.form["name"].strip(),
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
    project_rooms = conn.execute("SELECT id, name FROM rooms WHERE project_id = %s ORDER BY id", (room["project_id"],)).fetchall()

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
    return render_template("room.html", room=room, project=project, rooms=project_rooms, notes=notes, selected_date=selected_date)


@app.route("/project/<int:project_id>/timeline")
@login_required
def project_timeline(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
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



@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "logo":
            logo = request.files.get("company_logo")
            if logo and logo.filename and allowed_logo(logo.filename):
                logo_path = upload_file_to_storage(logo)
                set_app_setting("company_logo", logo_path)
                flash("Company logo updated.")
            else:
                flash("Please upload a valid logo file: PNG, JPG, WEBP, GIF, or SVG.")
        elif action == "permissions":
            user_id = int(request.form.get("user_id"))
            values = {k: (k in request.form) for k in PERMISSION_KEYS}
            conn.execute(
                """
                INSERT INTO user_permissions
                (user_id, see_comments, write_comments, edit_comments, delete_comments, see_pictures, add_pictures, delete_pictures, see_audio, add_audio, delete_audio)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    delete_audio = EXCLUDED.delete_audio
                """,
                (user_id, *[values[k] for k in PERMISSION_KEYS])
            )
            conn.commit()
            flash("User permissions updated.")
        return redirect(url_for("settings"))

    users = conn.execute("SELECT id, name, email, role FROM users ORDER BY name").fetchall()
    permissions = conn.execute("SELECT * FROM user_permissions").fetchall()
    conn.close()
    perm_map = {p["user_id"]: p for p in permissions}
    return render_template("settings.html", users=users, perm_map=perm_map, permission_keys=PERMISSION_KEYS)


@app.route("/notifications", methods=["GET", "POST"])
@admin_required
def notifications():
    conn = db()
    if request.method == "POST":
        conn.execute("UPDATE login_events SET is_read = TRUE WHERE is_read = FALSE")
        conn.commit()
        flash("Notifications marked as read.")
    events = conn.execute("SELECT * FROM login_events ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    return render_template("notifications.html", events=events)


@app.route("/note/<int:note_id>/edit", methods=["GET", "POST"])
@login_required
def edit_note(note_id):
    if not (is_main_admin() or has_perm("edit_comments")):
        flash("You do not have permission to edit comments.")
        return redirect(url_for("index"))
    conn = db()
    note = conn.execute("SELECT notes.*, rooms.name AS room_name FROM notes JOIN rooms ON notes.room_id = rooms.id WHERE notes.id = %s", (note_id,)).fetchone()
    if not note:
        conn.close()
        flash("Comment not found.")
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
    for table in ["users", "projects", "rooms", "notes", "material_inventory"]:
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
    data = download_storage_file(storage_path)
    if not data:
        return "File not found or storage permission denied.", 404

    mime_type = mimetypes.guess_type(storage_path)[0] or "application/octet-stream"
    return Response(data, mimetype=mime_type)


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
