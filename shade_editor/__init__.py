"""Shade Designer - project-scoped window shade simulation.

Adapted from the standalone package for ProjectONus:
  * the document lives in Postgres (shade_documents) instead of SQLite
  * photos go to Supabase Storage like every other picture in the app, and the
    document only keeps a reference, so a project with 20 photos stays a small row
  * authorization uses the app's real login and project access rules
  * images are validated with PyMuPDF, which the app already ships, so no new
    build dependency is added

The blueprint is registered from app.py, which passes in the callbacks below so
this module never imports app.py back (no circular import).
"""
import base64
import io
import json
import math
import secrets

from flask import Blueprint, abort, jsonify, render_template, request, session

try:
    import fitz
except Exception:  # pragma: no cover - the app already depends on PyMuPDF
    fitz = None

MAX_BYTES = 25 * 1024 * 1024
MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_PHOTOS = 20
STORED_PREFIX = "shade:"
DATA_PREFIXES = (
    "data:image/jpeg;base64",
    "data:image/png;base64",
    "data:image/webp;base64",
)


def _number(n, lo=0.0, hi=1.0):
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not lo <= n <= hi:
        raise ValueError("Invalid coordinate or position")


def _point(p):
    if not isinstance(p, dict):
        raise ValueError("Invalid point")
    _number(p.get("x"))
    _number(p.get("y"))


def decode_photo_source(src):
    """Return (raw_bytes, content_type, extension) for a new data-URL picture."""
    prefix, data = src.split(",", 1)
    if prefix not in DATA_PREFIXES:
        raise ValueError("Use a JPG, PNG or WebP picture")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ValueError("Invalid picture") from exc
    if len(raw) > MAX_PHOTO_BYTES:
        raise ValueError("Picture exceeds 12 MB")
    if fitz is not None:
        try:
            pixmap = fitz.Pixmap(io.BytesIO(raw))
            if pixmap.width * pixmap.height > MAX_PIXELS:
                raise ValueError("Picture is too large")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Could not read this picture. Use a JPG or PNG.") from exc
    kind = prefix.split("/", 1)[1].split(";", 1)[0]
    extension = "jpg" if kind == "jpeg" else kind
    return raw, f"image/{kind}", extension


def validate_document(doc, store_photo=None, photo_url=None):
    """Check the document and convert picture sources for storage.

    store_photo(raw, content_type, extension) -> stored reference string.
    Already-stored pictures arrive as the URL handed out on load and are mapped
    straight back to their reference, so re-saving never re-uploads them.
    """
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise ValueError("Unsupported document format")
    photos = doc.get("photos")
    # An empty layout is allowed: the bundled sample picture is never saved into
    # a customer's project, so a brand new document legitimately has no photos.
    if not isinstance(photos, list) or len(photos) > MAX_PHOTOS:
        raise ValueError(f"Use up to {MAX_PHOTOS} pictures")

    clean = []
    for photo in photos:
        if not isinstance(photo, dict):
            raise ValueError("Invalid photo")
        name = photo.get("name")
        src = photo.get("src")
        if not isinstance(name, str) or not 1 <= len(name) <= 255:
            raise ValueError("Invalid picture name")
        if not isinstance(src, str) or not src:
            raise ValueError("Missing picture")

        known = photo_url and photo_url(src)
        if known:
            stored = known
        elif src.startswith(STORED_PREFIX):
            stored = src
        elif "," in src and src.split(",", 1)[0] in DATA_PREFIXES:
            raw, content_type, extension = decode_photo_source(src)
            stored = store_photo(raw, content_type, extension) if store_photo else src
        else:
            raise ValueError("Missing picture")

        panels = photo.get("panels")
        if not isinstance(panels, dict):
            raise ValueError("Missing panels")
        for layer in ("b", "s"):
            items = panels.get(layer)
            if not isinstance(items, list) or len(items) != 4:
                raise ValueError("Expected four panels per layer")
            for p in items:
                if not isinstance(p, dict) or not isinstance(p.get("on"), bool):
                    raise ValueError("Invalid panel")
                _number(p.get("drop"))
                if not isinstance(p.get("c"), list) or len(p["c"]) != 4:
                    raise ValueError("Expected four corners")
                for pt in p["c"]:
                    _point(pt)

        lines = photo.get("lines", [])
        if not isinstance(lines, list) or len(lines) > 2000:
            raise ValueError("Too many lines")
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("Invalid line")
            _point(line.get("a"))
            _point(line.get("b"))

        clean.append(dict(name=name, src=stored, panels=panels, lines=lines))
    return dict(schema_version=1, photos=clean)


def create_blueprint(store, authorize, project_name=None, photo_url=None):
    """store    - object with get(project_id) and save(project_id, document, revision)
    authorize   - authorize(project_id, write=False) -> bool
    project_name- project_name(project_id) -> str, for the page heading
    photo_url   - photo_url(reference_or_url) used both ways: it turns a stored
                  reference into a browser URL, and recognises that URL coming back.
    """
    bp = Blueprint(
        "shade_editor",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/shade-editor-assets",
    )

    def check(project_id, write=False):
        if not authorize(project_id, write=write):
            abort(403)

    def to_browser(document):
        if not document:
            return document
        for photo in document.get("photos", []):
            src = photo.get("src") or ""
            if src.startswith(STORED_PREFIX) and photo_url:
                photo["src"] = photo_url(src, to_url=True)
        return document

    @bp.get("/projects/<project_id>/shades")
    def editor(project_id):
        check(project_id)
        if "shade_csrf" not in session:
            session["shade_csrf"] = secrets.token_urlsafe(32)
        return render_template(
            "shade_editor/editor.html",
            project_id=project_id,
            csrf=session["shade_csrf"],
            project_title=project_name(project_id) if project_name else "",
            can_write=authorize(project_id, write=True),
        )

    @bp.get("/api/projects/<project_id>/shades")
    def read(project_id):
        check(project_id)
        saved = store.get(project_id)
        saved["document"] = to_browser(saved.get("document"))
        response = jsonify(saved)
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.put("/api/projects/<project_id>/shades")
    def save(project_id):
        check(project_id, write=True)
        token = session.get("shade_csrf")
        if not token or not secrets.compare_digest(token, request.headers.get("X-CSRF-Token", "")):
            abort(403)
        if request.content_length and request.content_length > MAX_BYTES:
            abort(413)
        raw = request.stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            abort(413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Expected object")
            revision = body.get("revision")
            if type(revision) is not int or revision < 0:
                raise ValueError("Invalid revision")
            document = validate_document(
                body.get("document"),
                store_photo=lambda data, content_type, ext: store.store_photo(
                    project_id, data, content_type, ext),
                photo_url=(lambda src: photo_url(src, to_url=False)) if photo_url else None,
            )
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(error=str(exc)), 400
        except Exception as exc:
            return jsonify(error=f"The pictures could not be stored. {exc}"), 500

        result = store.save(project_id, document, revision)
        if result is None:
            return jsonify(error="Someone saved a newer version. Reload before saving."), 409
        return jsonify(revision=result)

    return bp
