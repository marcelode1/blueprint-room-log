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
