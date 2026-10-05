-- Example defense-in-depth SELECT policy for a separate reporting role.
-- This file is not installed by the application. Runtime retrieval uses verified
-- JWT claims and a Qdrant ACL filter; it does not set PostgreSQL user context.
-- Table owners and BYPASSRLS roles ordinarily bypass these policies. Validate
-- role privileges and transaction-scoped settings before relying on this example.
-- A trusted caller would set app.user_id and app.user_groups per transaction.
-- Do not expose those settings as client-controlled authorization claims.

ALTER TABLE chunk_acl ENABLE ROW LEVEL SECURITY;

CREATE POLICY chunk_visibility ON chunk_acl
    FOR SELECT
    USING (
        acl::jsonb ?| ARRAY['*', 'user:' || current_setting('app.user_id', true)]
        OR acl::jsonb ?| (
            SELECT COALESCE(array_agg('group:' || g), ARRAY[]::text[])
            FROM unnest(string_to_array(current_setting('app.user_groups', true), ',')) AS g
        )
    );
