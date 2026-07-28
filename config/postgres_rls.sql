-- Defense in depth (Postgres only): Row-Level Security on the ACL mirror, so
-- even a direct SQL query — a buggy admin dashboard, a future engineer with a
-- psql session — respects the permission model.
--
-- Usage: run once against your Postgres DB, then have the app set
--   SET app.user_id = 'alice@company.com';
--   SET app.user_groups = 'eng,oncall';
-- per connection/transaction before querying chunk_acl directly.

ALTER TABLE chunk_acl ENABLE ROW LEVEL SECURITY;

CREATE POLICY chunk_visibility ON chunk_acl
    USING (
        acl::jsonb ?| ARRAY['*', 'user:' || current_setting('app.user_id', true)]
        OR acl::jsonb ?| (
            SELECT COALESCE(array_agg('group:' || g), ARRAY[]::text[])
            FROM unnest(string_to_array(current_setting('app.user_groups', true), ',')) AS g
        )
    );
