-- Run once with psql as the TeslaMate database owner/superuser, connected to that database.
-- Set a login password afterwards with psql's interactive \password mileaf_reader.
CREATE ROLE mileaf_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE :"DBNAME" TO mileaf_reader;
GRANT USAGE ON SCHEMA public TO mileaf_reader;
GRANT SELECT ON TABLE public.schema_migrations, public.settings, public.cars,
    public.states, public.positions, public.drives, public.charging_processes,
    public.charges, public.addresses, public.geofences, public.updates TO mileaf_reader;
-- v4.3.0 stores credentials in private.tokens. Never grant access to them.
REVOKE ALL PRIVILEGES ON TABLE private.tokens FROM mileaf_reader;
REVOKE ALL PRIVILEGES ON SCHEMA private FROM mileaf_reader;
ALTER ROLE mileaf_reader SET default_transaction_read_only = on;
