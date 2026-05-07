# Blueprint Room Log - Version 2 Supabase

Required Render environment variables:

DATABASE_URL = your Supabase PostgreSQL URI
SUPABASE_URL = your Supabase Project URL
SUPABASE_KEY = your Supabase anon public key
SUPABASE_BUCKET = blueprint-files
SECRET_KEY = any long random password-style string

Supabase Storage:
Create a public bucket named blueprint-files.

Render:
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app

Default login:
admin@example.com
admin123

This version stores project data in Supabase PostgreSQL and files in Supabase Storage. The backup button exports JSON data plus uploaded files for portability.


## PDF Display Fix

This version prevents raw PDF files from being loaded as an image.
If PDF-to-PNG preview conversion fails, the project page falls back to an iframe PDF view instead of showing a broken image.
For best phone support, upload a PDF after this version is deployed so the app can create a PNG preview.


## Storage Proxy Fix

This version serves Supabase Storage files through the app using /storage_file/.
It also adds a "Regenerate PDF Preview" button on PDF blueprint projects.
Use this if an uploaded PDF shows as a broken image or "something went wrong."


## High-Resolution PDF Viewer Fix

This version stops converting PDF blueprints on the Render server.
Instead, it renders the PDF in the user's browser using PDF.js at high resolution.
This keeps blueprint lines sharper and avoids Bad Gateway crashes from server memory limits.


## V3 Mobile/PWA + Permissions

- Browser cloud app for computer.
- Installable PWA-style app on Android from browser.
- Main admin only:
  - Create projects
  - Delete projects
  - Delete comments/photos/audio
  - Manage users
  - Download backup
- Workers can add room comments, pictures, and audio.
- Customers can view projects, rooms, comments, and photos.
- Project creation asks for customer name, address, phone, and email.
- Mobile project cards show only project name, customer name, and address.
- Room page supports phone camera upload and browser audio recording.


## V3 High Quality PDF Fix

- PDF blueprints now render in browser at 5x quality by default.
- Added PDF Quality selector: Normal, High, Ultra.
- Ultra gives the sharpest blueprint lines but may be heavier on older phones.
- This avoids Render server crashes because PDF rendering happens on the user's device.


## Blueprint Management + Safe PDF Preview

- Creating a project no longer requires a blueprint upload.
- Main admin can upload/replace the project blueprint later.
- Main admin can remove/delete the blueprint from the project.
- Added "Open Original Blueprint File" for full PDF quality.
- Replaced Ultra PDF rendering with safer capped PDF rendering to avoid black screens and browser crashes.
