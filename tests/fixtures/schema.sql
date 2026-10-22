-- TeslaMate v4.3.0 read-only API projection, see docs/testing/fixtures.md.
-- Independently transcribed from upstream Ecto schemas and migrations; not a full database dump.
CREATE TYPE states_status AS ENUM ('online', 'offline', 'asleep');
CREATE TYPE unit_of_length AS ENUM ('km', 'mi');
CREATE TYPE unit_of_temperature AS ENUM ('C', 'F');
CREATE TYPE unit_of_pressure AS ENUM ('bar', 'psi');
CREATE TYPE range AS ENUM ('ideal', 'rated');
CREATE TYPE billing_type AS ENUM ('per_kwh', 'per_minute');
CREATE TABLE schema_migrations (version bigint PRIMARY KEY, inserted_at timestamp(0));
CREATE TABLE settings (
 id bigserial PRIMARY KEY, unit_of_length unit_of_length NOT NULL DEFAULT 'km',
 unit_of_temperature unit_of_temperature NOT NULL DEFAULT 'C',
 unit_of_pressure unit_of_pressure NOT NULL DEFAULT 'bar',
 preferred_range range NOT NULL DEFAULT 'rated', base_url varchar(255), grafana_url varchar(255),
 language text NOT NULL DEFAULT 'en', theme_mode text NOT NULL DEFAULT 'system',
 inserted_at timestamp(0) NOT NULL, updated_at timestamp(0) NOT NULL
);
CREATE TABLE car_settings (
 id bigserial PRIMARY KEY, suspend_min integer NOT NULL DEFAULT 21,
 suspend_after_idle_min integer NOT NULL DEFAULT 15, req_not_unlocked boolean NOT NULL DEFAULT false,
 free_supercharging boolean NOT NULL DEFAULT false, use_streaming_api boolean NOT NULL DEFAULT true,
 enabled boolean NOT NULL DEFAULT true, lfp_battery boolean NOT NULL DEFAULT false
);
CREATE TABLE cars (
 id smallserial PRIMARY KEY, eid bigint NOT NULL UNIQUE, vid bigint NOT NULL UNIQUE,
 vin text NOT NULL UNIQUE, name varchar(255), model varchar(255), efficiency double precision,
 trim_badging varchar(255), marketing_name varchar(255), exterior_color varchar(255),
 wheel_type varchar(255), spoiler_type varchar(255), display_priority smallint NOT NULL DEFAULT 1,
 settings_id bigint NOT NULL UNIQUE REFERENCES car_settings(id) ON DELETE CASCADE,
 inserted_at timestamp(0) NOT NULL, updated_at timestamp(0) NOT NULL
);
CREATE TABLE addresses (
 id serial PRIMARY KEY, display_name varchar(512), osm_id bigint, osm_type varchar(255),
 latitude numeric(8,6), longitude numeric(9,6), name varchar(255), house_number varchar(255),
 road varchar(255), neighbourhood varchar(255), city varchar(255), county varchar(255),
 postcode varchar(255), state varchar(255), state_district varchar(255), country varchar(255),
 raw jsonb, inserted_at timestamp(0) NOT NULL, updated_at timestamp(0) NOT NULL,
 UNIQUE(osm_id, osm_type)
);
CREATE TABLE geofences (
 id serial PRIMARY KEY, name varchar(255) NOT NULL, latitude numeric(8,6) NOT NULL,
 longitude numeric(9,6) NOT NULL, radius smallint NOT NULL DEFAULT 25,
 billing_type billing_type NOT NULL DEFAULT 'per_kwh', cost_per_unit numeric(9,4),
 session_fee numeric(14,2), inserted_at timestamp(0) NOT NULL, updated_at timestamp(0) NOT NULL
);
CREATE TABLE drives (
 id serial PRIMARY KEY, car_id smallint NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
 start_date timestamp(6) NOT NULL, end_date timestamp(6),
 outside_temp_avg numeric(4,1), inside_temp_avg numeric(4,1),
 speed_max smallint, power_max smallint, power_min smallint,
 start_ideal_range_km numeric(6,2), end_ideal_range_km numeric(6,2),
 start_rated_range_km numeric(6,2), end_rated_range_km numeric(6,2),
 start_km double precision, end_km double precision, distance double precision,
 duration_min smallint, ascent smallint, descent smallint,
 start_position_id integer, end_position_id integer,
 start_address_id integer REFERENCES addresses(id) ON DELETE SET NULL,
 end_address_id integer REFERENCES addresses(id) ON DELETE SET NULL,
 start_geofence_id integer REFERENCES geofences(id) ON DELETE SET NULL,
 end_geofence_id integer REFERENCES geofences(id) ON DELETE SET NULL
);
CREATE TABLE positions (
 id serial PRIMARY KEY, car_id smallint NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
 drive_id integer REFERENCES drives(id) ON DELETE SET NULL, date timestamp(6) NOT NULL,
 latitude numeric(8,6) NOT NULL, longitude numeric(9,6) NOT NULL,
 elevation smallint, speed smallint, power smallint, odometer double precision,
 ideal_battery_range_km numeric(6,2), est_battery_range_km numeric(6,2),
 rated_battery_range_km numeric(6,2), battery_level smallint, usable_battery_level smallint,
 battery_heater boolean, battery_heater_on boolean, battery_heater_no_power boolean,
 outside_temp numeric(4,1), inside_temp numeric(4,1), fan_status integer,
 driver_temp_setting numeric(4,1), passenger_temp_setting numeric(4,1), is_climate_on boolean,
 is_rear_defroster_on boolean, is_front_defroster_on boolean,
 tpms_pressure_fl numeric(4,1), tpms_pressure_fr numeric(4,1),
 tpms_pressure_rl numeric(4,1), tpms_pressure_rr numeric(4,1)
);
ALTER TABLE drives ADD FOREIGN KEY (start_position_id) REFERENCES positions(id) ON DELETE SET NULL;
ALTER TABLE drives ADD FOREIGN KEY (end_position_id) REFERENCES positions(id) ON DELETE SET NULL;
CREATE TABLE states (
 id serial PRIMARY KEY, car_id smallint NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
 state states_status NOT NULL, start_date timestamp(6) NOT NULL, end_date timestamp(6),
 CONSTRAINT positive_duration CHECK (end_date >= start_date)
);
CREATE UNIQUE INDEX "states_car_id__end_date_IS_NULL_index" ON states(car_id) WHERE end_date IS NULL;
CREATE TABLE charging_processes (
 id serial PRIMARY KEY, car_id smallint NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
 position_id integer NOT NULL REFERENCES positions(id), start_date timestamp(6) NOT NULL,
 end_date timestamp(6), charge_energy_added numeric(8,2), charge_energy_used numeric(8,2),
 start_ideal_range_km numeric(6,2), end_ideal_range_km numeric(6,2),
 start_rated_range_km numeric(6,2), end_rated_range_km numeric(6,2),
 start_battery_level smallint, end_battery_level smallint, duration_min smallint,
 outside_temp_avg numeric(4,1), cost numeric(14,2),
 address_id integer REFERENCES addresses(id) ON DELETE SET NULL,
 geofence_id integer REFERENCES geofences(id) ON DELETE SET NULL
);
CREATE TABLE charges (
 id serial PRIMARY KEY, charging_process_id integer NOT NULL REFERENCES charging_processes(id)
 ON DELETE CASCADE, date timestamp(6) NOT NULL, battery_heater boolean, battery_heater_on boolean,
 battery_heater_no_power boolean, battery_level smallint, usable_battery_level smallint,
 charge_energy_added numeric(8,2) NOT NULL, charger_actual_current smallint,
 charger_phases smallint, charger_pilot_current smallint, charger_power smallint NOT NULL,
 charger_voltage smallint, conn_charge_cable varchar(255), fast_charger_present boolean,
 fast_charger_brand varchar(255), fast_charger_type varchar(255),
 ideal_battery_range_km numeric(6,2) NOT NULL, rated_battery_range_km numeric(6,2),
 not_enough_power_to_heat boolean, outside_temp numeric(4,1),
 CONSTRAINT positive_charger_phases CHECK (charger_phases > 0)
);
CREATE TABLE updates (
 id serial PRIMARY KEY, car_id smallint NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
 start_date timestamp(6) NOT NULL, end_date timestamp(6), version varchar(255),
 CONSTRAINT positive_duration CHECK (end_date >= start_date)
);
-- Empty credential table exists only for permission probes; never populate it.
CREATE SCHEMA private;
CREATE TABLE private.tokens (
 id serial PRIMARY KEY, access bytea, refresh bytea,
 inserted_at timestamp(0) NOT NULL, updated_at timestamp(0) NOT NULL
);
