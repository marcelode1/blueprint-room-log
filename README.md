# Blueprint Room Log - Online Version

A starter Flask app for uploading a PDF or image blueprint, marking rooms, and saving room comments/photos by date.

## Default Login

Email: admin@example.com  
Password: admin123

Change this after testing.

## Run Locally

```bash
pip install -r requirements.txt
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Deploy on Render

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
gunicorn app:app
```

Environment Variable:

```text
SECRET_KEY = any long random password-style string
```

## Important Production Note

This simple version stores the SQLite database and uploaded photos on the server filesystem.
On many free hosting services, files can disappear after redeploys/restarts.

For real business use, upgrade to:
- PostgreSQL database
- Cloud photo storage such as Amazon S3, Backblaze B2, or Cloudinary
- Better user permissions
- Automatic backup


## File Support

Blueprint uploads:
- PDF
- JPG
- PNG
- WEBP

Room photo uploads:
- JPG
- PNG
- GIF
- WEBP

Note: iPhone HEIC photos may need conversion to JPG unless the phone/browser converts them automatically.

## Room Tracing

Rooms are now created by clicking around the room walls to create a polygon shape, instead of drawing only rectangles.

## Click Fix

The PDF/image blueprint now has pointer events disabled, so the SVG tracing layer receives mouse clicks correctly.

## Pro Upgrade Added

This version includes:

- Snap-to-wall/grid tracing for more precise room points
- Zoom and pan for large blueprints
- Room color coding:
  - General
  - Electrical
  - AV / Low Voltage
  - Lighting
- Project timeline view showing all room comments/photos by date
- Offline backup download for admin users

## Backup

Admin users can click "Download Backup" in the top menu.
This creates a ZIP file containing:
- project_log.db
- uploaded blueprints and photos

Save this ZIP offline for protection.

## Reliable Drawing Fix

This version captures drawing clicks from the entire blueprint viewport instead of depending on the PDF iframe/SVG to receive clicks. It also includes a Test Dot button to confirm drawing works.
