from flask import Flask, render_template, request, redirect, url_for, send_from_directory, session, flash, jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, uuid, zipfile, tempfile
from datetime import datetime

try:
    import fitz  # PyMuPDF, used to convert PDF blueprint first page to PNG for phones
except Exception:
    fitz = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_SECRET_KEY")
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

DB = os.path.join(APP_DIR, "project_log.db")
ALLOWED_PHOTOS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_BLUEPRINTS = {"pdf", "png", "jpg", "jpeg", "webp"}


def file_ext(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def allowed_photo(filename):
    return file_ext(filename) in ALLOWED_PHOTOS


def allowed_blueprint(filename):
    return file_ext(filename) in ALLOWED_BLUEPRINTS


def is_pdf(filename):
    return file_ext(filename) == "pdf"


def create_pdf_preview(pdf_filename):
    """
    Convert the first page of a PDF blueprint to a PNG image.
    This makes blueprints visible on phones where embedded PDFs often fail.
    """
    if fitz is None:
        return None

    pdf_path = os.path.join(app.config["UPLOAD_FOLDER"], pdf_filename)
    preview_filename = f"{uuid.uuid4().hex}_blueprint_preview.png"
    preview_path = os.path.join(app.config["UPLOAD_FOLDER"], preview_filename)

    try:
        doc = fitz.open(pdf_path)
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        pix.save(preview_path)
        doc.close()
        return preview_filename
    except Exception:
        return None


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'worker',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        blueprint_file TEXT,
        blueprint_preview_file TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        x REAL NOT NULL,
        y REAL NOT NULL,
        w REAL NOT NULL,
        h REAL NOT NULL,
        polygon_points TEXT,
        category TEXT DEFAULT 'general',
        room_color TEXT DEFAULT 'blue',
        created_at TEXT NOT NULL,
        FOREIGN KEY(project_id) REFERENCES projects(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL,
        user_id INTEGER,
        note_date TEXT NOT NULL,
        comment TEXT NOT NULL,
        photo_file TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(room_id) REFERENCES rooms(id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    try:
        cur.execute("ALTER TABLE rooms ADD COLUMN polygon_points TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE rooms ADD COLUMN category TEXT DEFAULT 'general'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE rooms ADD COLUMN room_color TEXT DEFAULT 'blue'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE projects ADD COLUMN blueprint_preview_file TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.commit()

    # Create default admin user
    cur.execute("SELECT COUNT(*) AS c FROM users")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
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


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = db()
    projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("index.html", projects=projects)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        conn = db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
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
@login_required
def users():
    if session.get("role") != "admin":
        flash("Only admin can add users.")
        return redirect(url_for("index"))

    conn = db()

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        role = request.form["role"]

        try:
            conn.execute(
                "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, generate_password_hash(password), role, datetime.now().isoformat())
            )
            conn.commit()
            flash("User added.")
        except sqlite3.IntegrityError:
            flash("That email already exists.")

    all_users = conn.execute("SELECT id, name, email, role, created_at FROM users ORDER BY name").fetchall()
    conn.close()
    return render_template("users.html", users=all_users)


@app.route("/projects/new", methods=["GET", "POST"])
@login_required
def new_project():
    if request.method == "POST":
        name = request.form["name"].strip()
        file = request.files.get("blueprint")

        blueprint_file = None
        blueprint_preview_file = None
        if file and allowed_blueprint(file.filename):
            filename = secure_filename(file.filename)
            unique = f"{uuid.uuid4().hex}_{filename}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique))
            blueprint_file = unique

            if is_pdf(unique):
                blueprint_preview_file = create_pdf_preview(unique)
            else:
                blueprint_preview_file = unique

        conn = db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO projects (name, blueprint_file, blueprint_preview_file, created_at) VALUES (?, ?, ?, ?)",
            (name, blueprint_file, blueprint_preview_file, datetime.now().isoformat())
        )
        conn.commit()
        project_id = cur.lastrowid
        conn.close()
        return redirect(url_for("project", project_id=project_id))

    return render_template("new_project.html")


@app.route("/project/<int:project_id>")
@login_required
def project(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    rooms = conn.execute("SELECT * FROM rooms WHERE project_id = ?", (project_id,)).fetchall()
    conn.close()

    if not project:
        flash("Project not found.")
        return redirect(url_for("index"))

    return render_template("project.html", project=project, rooms=rooms)


@app.route("/project/<int:project_id>/rooms", methods=["POST"])
@login_required
def add_room(project_id):
    name = request.form["name"].strip()
    category = request.form.get("category", "general")
    room_color = request.form.get("room_color", "blue")
    polygon_points = request.form.get("polygon_points", "").strip()

    # Keep these old rectangle fields for compatibility, but polygon is now the main method.
    x = float(request.form.get("x") or 0)
    y = float(request.form.get("y") or 0)
    w = float(request.form.get("w") or 0)
    h = float(request.form.get("h") or 0)

    if not polygon_points:
        flash("Please trace the room before saving.")
        return redirect(url_for("project", project_id=project_id))

    conn = db()
    conn.execute(
        "INSERT INTO rooms (project_id, name, x, y, w, h, polygon_points, category, room_color, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, name, x, y, w, h, polygon_points, category, room_color, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    return redirect(url_for("project", project_id=project_id))


@app.route("/room/<int:room_id>", methods=["GET", "POST"])
@login_required
def room(room_id):
    conn = db()
    room = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if not room:
        conn.close()
        flash("Room not found.")
        return redirect(url_for("index"))

    project = conn.execute("SELECT * FROM projects WHERE id = ?", (room["project_id"],)).fetchone()

    if request.method == "POST":
        comment = request.form["comment"].strip()
        note_date = request.form["note_date"]
        file = request.files.get("photo")

        photo_file = None
        if file and file.filename and allowed_photo(file.filename):
            filename = secure_filename(file.filename)
            unique = f"{uuid.uuid4().hex}_{filename}"
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique))
            photo_file = unique

        conn.execute(
            "INSERT INTO notes (room_id, user_id, note_date, comment, photo_file, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (room_id, session.get("user_id"), note_date, comment, photo_file, datetime.now().isoformat())
        )
        conn.commit()
        flash("Comment/photo added.")

    selected_date = request.args.get("date", "")
    query = """
        SELECT notes.*, users.name AS user_name
        FROM notes
        LEFT JOIN users ON notes.user_id = users.id
        WHERE room_id = ?
    """
    params = [room_id]

    if selected_date:
        query += " AND note_date = ?"
        params.append(selected_date)

    query += " ORDER BY note_date DESC, created_at DESC"

    notes = conn.execute(query, params).fetchall()
    conn.close()

    return render_template("room.html", room=room, project=project, notes=notes, selected_date=selected_date)



@app.route("/project/<int:project_id>/timeline")
@login_required
def project_timeline(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        conn.close()
        flash("Project not found.")
        return redirect(url_for("index"))

    selected_date = request.args.get("date", "")
    query = """
        SELECT notes.*, rooms.name AS room_name, rooms.category AS room_category, users.name AS user_name
        FROM notes
        JOIN rooms ON notes.room_id = rooms.id
        LEFT JOIN users ON notes.user_id = users.id
        WHERE rooms.project_id = ?
    """
    params = [project_id]

    if selected_date:
        query += " AND notes.note_date = ?"
        params.append(selected_date)

    query += " ORDER BY notes.note_date DESC, notes.created_at DESC"

    notes = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("timeline.html", project=project, notes=notes, selected_date=selected_date)


@app.route("/backup")
@login_required
def backup():
    if session.get("role") != "admin":
        flash("Only admin can download backups.")
        return redirect(url_for("index"))

    backup_name = f"blueprint_room_log_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    backup_path = os.path.join(tempfile.gettempdir(), backup_name)

    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(DB):
            z.write(DB, "project_log.db")
        if os.path.exists(UPLOAD_DIR):
            for root, dirs, files in os.walk(UPLOAD_DIR):
                for filename in files:
                    full = os.path.join(root, filename)
                    arc = os.path.join("uploads", os.path.relpath(full, UPLOAD_DIR))
                    z.write(full, arc)

    return send_from_directory(tempfile.gettempdir(), backup_name, as_attachment=True)


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
