-- ProjectONus Supabase RLS hardening
--
-- Run this in Supabase Dashboard > SQL Editor.
-- It enables Row Level Security on the public app tables reported by the
-- Supabase Security Advisor. No public read/write policies are added, so
-- anon/authenticated PostgREST access is blocked by default.
--
-- This app uses the Render/Flask backend database connection for app data.
-- Do not use FORCE ROW LEVEL SECURITY here; that could affect owner-level
-- backend maintenance connections.

do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'app_settings',
        'attendance_events',
        'comment_actions',
        'inventory_delete_codes',
        'inventory_items',
        'login_events',
        'material_inventory',
        'note_delete_codes',
        'notes',
        'project_blueprints',
        'project_delete_codes',
        'project_file_links',
        'project_file_permissions',
        'project_files',
        'project_permissions',
        'projects',
        'push_subscriptions',
        'room_delete_codes',
        'rooms',
        'suppliers',
        'task_attachment_delete_codes',
        'task_attachments',
        'task_delete_codes',
        'task_number_counters',
        'task_room_statuses',
        'task_supplier_items',
        'task_updates',
        'tasks',
        'user_permissions',
        'users',
        'worker_location_pings'
    ]
    loop
        execute format('alter table if exists public.%I enable row level security', table_name);
    end loop;
end $$;
