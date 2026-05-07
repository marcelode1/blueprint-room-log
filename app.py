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
ALLOWED_BLUEPRINTS = {"pdf", "png", "jpg", "jpeg", "webp"}


def file_ext(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def allowed_photo(filename):
    return file_ext(filename) in ALLOWED_PHOTOS


def allowed_blueprint(filename):
    return file_ext(filename) in ALLOWED_BLUEPRINTS


def allowed_audio(filename):
    return file_ext(filename) in ALLOWED_AUDIO


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
        "ALTER TABLE notes ADD COLUMN IF NOT EXISTS audio_file TEXT"
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


def can_add_notes():
    return session.get("role") in ["admin", "worker"]


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


@app.context_processor
def utility_processor():
    return dict(file_url=file_url, is_main_admin=is_main_admin, can_add_notes=can_add_notes)


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


@app.route("/project/<int:project_id>")
@login_required
def project(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    rooms = conn.execute("SELECT * FROM rooms WHERE project_id = %s ORDER BY id", (project_id,)).fetchall()
    conn.close()
    if not project:
        flash("Project not found.")
        return redirect(url_for("index"))
    return render_template("project.html", project=project, rooms=rooms)


@app.route("/project/<int:project_id>/rooms", methods=["POST"])
@admin_required
def add_room(project_id):
    polygon_points = request.form.get("polygon_points", "").strip()
    if not polygon_points:
        flash("Please trace the room before saving.")
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

    if request.method == "POST":
        if not can_add_notes():
            flash("You can view notes and photos, but you cannot add new ones.")
            return redirect(url_for("room", room_id=room_id))

        file = request.files.get("photo") or request.files.get("photo_camera")
        audio = request.files.get("audio")
        photo_file = upload_file_to_storage(file) if file and file.filename and allowed_photo(file.filename) else None
        audio_file = upload_file_to_storage(audio) if audio and audio.filename and allowed_audio(audio.filename) else None
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
    return render_template("room.html", room=room, project=project, notes=notes, selected_date=selected_date)


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
@admin_required
def delete_note(note_id):
    conn = db()
    note = conn.execute("SELECT notes.*, rooms.project_id FROM notes JOIN rooms ON notes.room_id = rooms.id WHERE notes.id = %s", (note_id,)).fetchone()
    if not note:
        conn.close()
        flash("Comment/photo not found.")
        return redirect(url_for("index"))

    room_id = note["room_id"]
    conn.execute("DELETE FROM notes WHERE id = %s", (note_id,))
    conn.commit()
    conn.close()
    flash("Comment/photo deleted.")
    return redirect(url_for("room", room_id=room_id))


@app.route("/backup")
@admin_required
def backup():

    conn = db()
    tables = {}
    for table in ["users", "projects", "rooms", "notes"]:
        tables[f"{table}.json"] = json.dumps(conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall(), indent=2, default=str)

    projects = conn.execute("SELECT blueprint_file, blueprint_preview_file FROM projects").fetchall()
    notes = conn.execute("SELECT photo_file, audio_file FROM notes WHERE photo_file IS NOT NULL OR audio_file IS NOT NULL").fetchall()
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


@app.route("/health")
def health():
    return "ok"


try:
    init_db()
except Exception as e:
    print("Database initialization failed:", e)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
