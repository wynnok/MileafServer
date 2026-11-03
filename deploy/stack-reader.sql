\set ON_ERROR_STOP on
BEGIN;
SELECT NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mileaf_reader') AS create_reader \gset
\if :create_reader
\ir /bootstrap/mileaf-reader.sql
\endif
-- Read the generated secret inside the container, never from command arguments.
\getenv reader_password MILEAF_READER_PASSWORD
ALTER ROLE mileaf_reader PASSWORD :'reader_password';
SELECT NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_reader') AS create_grafana \gset
\if :create_grafana
CREATE ROLE grafana_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE :"DBNAME" TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON TABLE public.schema_migrations, public.settings, public.cars,
    public.states, public.positions, public.drives, public.charging_processes,
    public.charges, public.addresses, public.geofences, public.updates,
    public.car_settings TO grafana_reader;
ALTER ROLE grafana_reader SET default_transaction_read_only = on;
\endif
\getenv grafana_password GRAFANA_READER_PASSWORD
ALTER ROLE grafana_reader PASSWORD :'grafana_password';
-- Fail closed if a previously provisioned role has been elevated.
DO $$
DECLARE reader text;
BEGIN
    FOREACH reader IN ARRAY ARRAY['mileaf_reader', 'grafana_reader'] LOOP
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = reader
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
        OR EXISTS (SELECT FROM pg_auth_members
            WHERE member = (SELECT oid FROM pg_roles WHERE rolname = reader))
        OR has_table_privilege(reader, 'private.tokens', 'SELECT')
        OR has_schema_privilege(reader, 'public', 'CREATE')
        OR EXISTS (
            SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v')
                AND has_table_privilege(reader, c.oid,
                    'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        )
    THEN
        RAISE EXCEPTION '% has unexpected privileges; review before deployment', reader;
    END IF;
    END LOOP;
END $$;
COMMIT;
