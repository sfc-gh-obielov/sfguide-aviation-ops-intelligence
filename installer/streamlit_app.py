"""
Airport Analytics Installer (Snowflake Native)

A Streamlit app that generates and optionally executes customized SQL setup scripts.
Designed to run inside Snowflake Streamlit with access to the Snowpark session.

Usage (in Snowflake):
    CREATE STREAMLIT installer FROM @repo/branches/main MAIN_FILE = 'installer_snowflake.py';
"""

import streamlit as st
import pandas as pd
from datetime import datetime
import re
import subprocess
import os


_RE_SECRET_STRING = re.compile(r"(SECRET_STRING\s*=\s*)'[^']*'(\s*;)", flags=re.IGNORECASE)


def _mask_sql_secrets(sql_text: str) -> str:
    """
    Mask any inline SECRET_STRING literals so we don't display secrets in the Streamlit UI.
    Note: we still execute the real SQL (unmasked).
    """
    try:
        return _RE_SECRET_STRING.sub(r"\1'***REDACTED***'\2", sql_text or "")
    except Exception:
        return sql_text


def _normalize_git_repo_stage_base(stage_base: str) -> str:
    """Normalize a user-provided Git repo stage base.

    Expected format (either is fine):
      - @REPO_NAME/branches/<branch>
      - @DB.SCHEMA.REPO_NAME/branches/<branch>   (fully qualified)

    Key behavior:
    - Adds a leading '@' if missing.
    - Preserves DB.SCHEMA qualification (often required), because unqualified @REPO
      resolves in the *current* schema (e.g. AIRPORT_SAN.PUBLIC) and may not exist there.
    """
    s = (stage_base or "").strip()
    if not s:
        return "@AVIA_INSTALLER.PUBLIC.AVIA_FLEET_REPO/branches/poc-v5-adsb-tar-sql"
    if not s.startswith("@"):
        s = "@" + s

    return s.rstrip("/")
def _get_git_sha_short() -> str:
    """Best-effort git sha for install audit metadata."""
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"

# Try to get Snowflake session (only works in Snowflake Streamlit)
try:
    from snowflake.snowpark.context import get_active_session
    session = get_active_session()
    IN_SNOWFLAKE = True
except:
    session = None
    IN_SNOWFLAKE = False


# Page config
st.set_page_config(
    page_title="Airport Analytics Installer",
    page_icon="✈️",
    layout="wide"
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: 700;
        color: #1E3A5F;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        font-size: 1.1rem;
        color: #666;
        margin-bottom: 2rem;
    }
    .success-box {
        background-color: #d4edda;
        border: 1px solid #c3e6cb;
        border-radius: 5px;
        padding: 1rem;
        margin: 1rem 0;
    }
    .code-box {
        background-color: #f8f9fa;
        border: 1px solid #dee2e6;
        border-radius: 5px;
        padding: 1rem;
        font-family: monospace;
        white-space: pre-wrap;
        max-height: 400px;
        overflow-y: auto;
    }
</style>
""", unsafe_allow_html=True)


# ============================================================================
# AIRPORT DATA
# Installer is Snowflake-native: airport inventory is sourced from Overture Maps.
# ============================================================================


def _sql_escape_str(s: str) -> str:
    """Escape a Python string for embedding as a single-quoted SQL string literal."""
    return ("" if s is None else str(s)).replace("'", "''")


@st.cache_data
def load_airports():
    """Load airports from Overture Maps (Snowflake required)."""
    if not (IN_SNOWFLAKE and session):
        # No local fallback: this installer is intended to run inside Snowflake Streamlit.
        return pd.DataFrame()

    # User-requested simplified airport inventory query:
    # - Uses two FLATTENs + GROUP BY (acceptable for inventory list)
    # - Filters out Point geometries
    # - Returns id + English name + IATA/ICAO
    overture_q = """
    SELECT
        i.id AS AIRPORT_ID,
        COALESCE(
            MAX(IFF(n.value:"key"::STRING = 'en', NULLIF(TRIM(n.value:"value"::STRING), ''), NULL)),
            i.names:primary::STRING
        ) AS AIRPORT_NAME,
        COALESCE(
            MAX(IFF(LOWER(t.value:"key"::STRING) IN ('iata','iata_code','iata:code'), NULLIF(TRIM(t.value:"value"::STRING), ''), NULL)),
            ''
        ) AS AIRPORT_CODE_IATA,
        COALESCE(
            MAX(IFF(LOWER(t.value:"key"::STRING) IN ('icao','icao_code','icao:code'), NULLIF(TRIM(t.value:"value"::STRING), ''), NULL)),
            ''
        ) AS AIRPORT_CODE_ICAO
    FROM OVERTURE_MAPS__BASE.CARTO.INFRASTRUCTURE i
        , LATERAL FLATTEN(input => i.names:"common":"key_value", OUTER => TRUE) n
        , LATERAL FLATTEN(
            input => IFF(IS_OBJECT(i.source_tags), i.source_tags, TRY_PARSE_JSON(i.source_tags)):"key_value",
            OUTER => TRUE
        ) t
    WHERE i.class ILIKE '%international_airport%'
      AND i.subtype ILIKE '%airport%'
      AND ST_ASGEOJSON(i.geometry):type::STRING <> 'Point'
    GROUP BY i.id, i.names:primary::STRING
    HAVING COALESCE(
        MAX(IFF(n.value:"key"::STRING = 'en', NULLIF(TRIM(n.value:"value"::STRING), ''), NULL)),
        i.names:primary::STRING
    ) IS NOT NULL
    ORDER BY AIRPORT_NAME
    LIMIT 5000
    """
    try:
        df = session.sql(overture_q).to_pandas()
        if df is not None and not df.empty and 'AIRPORT_ID' in df.columns:
            st.sidebar.success(f"✅ Loaded {len(df)} airports from Overture Maps")
            return df
        return pd.DataFrame()
    except Exception as e:
        st.sidebar.error(f"Failed to load airports from Overture Maps: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600)
def load_airport_geometry_by_id(airport_id: str):
    """Fetch airport geometry + centroid from Overture Maps by record id."""
    if not (IN_SNOWFLAKE and session) or not airport_id:
        return None
    q = f"""
    SELECT
      TO_VARCHAR(ST_ASGEOJSON(i.geometry)) AS geometry_json_str,
      ST_Y(ST_CENTROID(i.geometry)) AS center_lat,
      ST_X(ST_CENTROID(i.geometry)) AS center_lon
    FROM OVERTURE_MAPS__BASE.CARTO.INFRASTRUCTURE i
    WHERE i.id = '{_sql_escape_str(airport_id)}'
      AND i.class ILIKE '%international_airport%'
      AND i.subtype ILIKE '%airport%'
      AND ST_ASGEOJSON(i.geometry):type::STRING <> 'Point'
    LIMIT 1
    """
    try:
        df = session.sql(q).to_pandas()
        if df is None or df.empty:
            return None
        row = df.iloc[0].to_dict()
        return {
            "GEOMETRY": row.get("GEOMETRY_JSON_STR"),
            "CENTER_LAT": row.get("CENTER_LAT"),
            "CENTER_LON": row.get("CENTER_LON"),
        }
    except Exception:
        return None


# ============================================================================
# SQL TEMPLATE GENERATORS
# ============================================================================

def generate_base_sql(airport: dict, database: str, schema: str, warehouse: str, git_repo_stage_base: str) -> str:
    """Generate base infrastructure SQL."""
    airport_name_sql = _sql_escape_str(airport.get("name"))
    airport_iata_sql = _sql_escape_str(airport.get("iata_code"))
    airport_icao_sql = _sql_escape_str(airport.get("icao_code"))
    airport_id_sql = _sql_escape_str(airport.get("airport_id"))
    return f"""-- =============================================================================
-- BASE INFRASTRUCTURE FOR {airport['name']} ({airport['iata_code']})
-- Database: {database}.{schema}
-- Generated: {datetime.utcnow().isoformat()}
-- =============================================================================

-- Create database and schema
CREATE DATABASE IF NOT EXISTS {database};
CREATE SCHEMA IF NOT EXISTS {database}.{schema};

-- Grant usage (adjust roles as needed)
GRANT USAGE ON DATABASE {database} TO ROLE PUBLIC;
GRANT USAGE ON SCHEMA {database}.{schema} TO ROLE PUBLIC;

-- -----------------------------------------------------------------------------
-- 1. PROPERTIES_AIRPORT
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE {database}.{schema}.PROPERTIES_AIRPORT AS
WITH g AS (
  SELECT i.geometry AS geometry
  FROM OVERTURE_MAPS__BASE.CARTO.INFRASTRUCTURE i
  WHERE i.id = '{airport_id_sql}'
    AND i.class ILIKE '%international_airport%'
    AND i.subtype ILIKE '%airport%'
    AND ST_ASGEOJSON(i.geometry):type::STRING <> 'Point'
  LIMIT 1
)
SELECT
  '{airport_name_sql}'::STRING AS airport_name,
  '{airport_iata_sql}'::STRING AS airport_code,
  '{airport_icao_sql}'::STRING AS airport_icao,
  g.geometry AS geometry,
  ST_YMIN(g.geometry) AS min_lat,
  ST_YMAX(g.geometry) AS max_lat,
  ST_XMIN(g.geometry) AS min_lon,
  ST_XMAX(g.geometry) AS max_lon,
  ST_Y(ST_CENTROID(g.geometry)) AS center_lat,
  ST_X(ST_CENTROID(g.geometry)) AS center_lon
FROM g;

-- -----------------------------------------------------------------------------
-- 2. PROPERTIES_INFRASTRUCTURE (all Overture infrastructure intersecting airport)
-- Stores all infrastructure objects for flexible filtering and metadata queries.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE {database}.{schema}.PROPERTIES_INFRASTRUCTURE AS
WITH airport AS (
  SELECT geometry
  FROM {database}.{schema}.PROPERTIES_AIRPORT
  LIMIT 1
),
raw_infra AS (
  SELECT
    o.id,
    o.class,
    o.subtype,
    o.names:primary::STRING AS primary_name,
    o.names AS names,
    IFF(IS_OBJECT(o.source_tags), o.source_tags, TRY_PARSE_JSON(o.source_tags)) AS source_tags,
    o.geometry,
    ST_ASGEOJSON(o.geometry):type::STRING AS geometry_type
  FROM OVERTURE_MAPS__BASE.CARTO.INFRASTRUCTURE o
  INNER JOIN airport a
    ON ST_INTERSECTS(o.geometry, a.geometry)
),
-- Flatten source_tags for common OSM keys used in airport infrastructure
tags_flat AS (
  SELECT
    r.id,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'aeroway', kv.value:"value"::STRING, NULL)) AS osm_aeroway,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'ref', kv.value:"value"::STRING, NULL)) AS osm_ref,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'name', kv.value:"value"::STRING, NULL)) AS osm_name,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'width', TRY_TO_DOUBLE(kv.value:"value"::STRING), NULL)) AS osm_width,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'surface', kv.value:"value"::STRING, NULL)) AS osm_surface,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'length', TRY_TO_DOUBLE(kv.value:"value"::STRING), NULL)) AS osm_length,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'building', kv.value:"value"::STRING, NULL)) AS osm_building,
    MAX(IFF(LOWER(kv.value:"key"::STRING) = 'landuse', kv.value:"value"::STRING, NULL)) AS osm_landuse
  FROM raw_infra r
  , LATERAL FLATTEN(input => r.source_tags:"key_value", OUTER => TRUE) kv
  GROUP BY r.id
)
SELECT
  r.id AS infrastructure_id,
  r.class,
  r.subtype,
  r.primary_name,
  t.osm_aeroway,
  t.osm_ref,
  t.osm_name,
  t.osm_width,
  t.osm_surface,
  t.osm_length,
  t.osm_building,
  t.osm_landuse,
  r.names AS names_json,
  r.source_tags AS source_tags_json,
  r.geometry_type,
  r.geometry
FROM raw_infra r
LEFT JOIN tags_flat t ON r.id = t.id;

-- -----------------------------------------------------------------------------
-- GET_OSM_TAG UDF: Retrieve any OSM tag from source_tags_json by key
-- Usage: GET_OSM_TAG(source_tags_json, 'operator')
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION {database}.{schema}.GET_OSM_TAG(source_tags VARIANT, tag_key STRING)
RETURNS STRING
LANGUAGE SQL
AS $$
  SELECT MAX(f.value:"value"::STRING)
  FROM TABLE(FLATTEN(input => source_tags:"key_value", OUTER => TRUE)) f
  WHERE LOWER(f.value:"key"::STRING) = LOWER(tag_key)
$$;

-- -----------------------------------------------------------------------------
-- 3. PROPERTIES_GATES (Gate points from Overture Maps Infrastructure)
-- In Overture, airport gates are typically POINT features (OSM aeroway=gate).
-- Gate names are frequently stored in `source_tags.key_value[]` under key="ref".
-- Snowflake cannot GROUP BY GEOGRAPHY/GEOMETRY, so we aggregate on ST_ASGEOJSON(geometry).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE {database}.{schema}.PROPERTIES_GATES AS
WITH airport AS (
  SELECT geometry
  FROM {database}.{schema}.PROPERTIES_AIRPORT
  LIMIT 1
),
gates AS (
  SELECT
    i.id,
    ST_ASGEOJSON(i.geometry) AS gate_geojson,
    i.names:primary::STRING AS primary_name,
    IFF(
      IS_OBJECT(i.source_tags),
      i.source_tags,
      TRY_PARSE_JSON(i.source_tags)
    ) AS tags
  FROM OVERTURE_MAPS__BASE.CARTO.INFRASTRUCTURE i
  JOIN airport a
    ON ST_DWITHIN(a.geometry, i.geometry, 2000)
  WHERE i.class ILIKE '%airport_gate%'
),
tags_kv AS (
  SELECT
    g.id,
    g.gate_geojson,
    g.primary_name,
    kv.value:"key"::STRING AS k,
    kv.value:"value"::STRING AS v
  FROM gates g
  , LATERAL FLATTEN(input => g.tags:"key_value", OUTER => TRUE) kv
),
picked AS (
  SELECT
    id,
    gate_geojson,
    MAX(NULLIF(TRIM(primary_name), '')) AS primary_name_any,
    MAX(IFF(LOWER(k) = 'ref',      NULLIF(TRIM(v), ''), NULL)) AS ref_value,
    MAX(IFF(LOWER(k) = 'ref:gate', NULLIF(TRIM(v), ''), NULL)) AS ref_gate_value,
    MAX(IFF(LOWER(k) = 'gate_ref', NULLIF(TRIM(v), ''), NULL)) AS gate_ref_value,
    MAX(IFF(LOWER(k) = 'name',     NULLIF(TRIM(v), ''), NULL)) AS name_value
  FROM tags_kv
  GROUP BY 1,2
)
SELECT
  id AS gate_id,
  COALESCE(primary_name_any, ref_value, ref_gate_value, gate_ref_value, name_value, id) AS gate_name,
  TRY_TO_GEOGRAPHY(gate_geojson) AS gate_geom
FROM picked
WHERE gate_geojson IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 4. PROPERTIES_RUNWAYS (from Overture Maps Infrastructure)
-- -----------------------------------------------------------------------------
-- NOTE: Runway Crossings logic relies on an "inside runway" signal.
-- A LINESTRING runway rarely intersects noisy ADS-B points, so we store a runway *corridor area*
-- by buffering the runway centerline to a wider corridor.
--
-- Buffer width:
-- - If Overture `source_tags` contains a numeric `width` tag for a runway segment, we use (width_m / 2) as buffer radius.
--   (Width is full runway width; buffer radius expands on both sides of the centerline.)
-- - If width is missing/invalid, we fall back to a conservative default radius (30m).
-- Snowflake buffers GEOMETRY in meters, so we:
--   GEOGRAPHY -> GEOMETRY (WGS84) -> EPSG:3857 -> ST_BUFFER(radius_m) -> EPSG:4326 -> GEOGRAPHY
--
-- Split into 3 statements to reduce compilation time (Overture union, then buffer, then transform+store).
-- IMPORTANT: In Snowflake Streamlit execution context, `CREATE TEMPORARY TABLE` can be unsupported.
-- We use normal tables with a `TEMP_` prefix instead.

-- 1) Extract runway segments + width-derived buffer radius (meters)
CREATE OR REPLACE TABLE {database}.{schema}.TEMP_RUNWAY_SEGMENTS AS
WITH airport AS (
  SELECT geometry
  FROM {database}.{schema}.PROPERTIES_AIRPORT
  LIMIT 1
),
runways AS (
  SELECT
    i.id,
    i.geometry AS runway_geog,
    IFF(IS_OBJECT(i.source_tags), i.source_tags, TRY_PARSE_JSON(i.source_tags)) AS tags
  FROM OVERTURE_MAPS__BASE.CARTO.INFRASTRUCTURE i
  JOIN airport a
    ON ST_INTERSECTS(a.geometry, i.geometry)
  WHERE (i.subtype ILIKE 'runway' OR i.class ILIKE 'runway' OR i.source_tags ILIKE '%runway%')
    AND i.geometry IS NOT NULL
),
tags_kv AS (
  SELECT
    r.id,
    kv.value:"key"::STRING AS k,
    kv.value:"value"::STRING AS v
  FROM runways r
  , LATERAL FLATTEN(input => r.tags:"key_value", OUTER => TRUE) kv
),
widths AS (
  SELECT
    id,
    MAX(IFF(LOWER(k) = 'width', TRY_TO_DOUBLE(v), NULL)) AS width_m
  FROM tags_kv
  GROUP BY 1
)
SELECT
  r.id,
  r.runway_geog,
  w.width_m,
  COALESCE(w.width_m / 2.0, 30.0) AS buffer_radius_m
FROM runways r
LEFT JOIN widths w USING (id);

-- 1b) Convert runway segments to EPSG:3857 GEOMETRY (meters) once (reduces nested geospatial expressions later)
CREATE OR REPLACE TABLE {database}.{schema}.TEMP_RUNWAY_GEOM_3857 AS
SELECT
  id AS runway_id,
  ST_TRANSFORM(TO_GEOMETRY(runway_geog), 3857) AS runway_geom_3857,
  buffer_radius_m
FROM {database}.{schema}.TEMP_RUNWAY_SEGMENTS
WHERE runway_geog IS NOT NULL;

-- 2) Buffer all runway segments in meters (EPSG:3857)
-- NOTE: Some Snowflake accounts do not support ST_UNION_AGG over GEOMETRY, so we
-- keep buffered pieces as GEOMETRY here and union them later as GEOGRAPHY.
CREATE OR REPLACE TABLE {database}.{schema}.TEMP_RUNWAY_BUFFER_3857 AS
SELECT
  runway_id,
  ST_BUFFER(runway_geom_3857, COALESCE(buffer_radius_m, 30.0)) AS runway_buffer_3857
FROM {database}.{schema}.TEMP_RUNWAY_GEOM_3857
WHERE runway_geom_3857 IS NOT NULL;

-- 3) Reproject back to 4326 and store as GEOGRAPHY (runway corridor area)
CREATE OR REPLACE TABLE {database}.{schema}.PROPERTIES_RUNWAYS AS
SELECT
  -- Use a simple stable row id; if we later split into multiple polygons, we'll re-number them.
  'RWY_001' AS runway_id,
  -- Union as GEOGRAPHY (supported), after transforming buffered GEOMETRY back to WGS84.
  ST_UNION_AGG(TO_GEOGRAPHY(ST_TRANSFORM(runway_buffer_3857, 4326))) AS runway_geog
FROM {database}.{schema}.TEMP_RUNWAY_BUFFER_3857;

-- 4) If runway corridor is a MULTIPOLYGON, split into one polygon per row.
-- Snowflake doesn't expose a built-in "dump parts" function in all accounts, so we use
-- a small JS table function that expands GeoJSON Polygon/MultiPolygon into Polygon rows.
-- NOTE: the installer splits statements on `;`, so the JS body must NOT contain semicolons.
CREATE OR REPLACE FUNCTION {database}.{schema}.ST_GETPOLYGONS(G OBJECT)
RETURNS TABLE (POLYGON OBJECT)
LANGUAGE JAVASCRIPT
AS '
{{
processRow: function split_multipolygon(row, rowWriter, context){{
    var geojson = row.G
    var polygons = []
    if (!geojson) return
    if (geojson.type === \"Polygon\") polygons.push(geojson.coordinates)
    else if (geojson.type === \"MultiPolygon\") {{
        for (var i = 0; i < geojson.coordinates.length; i++) polygons.push(geojson.coordinates[i])
    }}
    for (var j = 0; j < polygons.length; j++) {{
        rowWriter.writeRow({{POLYGON: {{\"type\":\"Polygon\",\"coordinates\": polygons[j]}}}})
    }}
}}
}}
';

CREATE OR REPLACE TABLE {database}.{schema}.TEMP_RUNWAY_POLYGONS AS
SELECT
  CONCAT('RWY_', LPAD(TO_VARCHAR(ROW_NUMBER() OVER (ORDER BY TO_VARCHAR(p.POLYGON))), 3, '0')) AS runway_id,
  TO_GEOGRAPHY(p.POLYGON) AS runway_geog
FROM {database}.{schema}.PROPERTIES_RUNWAYS r,
TABLE ({database}.{schema}.ST_GETPOLYGONS(ST_ASGEOJSON(r.runway_geog))) p
WHERE r.runway_geog IS NOT NULL;

CREATE OR REPLACE TABLE {database}.{schema}.PROPERTIES_RUNWAYS AS
SELECT runway_id, runway_geog
FROM {database}.{schema}.TEMP_RUNWAY_POLYGONS
WHERE runway_geog IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 5. HELPER_AIRLINE_DIM (standing airline reference)
-- -----------------------------------------------------------------------------
-- NOTE: we load `installer/airlines.csv` via SQL from the Git repo stage
-- (e.g., @sd_poc_repo/branches/<branch>/installer/airlines.csv).
-- This keeps the install fully SQL-based (no Python-side file loading).
CREATE OR REPLACE TABLE {database}.{schema}.HELPER_AIRLINE_DIM (
  AIRLINE_ID INT,
  AIRLINE_NAME STRING,
  AIRLINE_IATA STRING,
  AIRLINE_ICAO STRING,
  AIRLINE_CALLSIGN STRING,
  COUNTRY STRING,
  IS_ACTIVE STRING
);

-- CSV file format for airline dim
CREATE OR REPLACE FILE FORMAT {database}.{schema}.FF_AIRLINES_CSV
  TYPE = CSV
  FIELD_DELIMITER = ','
  SKIP_HEADER = 1
  FIELD_OPTIONALLY_ENCLOSED_BY = '\"'
  NULL_IF = ('\\N', 'N/A', '-', '');

-- Load from Git repository stage.
-- NOTE: Snowflake does NOT support `COPY INTO <table>` directly from a Git Repository stage.
-- Instead we read the CSV via a SELECT from the stage and INSERT into the table.
TRUNCATE TABLE {database}.{schema}.HELPER_AIRLINE_DIM;

INSERT INTO {database}.{schema}.HELPER_AIRLINE_DIM (
  AIRLINE_ID, AIRLINE_NAME, AIRLINE_IATA, AIRLINE_ICAO, AIRLINE_CALLSIGN, COUNTRY, IS_ACTIVE
)
SELECT
  TRY_TO_NUMBER(t.$1)::INT AS airline_id,
  t.$2::STRING AS airline_name,
  t.$3::STRING AS airline_iata,
  t.$4::STRING AS airline_icao,
  t.$5::STRING AS airline_callsign,
  t.$6::STRING AS country,
  t.$7::STRING AS is_active
FROM {git_repo_stage_base}/installer/airlines.csv
  (FILE_FORMAT => {database}.{schema}.FF_AIRLINES_CSV) t;

-- -----------------------------------------------------------------------------
-- 7. HELPER_AIRLINE_IATA_ICAO_MAP (IATA↔ICAO translation for callsign matching)
-- -----------------------------------------------------------------------------
-- Purpose: Allow matching ADS-B callsigns that use ICAO prefix (SKW3481) with
-- schedule data that uses IATA prefix (OO3481), and vice versa.
CREATE OR REPLACE TABLE {database}.{schema}.HELPER_AIRLINE_IATA_ICAO_MAP AS
SELECT
  UPPER(TRIM(AIRLINE_IATA)) AS airline_iata,
  UPPER(TRIM(AIRLINE_ICAO)) AS airline_icao,
  MAX(AIRLINE_NAME) AS airline_name
FROM {database}.{schema}.HELPER_AIRLINE_DIM
WHERE AIRLINE_IATA IS NOT NULL
  AND TRIM(AIRLINE_IATA) <> ''
  AND AIRLINE_ICAO IS NOT NULL
  AND TRIM(AIRLINE_ICAO) <> ''
  AND LENGTH(TRIM(AIRLINE_IATA)) IN (2,3)
  AND LENGTH(TRIM(AIRLINE_ICAO)) IN (2,3)
GROUP BY 1, 2;

-- -----------------------------------------------------------------------------
-- 8. FLIGHT_SCHEDULE tables (always created, even without API key)
-- -----------------------------------------------------------------------------
-- Raw schedule table (Bronze layer)
CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_FLIGHT_SCHEDULE_RAW (
    flight_date DATE,
    flight_status VARCHAR(32),
    departure_airport VARCHAR(8),
    departure_scheduled TIMESTAMP_NTZ,
    departure_estimated TIMESTAMP_NTZ,
    departure_actual TIMESTAMP_NTZ,
    departure_delay INT,
    departure_terminal VARCHAR(8),
    departure_gate VARCHAR(8),
    arrival_airport VARCHAR(8),
    arrival_scheduled TIMESTAMP_NTZ,
    arrival_estimated TIMESTAMP_NTZ,
    arrival_actual TIMESTAMP_NTZ,
    arrival_delay INT,
    arrival_terminal VARCHAR(8),
    arrival_gate VARCHAR(8),
    airline_name VARCHAR(128),
    airline_iata VARCHAR(8),
    airline_icao VARCHAR(8),
    flight_number VARCHAR(16),
    flight_iata VARCHAR(16),
    flight_icao VARCHAR(16),
    aircraft_registration VARCHAR(16),
    aircraft_iata VARCHAR(8),
    aircraft_icao VARCHAR(8),
    codeshared_airline VARCHAR(128),
    codeshared_flight_iata VARCHAR(16),
    raw_json VARIANT,
    ingested_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Canonical schedule table (Silver layer)
CREATE TABLE IF NOT EXISTS {database}.{schema}.FLIGHT_SCHEDULE (
    FLIGHT_KEY VARCHAR(128),
    FLIGHT_DATE DATE,
    FLIGHT_STATUS VARCHAR(32),
    DEPARTURE_AIRPORT VARCHAR(8),
    ARRIVAL_AIRPORT VARCHAR(8),
    DEPARTURE_SCHEDULED TIMESTAMP_NTZ,
    DEPARTURE_ESTIMATED TIMESTAMP_NTZ,
    DEPARTURE_ACTUAL TIMESTAMP_NTZ,
    DEPARTURE_DELAY INT,
    DEPARTURE_TERMINAL VARCHAR(8),
    DEPARTURE_GATE VARCHAR(8),
    ARRIVAL_SCHEDULED TIMESTAMP_NTZ,
    ARRIVAL_ESTIMATED TIMESTAMP_NTZ,
    ARRIVAL_ACTUAL TIMESTAMP_NTZ,
    ARRIVAL_DELAY INT,
    ARRIVAL_TERMINAL VARCHAR(8),
    ARRIVAL_GATE VARCHAR(8),
    AIRLINE_NAME VARCHAR(128),
    AIRLINE_IATA VARCHAR(8),
    AIRLINE_ICAO VARCHAR(8),
    FLIGHT_NUMBER VARCHAR(16),
    FLIGHT_IATA VARCHAR(16),
    FLIGHT_ICAO VARCHAR(16),
    AIRCRAFT_REGISTRATION VARCHAR(16),
    IS_CODESHARE BOOLEAN,
    CREATED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- Verify
SELECT 'PROPERTIES_AIRPORT' AS tbl, COUNT(*) AS cnt FROM {database}.{schema}.PROPERTIES_AIRPORT
UNION ALL SELECT 'PROPERTIES_INFRASTRUCTURE', COUNT(*) FROM {database}.{schema}.PROPERTIES_INFRASTRUCTURE
UNION ALL SELECT 'PROPERTIES_GATES', COUNT(*) FROM {database}.{schema}.PROPERTIES_GATES
UNION ALL SELECT 'PROPERTIES_RUNWAYS', COUNT(*) FROM {database}.{schema}.PROPERTIES_RUNWAYS
UNION ALL SELECT 'HELPER_FLIGHT_SCHEDULE_RAW', COUNT(*) FROM {database}.{schema}.HELPER_FLIGHT_SCHEDULE_RAW
UNION ALL SELECT 'FLIGHT_SCHEDULE', COUNT(*) FROM {database}.{schema}.FLIGHT_SCHEDULE;
"""


def generate_adsb_sql(airport: dict, database: str, schema: str, warehouse: str, adsb_history_backfill_days: int = 5) -> str:
    """Generate ADS-B ingestion SQL."""
    adsb_history_backfill_days = int(adsb_history_backfill_days or 0)
    # Pre-compute values for embedding in Python procedure code
    adsb_raw_table = f"{database}.{schema}.HELPER_ADSB_LOL_RAW"
    # External Access Integrations are ACCOUNT-level objects. Name them per-airport to avoid collisions
    # when multiple airport DBs are installed in the same Snowflake account.
    eai_adsb_lol = re.sub(r"[^A-Za-z0-9_]", "_", f"{database}_{schema}_ADSB_LOL_EAI").upper()
    eai_github = re.sub(r"[^A-Za-z0-9_]", "_", f"{database}_{schema}_GITHUB_EAI").upper()
    # Convert 50km to Nautical Miles (50 / 1.852 = 27) - larger radius to catch aircraft on approach
    radius_nm = 27
    api_url = f"https://api.adsb.lol/v2/point/{airport['lat']}/{airport['lon']}/{radius_nm}"
    
    return f"""-- =============================================================================
-- ADS-B INGESTION FOR {airport['name']} ({airport['iata_code']})
-- Database: {database}.{schema}
-- Source: adsb.lol API
-- =============================================================================

-- -----------------------------------------------------------------------------
-- MIGRATION NOTE (non-destructive):
-- If you are upgrading an existing install that previously used ADSB_DATA_GOLD / ADSB_DATA_SILVER
-- and legacy GATES/RUNWAYS, you can optionally migrate data once:
--
-- 1) Properties tables:
--    CREATE OR REPLACE TABLE {database}.{schema}.PROPERTIES_GATES     AS SELECT * FROM {database}.{schema}.GATES;
--    CREATE OR REPLACE TABLE {database}.{schema}.PROPERTIES_RUNWAYS   AS SELECT * FROM {database}.{schema}.RUNWAYS;
--
-- 2) ADSB_DATA:
--    INSERT INTO {database}.{schema}.ADSB_DATA (
--      FLIGHT_KEY, ICAO_HEX, REGISTRATION, TYPE, AIRCRAFT_DESC, FLIGHT, TIMESTAMP, LOCATION,
--      TRACK, TRUE_HEADING, VELOCITY, ALTITUDE_BARO, ALTITUDE_GEOM, VERTICAL_RATE, SQUAWK, CATEGORY, SOURCE
--    )
--    SELECT
--      FLIGHT_KEY, ICAO_HEX, REGISTRATION, TYPE, AIRCRAFT_DESC, FLIGHT, TIMESTAMP, LOCATION,
--      TRACK, TRUE_HEADING, VELOCITY, ALTITUDE_BARO, ALTITUDE_GEOM, VERTICAL_RATE, SQUAWK, CATEGORY, SOURCE
--    FROM {database}.{schema}.ADSB_DATA_GOLD;
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- External Network Access (for adsb.lol API)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE NETWORK RULE {database}.{schema}.{schema}_adsb_lol_rule
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('api.adsb.lol:443');

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION {eai_adsb_lol}
  ALLOWED_NETWORK_RULES = ({database}.{schema}.{schema}_adsb_lol_rule)
  ENABLED = TRUE;

-- -----------------------------------------------------------------------------
-- HELPER raw table (Bronze layer)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE {database}.{schema}.HELPER_ADSB_LOL_RAW (
    hex VARCHAR(16),
    flight VARCHAR(32),
    registration VARCHAR(16),
    aircraft_type VARCHAR(8),
    aircraft_desc VARCHAR(128),
    lat FLOAT,
    lon FLOAT,
    alt_baro INT,
    alt_geom INT,
    ground_speed FLOAT,
    track FLOAT,
    true_heading FLOAT,
    vertical_rate INT,
    squawk VARCHAR(8),
    category VARCHAR(8),
    timestamp TIMESTAMP_NTZ,
    ingested_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- -----------------------------------------------------------------------------
-- HELPER aircraft metadata (dimension, populated via adsb.lol lookup by ICAO_HEX)
-- Purpose: improve AIRCRAFT_DESC coverage when realtime point feed omits `desc`.
-- Docs: https://api.adsb.lol/docs
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE {database}.{schema}.HELPER_AIRCRAFT_META (
    ICAO_HEX VARCHAR(16),
    REGISTRATION VARCHAR(16),
    TYPE VARCHAR(8),
    AIRCRAFT_DESC VARCHAR(256),
    UPDATED_AT TIMESTAMP_NTZ,
    SOURCE VARCHAR(32),
    RAW_JSON VARIANT
);

-- -----------------------------------------------------------------------------
-- Canonical ADS-B table (single source of truth for dashboards)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE {database}.{schema}.ADSB_DATA (
    FLIGHT_KEY VARCHAR(128),
    ICAO_HEX VARCHAR(16),
    REGISTRATION VARCHAR(16),
    TYPE VARCHAR(8),
    AIRCRAFT_DESC VARCHAR(128),
    FLIGHT VARCHAR(32),
    TIMESTAMP TIMESTAMP_NTZ,
    LOCATION GEOGRAPHY,
    TRACK FLOAT,
    TRUE_HEADING FLOAT,
    VELOCITY FLOAT,
    ALTITUDE_BARO INT,
    ALTITUDE_GEOM INT,
    VERTICAL_RATE INT,
    SQUAWK VARCHAR(8),
    CATEGORY VARCHAR(8),
    SOURCE VARCHAR(16),
    INGESTED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    -- Schedule/enrichment fields (nullable)
    SCHEDULE_FLIGHT_KEY VARCHAR(128),
    SCHEDULE_FLIGHT_NUMBER VARCHAR(16),
    AIRLINE_NAME VARCHAR(128),
    AIRLINE_IATA VARCHAR(8),
    AIRLINE_ICAO VARCHAR(8),
    ORIGIN_AIRPORT VARCHAR(8),
    DESTINATION_AIRPORT VARCHAR(8),
    IS_LOCAL_OD BOOLEAN,
    SCHEDULED_DEPARTURE TIMESTAMP_NTZ,
    SCHEDULED_ARRIVAL TIMESTAMP_NTZ,
    MATCH_METHOD VARCHAR(32),
    MATCH_CONFIDENCE INT,
    MATCHED_AT TIMESTAMP_NTZ
);

-- Non-destructive schema evolution for upgrades
ALTER TABLE {database}.{schema}.ADSB_DATA ADD COLUMN IF NOT EXISTS IS_LOCAL_OD BOOLEAN;

-- -----------------------------------------------------------------------------
-- Matching observability tables (persistent, debuggable artifacts)
-- Implements Phase 0 of FLIGHT_MATCHING_RECOMMENDATIONS.md: leg inference + candidates + results
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_FLIGHT_LEG (
  SERVICE_DATE DATE,
  ICAO_HEX STRING,
  SEG_ID INT,
  LEG_START_TS TIMESTAMP_NTZ,
  LEG_END_TS TIMESTAMP_NTZ,
  DIRECTION STRING,
  START_LOC GEOGRAPHY,
  END_LOC GEOGRAPHY,
  CALLSIGN STRING,
  REGISTRATION STRING,
  POINTS INT,
  COMPUTED_AT TIMESTAMP_NTZ
);

CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_FLIGHT_MATCH_CANDIDATES (
  SERVICE_DATE DATE,
  ICAO_HEX STRING,
  SEG_ID INT,
  MATCH_METHOD STRING,
  MATCH_PRIORITY INT,
  DATE_DIFF_DAYS INT,
  DIFF_MIN INT,
  ABS_DIFF_MIN INT,
  DIRECTION STRING,
  DIRECTION_OK BOOLEAN,
  SCHEDULE_FLIGHT_KEY STRING,
  SCHEDULE_FLIGHT_NUMBER STRING,
  AIRLINE_ICAO STRING,
  AIRLINE_IATA STRING,
  DEPARTURE_AIRPORT STRING,
  ARRIVAL_AIRPORT STRING,
  SCORE INT,
  CREATED_AT TIMESTAMP_NTZ
);

CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_FLIGHT_MATCH_RESULT (
  SERVICE_DATE DATE,
  ICAO_HEX STRING,
  SEG_ID INT,
  MATCH_METHOD STRING,
  MATCH_PRIORITY INT,
  DATE_DIFF_DAYS INT,
  ABS_DIFF_MIN INT,
  DIRECTION STRING,
  DIRECTION_OK BOOLEAN,
  SCHEDULE_FLIGHT_KEY STRING,
  SCHEDULE_FLIGHT_NUMBER STRING,
  SCORE INT,
  CHOSEN_AT TIMESTAMP_NTZ
);

-- -----------------------------------------------------------------------------
-- Recurring callsign prior (Phase 4)
-- Built from historical leg->schedule matches to provide a conservative fallback
-- for airline + O/D when schedule matching is missing/ambiguous for a given callsign.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_RECURRING_CALLSIGN_PRIOR (
  CALLSIGN_KEY STRING,
  AIRLINE_ICAO STRING,
  AIRLINE_IATA STRING,
  AIRLINE_NAME STRING,
  ORIGIN_AIRPORT STRING,
  DESTINATION_AIRPORT STRING,
  LEGS INT,
  LAST_SEEN_DATE DATE,
  UPDATED_AT TIMESTAMP_NTZ
);

-- -----------------------------------------------------------------------------
-- Enrichment: associate all points to schedule flight number/key (best-effort)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_ENRICH_ADSB_WITH_SCHEDULE(p_days_back INT)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
  v_days INT;
  v_src_rows NUMBER(38,0);
  v_merge_rows NUMBER(38,0);
BEGIN
  v_days := COALESCE(:p_days_back, 2);

  -- Self-heal: duplicates in ADSB_DATA (by ICAO_HEX,TIMESTAMP) cause MERGE to fail with:
  --   "Duplicate row detected during DML action"
  -- This can happen from older installer versions / parallel loads.
  -- Keep newest INGESTED_AT per key within the enrichment window.
  DELETE FROM {database}.{schema}.ADSB_DATA
  WHERE (ICAO_HEX, TIMESTAMP, INGESTED_AT) IN (
    SELECT ICAO_HEX, TIMESTAMP, INGESTED_AT
    FROM (
      SELECT
        ICAO_HEX,
        TIMESTAMP,
        INGESTED_AT,
        ROW_NUMBER() OVER (
          PARTITION BY ICAO_HEX, TIMESTAMP
          ORDER BY INGESTED_AT DESC
        ) AS rn
      FROM {database}.{schema}.ADSB_DATA
      WHERE TIMESTAMP::DATE >= DATEADD('day', -:v_days, CURRENT_DATE())
        AND ICAO_HEX IS NOT NULL
        AND TIMESTAMP IS NOT NULL
    )
    WHERE rn > 1
  );

  -- Source sanity: use calendar days (UTC date) rather than "last N hours"
  SELECT COUNT(*) INTO v_src_rows
  FROM {database}.{schema}.ADSB_DATA
  WHERE TIMESTAMP::DATE >= DATEADD('day', -:v_days, CURRENT_DATE())
    AND ICAO_HEX IS NOT NULL
    AND TIMESTAMP IS NOT NULL;

  IF (v_src_rows = 0) THEN
    RETURN 'Enrichment skipped: no ADSB_DATA rows in last ' || :v_days || ' days';
  END IF;

  -- 1) Build airborne segments ("legs") per aircraft/day using a simple ground/air state machine.
  CREATE OR REPLACE TEMP TABLE tmp_airborne_leg AS
  WITH pts AS (
    SELECT
      ICAO_HEX,
      REGISTRATION,
      FLIGHT,
      TIMESTAMP::DATE AS service_date,
      TIMESTAMP AS ts,
      LOCATION,
      VELOCITY,
      ALTITUDE_BARO,
      -- Treat low-speed points as ground even if ALTITUDE_BARO is missing (common in realtime feeds).
      IFF(
        COALESCE(VELOCITY, 0) <= 40
        AND (
          ALTITUDE_BARO IS NULL
          OR ALTITUDE_BARO <= 50
        ),
        1, 0
      ) AS is_ground,
      DATEDIFF('minute', LAG(TIMESTAMP) OVER (PARTITION BY ICAO_HEX, TIMESTAMP::DATE ORDER BY TIMESTAMP), TIMESTAMP) AS gap_min,
      LAG(IFF(
            COALESCE(VELOCITY, 0) <= 40
            AND (ALTITUDE_BARO IS NULL OR ALTITUDE_BARO <= 50),
            1, 0
          ))
        OVER (PARTITION BY ICAO_HEX, TIMESTAMP::DATE ORDER BY TIMESTAMP) AS prev_is_ground
    FROM {database}.{schema}.ADSB_DATA
    WHERE TIMESTAMP::DATE >= DATEADD('day', -:v_days, CURRENT_DATE())
      AND ICAO_HEX IS NOT NULL
      AND LOCATION IS NOT NULL
      AND TIMESTAMP IS NOT NULL
  ),
  seg AS (
    SELECT
      *,
      SUM(IFF(COALESCE(gap_min, 999999) > 20 OR COALESCE(prev_is_ground, is_ground) <> is_ground, 1, 0))
        OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts ROWS UNBOUNDED PRECEDING) AS seg_id
    FROM pts
  ),
  airborne AS (
    SELECT *
    FROM seg
    WHERE is_ground = 0
  )
  SELECT
    ICAO_HEX,
    service_date,
    seg_id,
    MIN(ts) AS leg_start_ts,
    MAX(ts) AS leg_end_ts,
    MAX(REGISTRATION) AS registration,
    MAX(NULLIF(UPPER(TRIM(FLIGHT)), '')) AS callsign,
    COUNT(*) AS points
  FROM airborne
  GROUP BY 1,2,3;

  -- 2) Classify leg direction relative to the airport polygon.
  CREATE OR REPLACE TEMP TABLE tmp_leg_dir AS
  WITH ap AS (SELECT geometry AS g FROM {database}.{schema}.PROPERTIES_AIRPORT LIMIT 1),
  p0 AS (
    SELECT
      ICAO_HEX,
      TIMESTAMP::DATE AS service_date,
      TIMESTAMP AS ts,
      LOCATION,
      IFF(
        COALESCE(VELOCITY, 0) <= 40
        AND (ALTITUDE_BARO IS NULL OR ALTITUDE_BARO <= 50),
        1, 0
      ) AS is_ground
    FROM {database}.{schema}.ADSB_DATA
    WHERE TIMESTAMP::DATE >= DATEADD('day', -:v_days, CURRENT_DATE())
      AND ICAO_HEX IS NOT NULL
      AND LOCATION IS NOT NULL
      AND TIMESTAMP IS NOT NULL
  ),
  p1 AS (
    SELECT
      *,
      LAG(ts) OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts) AS prev_ts,
      LAG(is_ground) OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts) AS prev_is_ground
    FROM p0
  ),
  p2 AS (
    SELECT
      *,
      DATEDIFF('minute', prev_ts, ts) AS gap_min
    FROM p1
  ),
  p AS (
    SELECT
      *,
      -- NOTE: window functions cannot be nested in Snowflake; keep LAG() in prior CTEs and only SUM() here.
      SUM(IFF(COALESCE(gap_min, 999999) > 20 OR COALESCE(prev_is_ground, is_ground) <> is_ground, 1, 0))
        OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts ROWS UNBOUNDED PRECEDING) AS seg_id
    FROM p2
  ),
  start_rows AS (
    SELECT ICAO_HEX, service_date, seg_id, LOCATION AS start_loc
    FROM p
    WHERE is_ground = 0
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ICAO_HEX, service_date, seg_id ORDER BY ts ASC) = 1
  ),
  end_rows AS (
    SELECT ICAO_HEX, service_date, seg_id, LOCATION AS end_loc
    FROM p
    WHERE is_ground = 0
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ICAO_HEX, service_date, seg_id ORDER BY ts DESC) = 1
  ),
  endpoints AS (
    SELECT s.ICAO_HEX, s.service_date, s.seg_id, s.start_loc, e.end_loc
    FROM start_rows s
    JOIN end_rows e USING (ICAO_HEX, service_date, seg_id)
  )
  SELECT
    l.*,
    CASE
      WHEN ST_DWITHIN(e.start_loc, ap.g, 5000) AND NOT ST_DWITHIN(e.end_loc, ap.g, 5000) THEN 'departure'
      WHEN NOT ST_DWITHIN(e.start_loc, ap.g, 5000) AND ST_DWITHIN(e.end_loc, ap.g, 5000) THEN 'arrival'
      WHEN ST_DWITHIN(e.start_loc, ap.g, 5000) AND ST_DWITHIN(e.end_loc, ap.g, 5000) THEN 'local'
      ELSE 'unknown'
    END AS direction,
    e.start_loc,
    e.end_loc
  FROM tmp_airborne_leg l
  JOIN endpoints e USING (ICAO_HEX, service_date, seg_id)
  CROSS JOIN ap;

  -- Persist legs for debugging/analytics (keep last v_days + 1 due to ±1 day schedule joins)
  DELETE FROM {database}.{schema}.HELPER_FLIGHT_LEG
  WHERE service_date >= DATEADD('day', -(:v_days + 1), CURRENT_DATE());

  INSERT INTO {database}.{schema}.HELPER_FLIGHT_LEG (
    SERVICE_DATE, ICAO_HEX, SEG_ID, LEG_START_TS, LEG_END_TS,
    DIRECTION, START_LOC, END_LOC, CALLSIGN, REGISTRATION, POINTS, COMPUTED_AT
  )
  SELECT
    service_date,
    ICAO_HEX,
    seg_id,
    leg_start_ts,
    leg_end_ts,
    direction,
    start_loc,
    end_loc,
    callsign,
    registration,
    points,
    CURRENT_TIMESTAMP()
  FROM tmp_leg_dir;

  -- 3) Match legs to schedule using (a) registration + date + time proximity, and
  --    (b) callsign (flight/callsign) + date + time proximity as a fallback.
  CREATE OR REPLACE TEMP TABLE tmp_leg_candidates AS
  WITH airport AS (
    SELECT
      UPPER(airport_code) AS airport_code,
      UPPER(airport_icao) AS airport_icao
    FROM {database}.{schema}.PROPERTIES_AIRPORT
    LIMIT 1
  ),
  sched AS (
    SELECT
      FLIGHT_KEY AS schedule_flight_key,
      FLIGHT_NUMBER AS schedule_flight_number,
      FLIGHT_DATE AS service_date,
      UPPER(AIRCRAFT_REGISTRATION) AS registration,
      UPPER(TRIM(AIRLINE_ICAO)) AS airline_icao,
      UPPER(TRIM(AIRLINE_IATA)) AS airline_iata,
      UPPER(TRIM(FLIGHT_ICAO)) AS flight_icao,
      UPPER(TRIM(FLIGHT_IATA)) AS flight_iata,
      UPPER(TRIM(FLIGHT_NUMBER)) AS flight_number_norm,
      DEPARTURE_AIRPORT,
      ARRIVAL_AIRPORT,
      DEPARTURE_SCHEDULED,
      ARRIVAL_SCHEDULED
    FROM {database}.{schema}.FLIGHT_SCHEDULE
    -- Include an extra day because ADSB uses UTC dates while schedule is airport-local date in practice.
    WHERE FLIGHT_DATE >= DATEADD('day', -(:v_days + 1), CURRENT_DATE())
  ),
  candidates_reg AS (
    SELECT
      l.ICAO_HEX, l.service_date, l.seg_id,
      s.schedule_flight_key, s.schedule_flight_number,
      s.airline_icao, s.airline_iata,
      s.DEPARTURE_AIRPORT, s.ARRIVAL_AIRPORT,
      l.direction,
      ABS(DATEDIFF('day', s.service_date, l.service_date)) AS date_diff_days,
      CASE
        WHEN l.direction = 'departure' THEN DATEDIFF('minute', s.DEPARTURE_SCHEDULED, l.leg_start_ts)
        WHEN l.direction = 'arrival' THEN DATEDIFF('minute', s.ARRIVAL_SCHEDULED, l.leg_end_ts)
        ELSE LEAST(
          ABS(DATEDIFF('minute', s.DEPARTURE_SCHEDULED, l.leg_start_ts)),
          ABS(DATEDIFF('minute', s.ARRIVAL_SCHEDULED, l.leg_end_ts))
        )
      END AS diff_min,
      CASE
        WHEN l.direction = 'departure' THEN l.leg_start_ts
        WHEN l.direction = 'arrival' THEN l.leg_end_ts
        ELSE DATEADD('minute', DATEDIFF('minute', l.leg_start_ts, l.leg_end_ts)/2, l.leg_start_ts)
      END AS anchor_ts
    FROM tmp_leg_dir l
    JOIN sched s
      -- Allow ±1 day due to UTC vs local date boundaries
      ON s.service_date BETWEEN DATEADD('day', -1, l.service_date) AND DATEADD('day', 1, l.service_date)
     AND s.registration = UPPER(l.registration)
    WHERE l.registration IS NOT NULL
  ),
  callsign_normalized AS (
    -- Normalize callsigns: strip trailing operational suffixes (W,J,X,Y,Z)
    -- and extract airline prefix + flight number for dual-prefix matching.
    SELECT
      ICAO_HEX, service_date, seg_id, callsign, leg_start_ts, leg_end_ts, direction,
      -- Strip single trailing letter suffix if present (SKW864W → SKW864)
      REGEXP_REPLACE(UPPER(TRIM(callsign)), '[WJXYZ]$', '') AS callsign_normalized,
      -- Extract airline prefix (2-3 letters)
      REGEXP_SUBSTR(UPPER(TRIM(callsign)), '^[A-Z]{{2,3}}') AS airline_prefix,
      -- Extract numeric part
      REGEXP_SUBSTR(UPPER(TRIM(callsign)), '[0-9]+') AS flight_number_part
    FROM tmp_leg_dir
    WHERE callsign IS NOT NULL AND callsign <> ''
  ),
  callsign_with_alternates AS (
    -- For each callsign, get alternate airline codes (IATA↔ICAO translation)
    SELECT
      c.*,
      m_icao.airline_iata AS alternate_iata,
      m_iata.airline_icao AS alternate_icao
    FROM callsign_normalized c
    -- If callsign has 3-letter prefix (ICAO), find corresponding IATA
    LEFT JOIN {database}.{schema}.HELPER_AIRLINE_IATA_ICAO_MAP m_icao
      ON LENGTH(c.airline_prefix) = 3
     AND m_icao.airline_icao = c.airline_prefix
    -- If callsign has 2-letter prefix (IATA), find corresponding ICAO
    LEFT JOIN {database}.{schema}.HELPER_AIRLINE_IATA_ICAO_MAP m_iata
      ON LENGTH(c.airline_prefix) = 2
     AND m_iata.airline_iata = c.airline_prefix
  ),
  candidates_callsign AS (
    SELECT
      l.ICAO_HEX, l.service_date, l.seg_id,
      s.schedule_flight_key, s.schedule_flight_number,
      s.airline_icao, s.airline_iata,
      s.DEPARTURE_AIRPORT, s.ARRIVAL_AIRPORT,
      l.direction,
      ABS(DATEDIFF('day', s.service_date, l.service_date)) AS date_diff_days,
      CASE
        WHEN l.direction = 'departure' THEN DATEDIFF('minute', s.DEPARTURE_SCHEDULED, l.leg_start_ts)
        WHEN l.direction = 'arrival' THEN DATEDIFF('minute', s.ARRIVAL_SCHEDULED, l.leg_end_ts)
        ELSE LEAST(
          ABS(DATEDIFF('minute', s.DEPARTURE_SCHEDULED, l.leg_start_ts)),
          ABS(DATEDIFF('minute', s.ARRIVAL_SCHEDULED, l.leg_end_ts))
        )
      END AS diff_min,
      CASE
        WHEN l.direction = 'departure' THEN l.leg_start_ts
        WHEN l.direction = 'arrival' THEN l.leg_end_ts
        ELSE DATEADD('minute', DATEDIFF('minute', l.leg_start_ts, l.leg_end_ts)/2, l.leg_start_ts)
      END AS anchor_ts
    FROM callsign_with_alternates l
    JOIN sched s
      -- Allow ±1 day due to UTC vs local date boundaries
      ON s.service_date BETWEEN DATEADD('day', -1, l.service_date) AND DATEADD('day', 1, l.service_date)
     AND (
          -- Exact callsign match (original or normalized)
          s.flight_icao = UPPER(TRIM(l.callsign))
       OR s.flight_iata = UPPER(TRIM(l.callsign))
       OR s.flight_icao = l.callsign_normalized
       OR s.flight_iata = l.callsign_normalized
       -- Numeric + airline prefix match (current logic, but with normalized callsign)
       OR (
            l.airline_prefix IS NOT NULL
        AND l.flight_number_part IS NOT NULL
        AND s.flight_number_norm = l.flight_number_part
        AND (
              (LENGTH(l.airline_prefix) = 3 AND s.airline_icao = l.airline_prefix)
           OR (LENGTH(l.airline_prefix) = 2 AND s.airline_iata = l.airline_prefix)
           -- NEW: Try alternate code (IATA↔ICAO translation)
           OR (LENGTH(l.airline_prefix) = 3 AND l.alternate_iata IS NOT NULL AND s.airline_iata = l.alternate_iata)
           OR (LENGTH(l.airline_prefix) = 2 AND l.alternate_icao IS NOT NULL AND s.airline_icao = l.alternate_icao)
        )
       )
     )
  ),
  candidates AS (
    SELECT *, 'registration_time' AS match_method, 0 AS match_priority FROM candidates_reg
    UNION ALL
    SELECT *, 'callsign_time' AS match_method, 1 AS match_priority FROM candidates_callsign
  ),
  filtered AS (
    SELECT *,
      ABS(diff_min) AS abs_diff
    FROM candidates
    WHERE
      (match_method = 'registration_time' AND abs(diff_min) <= 240)
      OR
      -- Callsign is a strong identifier; expand to ±36 hours to catch irregular ops/delays.
      (match_method = 'callsign_time' AND abs(diff_min) <= 2160)
  ),
  scored AS (
    SELECT
      c.*,
      -- Direction sanity: for arrivals, schedule ARRIVAL should be our airport; for departures, schedule DEPARTURE.
      IFF(
        c.direction IN ('arrival','departure'),
        IFF(
          c.direction = 'arrival',
          UPPER(TRIM(c.ARRIVAL_AIRPORT)) IN ((SELECT airport_code FROM airport), (SELECT airport_icao FROM airport)),
          UPPER(TRIM(c.DEPARTURE_AIRPORT)) IN ((SELECT airport_code FROM airport), (SELECT airport_icao FROM airport))
        ),
        TRUE
      ) AS direction_ok,
      -- Base score from time gap, then penalize date diff and direction mismatch.
      -- Improved confidence tiers for wider callsign window (±36 hrs):
      --   0-120 min: 80-90 confidence
      --   121-1440 min (24h): 60-79 confidence
      --   1441-2160 min (36h): 40-59 confidence
      (
        IFF(
          c.match_method = 'registration_time',
          GREATEST(0, 100 - (c.abs_diff * 100 / 240))::INT,
          -- Callsign scoring with tiered confidence
          CASE
            WHEN c.abs_diff <= 120 THEN GREATEST(0, 90 - (c.abs_diff * 10 / 120))::INT
            WHEN c.abs_diff <= 1440 THEN GREATEST(0, 79 - ((c.abs_diff - 120) * 19 / 1320))::INT
            ELSE GREATEST(0, 59 - ((c.abs_diff - 1440) * 19 / 720))::INT
          END
        )
        - (c.date_diff_days * 10)
        - IFF(
            c.direction IN ('arrival','departure')
            AND NOT IFF(
              c.direction = 'arrival',
              UPPER(TRIM(c.ARRIVAL_AIRPORT)) IN ((SELECT airport_code FROM airport), (SELECT airport_icao FROM airport)),
              UPPER(TRIM(c.DEPARTURE_AIRPORT)) IN ((SELECT airport_code FROM airport), (SELECT airport_icao FROM airport))
            ),
            30, 0
          )
      )::INT AS score
    FROM filtered c
  )
  SELECT
    ICAO_HEX, service_date, seg_id,
    schedule_flight_key, schedule_flight_number,
    match_method,
    match_priority,
    date_diff_days,
    diff_min,
    abs_diff,
    direction,
    direction_ok,
    airline_icao,
    airline_iata,
    DEPARTURE_AIRPORT,
    ARRIVAL_AIRPORT,
    score,
    anchor_ts
  FROM scored;

  -- Persist candidates (debugging)
  DELETE FROM {database}.{schema}.HELPER_FLIGHT_MATCH_CANDIDATES
  WHERE service_date >= DATEADD('day', -(:v_days + 1), CURRENT_DATE());

  INSERT INTO {database}.{schema}.HELPER_FLIGHT_MATCH_CANDIDATES (
    SERVICE_DATE, ICAO_HEX, SEG_ID,
    MATCH_METHOD, MATCH_PRIORITY, DATE_DIFF_DAYS, DIFF_MIN, ABS_DIFF_MIN,
    DIRECTION, DIRECTION_OK,
    SCHEDULE_FLIGHT_KEY, SCHEDULE_FLIGHT_NUMBER,
    AIRLINE_ICAO, AIRLINE_IATA,
    DEPARTURE_AIRPORT, ARRIVAL_AIRPORT,
    SCORE, CREATED_AT
  )
  SELECT
    service_date, ICAO_HEX, seg_id,
    match_method, match_priority, date_diff_days, diff_min, abs_diff,
    direction, direction_ok,
    schedule_flight_key, schedule_flight_number,
    airline_icao, airline_iata,
    DEPARTURE_AIRPORT, ARRIVAL_AIRPORT,
    score, CURRENT_TIMESTAMP()
  FROM tmp_leg_candidates;

  -- Choose best candidate per leg using score + direction sanity
  CREATE OR REPLACE TEMP TABLE tmp_leg_match AS
  SELECT
    ICAO_HEX,
    service_date,
    seg_id,
    schedule_flight_key,
    schedule_flight_number,
    match_method,
    -- Keep existing confidence semantics for downstream consumers
    GREATEST(0, score)::INT AS match_confidence,
    anchor_ts
  FROM tmp_leg_candidates
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY ICAO_HEX, service_date, seg_id
    ORDER BY match_priority ASC, direction_ok DESC, score DESC, abs_diff ASC, date_diff_days ASC
  ) = 1;

  -- Persist chosen results
  DELETE FROM {database}.{schema}.HELPER_FLIGHT_MATCH_RESULT
  WHERE service_date >= DATEADD('day', -(:v_days + 1), CURRENT_DATE());

  INSERT INTO {database}.{schema}.HELPER_FLIGHT_MATCH_RESULT (
    SERVICE_DATE, ICAO_HEX, SEG_ID,
    MATCH_METHOD, MATCH_PRIORITY, DATE_DIFF_DAYS, ABS_DIFF_MIN,
    DIRECTION, DIRECTION_OK,
    SCHEDULE_FLIGHT_KEY, SCHEDULE_FLIGHT_NUMBER,
    SCORE, CHOSEN_AT
  )
  SELECT
    service_date, ICAO_HEX, seg_id,
    match_method, match_priority, date_diff_days, abs_diff,
    direction, direction_ok,
    schedule_flight_key, schedule_flight_number,
    score, CURRENT_TIMESTAMP()
  FROM tmp_leg_candidates
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY ICAO_HEX, service_date, seg_id
    ORDER BY match_priority ASC, direction_ok DESC, score DESC, abs_diff ASC, date_diff_days ASC
  ) = 1;

  -- -----------------------------------------------------------------------------
  -- Phase 4: Refresh recurring callsign prior (conservative fallback)
  -- -----------------------------------------------------------------------------
  CREATE OR REPLACE TABLE {database}.{schema}.HELPER_RECURRING_CALLSIGN_PRIOR AS
  WITH base AS (
    SELECT
      l.service_date,
      l.callsign,
      REGEXP_SUBSTR(UPPER(TRIM(l.callsign)), '^[A-Z]{{2,3}}[0-9]+') AS callsign_key,
      r.schedule_flight_key,
      fs.AIRLINE_ICAO,
      fs.AIRLINE_IATA,
      fs.AIRLINE_NAME,
      fs.DEPARTURE_AIRPORT AS origin_airport,
      fs.ARRIVAL_AIRPORT AS destination_airport
    FROM {database}.{schema}.HELPER_FLIGHT_LEG l
    JOIN {database}.{schema}.HELPER_FLIGHT_MATCH_RESULT r
      ON r.ICAO_HEX = l.ICAO_HEX AND r.service_date = l.service_date AND r.seg_id = l.seg_id
    LEFT JOIN {database}.{schema}.FLIGHT_SCHEDULE fs
      ON fs.FLIGHT_KEY = r.schedule_flight_key
    WHERE l.service_date >= DATEADD('day', -30, CURRENT_DATE())
      AND l.callsign IS NOT NULL AND TRIM(l.callsign) <> ''
      AND REGEXP_SUBSTR(UPPER(TRIM(l.callsign)), '[0-9]+') IS NOT NULL
      AND callsign_key IS NOT NULL AND callsign_key <> ''
      AND fs.FLIGHT_KEY IS NOT NULL
  ),
  airline_counts AS (
    SELECT
      callsign_key,
      TRIM(UPPER(AIRLINE_ICAO)) AS airline_icao,
      TRIM(UPPER(AIRLINE_IATA)) AS airline_iata,
      MAX(TRIM(AIRLINE_NAME)) AS airline_name,
      COUNT(*) AS legs
    FROM base
    GROUP BY 1,2,3
  ),
  best_airline AS (
    SELECT *
    FROM airline_counts
    QUALIFY ROW_NUMBER() OVER (PARTITION BY callsign_key ORDER BY legs DESC, airline_icao ASC NULLS LAST, airline_iata ASC NULLS LAST) = 1
  ),
  od_counts AS (
    SELECT
      callsign_key,
      TRIM(UPPER(origin_airport)) AS origin_airport,
      TRIM(UPPER(destination_airport)) AS destination_airport,
      COUNT(*) AS legs
    FROM base
    WHERE origin_airport IS NOT NULL AND destination_airport IS NOT NULL
    GROUP BY 1,2,3
  ),
  best_od AS (
    SELECT *
    FROM od_counts
    QUALIFY ROW_NUMBER() OVER (PARTITION BY callsign_key ORDER BY legs DESC, origin_airport ASC, destination_airport ASC) = 1
  ),
  totals AS (
    SELECT callsign_key, COUNT(*) AS legs, MAX(service_date) AS last_seen_date
    FROM base
    GROUP BY 1
  )
  SELECT
    t.callsign_key,
    a.airline_icao,
    a.airline_iata,
    a.airline_name,
    o.origin_airport,
    o.destination_airport,
    t.legs,
    t.last_seen_date,
    CURRENT_TIMESTAMP() AS updated_at
  FROM totals t
  LEFT JOIN best_airline a USING (callsign_key)
  LEFT JOIN best_od o USING (callsign_key)
  WHERE t.legs >= 5;

  -- 4) Apply schedule association to points in ADSB_DATA (canonical table)
  MERGE INTO {database}.{schema}.ADSB_DATA t
  USING (
    WITH airport AS (
      SELECT
        UPPER(airport_code) AS airport_code,
        UPPER(airport_icao) AS airport_icao,
        geometry AS airport_geom
      FROM {database}.{schema}.PROPERTIES_AIRPORT
      LIMIT 1
    ),
    pts AS (
      SELECT
        s.*,
        s.TIMESTAMP::DATE AS service_date,
        IFF(
          COALESCE(s.VELOCITY, 0) <= 40
          AND (s.ALTITUDE_BARO IS NULL OR s.ALTITUDE_BARO <= 50),
          1, 0
        ) AS is_ground,
        DATEDIFF('minute', LAG(s.TIMESTAMP) OVER (PARTITION BY s.ICAO_HEX, s.TIMESTAMP::DATE ORDER BY s.TIMESTAMP), s.TIMESTAMP) AS gap_min,
        LAG(IFF(
              COALESCE(s.VELOCITY, 0) <= 40
              AND (s.ALTITUDE_BARO IS NULL OR s.ALTITUDE_BARO <= 50),
              1, 0
            ))
          OVER (PARTITION BY s.ICAO_HEX, s.TIMESTAMP::DATE ORDER BY s.TIMESTAMP) AS prev_is_ground
      FROM {database}.{schema}.ADSB_DATA s
      WHERE s.TIMESTAMP::DATE >= DATEADD('day', -:v_days, CURRENT_DATE())
        AND s.ICAO_HEX IS NOT NULL
        AND s.TIMESTAMP IS NOT NULL
    ),
    seg AS (
      SELECT
        *,
        SUM(IFF(COALESCE(gap_min, 999999) > 20 OR COALESCE(prev_is_ground, is_ground) <> is_ground, 1, 0))
          OVER (PARTITION BY ICAO_HEX, service_date ORDER BY TIMESTAMP ROWS UNBOUNDED PRECEDING) AS seg_id
      FROM pts
    ),
    joined AS (
      SELECT
        p.*,
        m.schedule_flight_key AS match_schedule_flight_key,
        m.schedule_flight_number AS match_schedule_flight_number,
        m.match_method AS match_match_method,
        m.match_confidence AS match_match_confidence,
        m.anchor_ts AS match_anchor_ts
      FROM seg p
      LEFT JOIN tmp_leg_match m
        ON m.ICAO_HEX = p.ICAO_HEX
       AND m.service_date = p.service_date
       AND m.seg_id = p.seg_id
    ),
    filled AS (
      SELECT
        j.*,
        COALESCE(j.match_schedule_flight_key, j.SCHEDULE_FLIGHT_KEY) AS schedule_flight_key_merged,
        COALESCE(j.match_schedule_flight_number, j.SCHEDULE_FLIGHT_NUMBER) AS schedule_flight_number_merged,
        COALESCE(j.match_match_method, j.MATCH_METHOD) AS match_method_merged,
        COALESCE(j.match_match_confidence, j.MATCH_CONFIDENCE) AS match_confidence_merged,
        COALESCE(j.match_anchor_ts, j.MATCHED_AT) AS anchor_ts_merged,
        LAST_VALUE(COALESCE(j.match_schedule_flight_key, j.SCHEDULE_FLIGHT_KEY)) IGNORE NULLS
          OVER (PARTITION BY j.ICAO_HEX, j.service_date ORDER BY j.TIMESTAMP ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS prev_key,
        LAST_VALUE(COALESCE(j.match_anchor_ts, j.MATCHED_AT)) IGNORE NULLS
          OVER (PARTITION BY j.ICAO_HEX, j.service_date ORDER BY j.TIMESTAMP ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS prev_anchor,
        FIRST_VALUE(COALESCE(j.match_schedule_flight_key, j.SCHEDULE_FLIGHT_KEY)) IGNORE NULLS
          OVER (PARTITION BY j.ICAO_HEX, j.service_date ORDER BY j.TIMESTAMP ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS next_key,
        FIRST_VALUE(COALESCE(j.match_anchor_ts, j.MATCHED_AT)) IGNORE NULLS
          OVER (PARTITION BY j.ICAO_HEX, j.service_date ORDER BY j.TIMESTAMP ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS next_anchor
      FROM joined
      AS j
    )
    ,final AS (
      SELECT
      -- Choose nearest schedule key if point itself isn't on a matched leg.
      COALESCE(
        schedule_flight_key_merged,
        IFF(prev_key IS NULL, next_key,
            IFF(next_key IS NULL, prev_key,
                IFF(ABS(DATEDIFF('minute', prev_anchor, TIMESTAMP)) <= ABS(DATEDIFF('minute', next_anchor, TIMESTAMP)), prev_key, next_key)
            )
        )
      ) AS schedule_flight_key_final,
      COALESCE(
        schedule_flight_number_merged,
        -- fallback: use existing callsign when present
        NULLIF(TRIM(FLIGHT), '')
      ) AS schedule_flight_number_final,
      COALESCE(match_method_merged, 'propagated') AS match_method_final,
      COALESCE(match_confidence_merged, 50) AS match_confidence_final,
      NULLIF(UPPER(TRIM(FLIGHT)), '') AS callsign_raw,
      ICAO_HEX, TIMESTAMP
    FROM filled
    )
    ,fs_dedup AS (
      -- Defensive: ensure FLIGHT_SCHEDULE contributes at most 1 row per FLIGHT_KEY.
      -- If older installs (or API quirks) produced duplicates, join fanout would create
      -- duplicate (ICAO_HEX,TIMESTAMP) rows in the MERGE source and the MERGE would fail.
      SELECT *
      FROM {database}.{schema}.FLIGHT_SCHEDULE
      QUALIFY ROW_NUMBER() OVER (
        PARTITION BY FLIGHT_KEY
        ORDER BY UPDATED_AT DESC
      ) = 1
    )
    ,rp_dedup AS (
      -- Ensure 1 row per callsign_key to avoid join fanout.
      SELECT *
      FROM {database}.{schema}.HELPER_RECURRING_CALLSIGN_PRIOR
      QUALIFY ROW_NUMBER() OVER (
        PARTITION BY CALLSIGN_KEY
        ORDER BY LEGS DESC, UPDATED_AT DESC
      ) = 1
    )
    ,airline_dim_icao AS (
      -- HELPER_AIRLINE_DIM can contain multiple rows per code; collapse to 1 row/code to avoid fanout.
      SELECT
        TRIM(UPPER(AIRLINE_ICAO)) AS airline_icao,
        MAX(NULLIF(TRIM(AIRLINE_NAME), '')) AS airline_name
      FROM {database}.{schema}.HELPER_AIRLINE_DIM
      WHERE AIRLINE_ICAO IS NOT NULL AND TRIM(AIRLINE_ICAO) <> ''
      GROUP BY 1
    )
    ,airline_dim_iata AS (
      SELECT
        TRIM(UPPER(AIRLINE_IATA)) AS airline_iata,
        MAX(NULLIF(TRIM(AIRLINE_NAME), '')) AS airline_name,
        MAX(NULLIF(TRIM(AIRLINE_IATA), '')) AS airline_iata_raw
      FROM {database}.{schema}.HELPER_AIRLINE_DIM
      WHERE AIRLINE_IATA IS NOT NULL AND TRIM(AIRLINE_IATA) <> ''
      GROUP BY 1
    )
    SELECT
      f.schedule_flight_key_final,
      f.schedule_flight_number_final,
      f.match_method_final,
      f.match_confidence_final,
      -- Airline fallback priority:
      --  1) Schedule (best)
      --  2) Recurring callsign prior (when schedule missing)
      --  3) Airline dim by prefix (last resort)
      COALESCE(fs.AIRLINE_NAME, rp.AIRLINE_NAME, ad3.airline_name, ad2.airline_name) AS airline_name_final,
      COALESCE(fs.AIRLINE_IATA, rp.AIRLINE_IATA, ad2.airline_iata_raw) AS airline_iata_final,
      COALESCE(fs.AIRLINE_ICAO, rp.AIRLINE_ICAO, ad3.airline_icao) AS airline_icao_final,
      COALESCE(fs.DEPARTURE_AIRPORT, rp.ORIGIN_AIRPORT) AS origin_airport_final,
      COALESCE(fs.ARRIVAL_AIRPORT, rp.DESTINATION_AIRPORT) AS destination_airport_final,
      fs.DEPARTURE_SCHEDULED AS scheduled_departure_final,
      fs.ARRIVAL_SCHEDULED AS scheduled_arrival_final,
      IFF(
        airport.airport_code IS NOT NULL
        AND (
          UPPER(TRIM(fs.DEPARTURE_AIRPORT)) IN (airport.airport_code, airport.airport_icao)
          OR UPPER(TRIM(fs.ARRIVAL_AIRPORT)) IN (airport.airport_code, airport.airport_icao)
        ),
        TRUE, FALSE
      ) AS is_local_od_final,
      f.ICAO_HEX,
      f.TIMESTAMP
    FROM final f
    LEFT JOIN fs_dedup fs
      ON fs.FLIGHT_KEY = f.schedule_flight_key_final
    LEFT JOIN rp_dedup rp
      ON rp.CALLSIGN_KEY = REGEXP_SUBSTR(f.callsign_raw, '^[A-Z]{{2,3}}[0-9]+')
    LEFT JOIN airline_dim_icao ad3
      ON ad3.airline_icao = REGEXP_SUBSTR(f.callsign_raw, '^[A-Z]{{3}}')
    LEFT JOIN airline_dim_iata ad2
      ON ad2.airline_iata = REGEXP_SUBSTR(f.callsign_raw, '^[A-Z]{{2}}')
    CROSS JOIN airport
  ) s
  ON t.ICAO_HEX = s.ICAO_HEX AND t.TIMESTAMP = s.TIMESTAMP
  WHEN MATCHED THEN UPDATE SET
    t.SCHEDULE_FLIGHT_KEY = s.schedule_flight_key_final,
    t.SCHEDULE_FLIGHT_NUMBER = s.schedule_flight_number_final,
    t.AIRLINE_NAME = s.airline_name_final,
    t.AIRLINE_IATA = s.airline_iata_final,
    t.AIRLINE_ICAO = s.airline_icao_final,
    t.ORIGIN_AIRPORT = s.origin_airport_final,
    t.DESTINATION_AIRPORT = s.destination_airport_final,
    t.IS_LOCAL_OD = s.is_local_od_final,
    t.SCHEDULED_DEPARTURE = s.scheduled_departure_final,
    t.SCHEDULED_ARRIVAL = s.scheduled_arrival_final,
    t.MATCH_METHOD = s.match_method_final,
    t.MATCH_CONFIDENCE = s.match_confidence_final,
    t.MATCHED_AT = CURRENT_TIMESTAMP();

  v_merge_rows := SQLROWCOUNT;

  RETURN 'Enriched ADSB points for last ' || :v_days || ' days'
         || ' (source_rows=' || :v_src_rows || ', merge_rows=' || :v_merge_rows || ')';
END;
$$;

-- Hourly enrichment task (keeps schedule association current without slowing the 1-minute ingest task)
CREATE OR REPLACE TASK {database}.{schema}.TASK_ENRICH_ADSB_HOURLY
  WAREHOUSE = {warehouse}
  SCHEDULE = '60 MINUTE'
  ALLOW_OVERLAPPING_EXECUTION = FALSE
AS
  CALL {database}.{schema}.PROC_ENRICH_ADSB_WITH_SCHEDULE(2);

-- -----------------------------------------------------------------------------
-- Aircraft description enrichment (lookup by ICAO_HEX, then backfill ADSB_DATA)
-- NOTE: This is best-effort and rate-limit friendly: it only looks up a bounded
-- set of "recent + missing-desc" ICAO_HEX values per run.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_ENRICH_AIRCRAFT_META(
    p_max_hexes INT,
    p_days_back INT,
    p_min_age_hours INT
)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'requests')
HANDLER = 'enrich'
EXTERNAL_ACCESS_INTEGRATIONS = ({eai_adsb_lol})
AS
$$
import json
import requests
from datetime import datetime

def _pick_aircraft_obj(payload):
    # Be resilient to schema changes / different endpoints:
    # - sometimes the aircraft is under payload['ac'][0]
    # - sometimes it's directly the payload dict
    if isinstance(payload, dict):
        ac = payload.get("ac")
        if isinstance(ac, list) and len(ac) > 0 and isinstance(ac[0], dict):
            return ac[0]
        return payload
    return {{}}

def enrich(session, p_max_hexes: int = 200, p_days_back: int = 2, p_min_age_hours: int = 24):
    p_max_hexes = int(p_max_hexes or 200)
    p_days_back = int(p_days_back or 2)
    p_min_age_hours = int(p_min_age_hours or 24)

    db_schema = "{database}.{schema}"

    # Find candidate ICAO_HEX values: recently seen, missing desc/type, and not updated recently in meta.
    q = '''
    WITH candidates AS (
      SELECT DISTINCT ICAO_HEX
      FROM %s.ADSB_DATA
      WHERE TIMESTAMP >= DATEADD('day', -%d, CURRENT_TIMESTAMP())
        AND ICAO_HEX IS NOT NULL
        AND (
          AIRCRAFT_DESC IS NULL OR TRIM(AIRCRAFT_DESC) = ''
          OR TYPE IS NULL OR TRIM(TYPE) = ''
        )
    ),
    filtered AS (
      SELECT c.ICAO_HEX
      FROM candidates c
      LEFT JOIN %s.HELPER_AIRCRAFT_META m
        ON m.ICAO_HEX = c.ICAO_HEX
       AND m.UPDATED_AT >= DATEADD('hour', -%d, CURRENT_TIMESTAMP())
      WHERE m.ICAO_HEX IS NULL
    )
    SELECT ICAO_HEX
    FROM filtered
    ORDER BY ICAO_HEX
    LIMIT %d
    ''' % (db_schema, p_days_back, db_schema, p_min_age_hours, p_max_hexes)
    hexes = [r["ICAO_HEX"] for r in session.sql(q).collect()]
    if not hexes:
        return "No missing aircraft meta to enrich"

    updated = 0
    errors = 0
    now = datetime.utcnow().isoformat()

    for hx in hexes:
        hx = (hx or "").strip().upper()
        if not hx:
            continue
        # Endpoint naming varies; this one is commonly used by ADSB.lol deployments.
        url = "https://api.adsb.lol/v2/hex/%s" % hx
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            ac = _pick_aircraft_obj(payload)

            reg = (ac.get("r") or ac.get("registration") or "").strip().upper() or None
            typ = (ac.get("t") or ac.get("type") or ac.get("aircraft_type") or "").strip().upper() or None
            desc = (ac.get("desc") or ac.get("aircraft_desc") or "").strip() or None

            raw = json.dumps(payload, ensure_ascii=False)

            # Upsert into HELPER_AIRCRAFT_META
            merge_sql = '''
                MERGE INTO %s.HELPER_AIRCRAFT_META t
                USING (
                  SELECT
                    ? AS ICAO_HEX,
                    ? AS REGISTRATION,
                    ? AS TYPE,
                    ? AS AIRCRAFT_DESC,
                    TO_TIMESTAMP_NTZ(?) AS UPDATED_AT,
                    'ADSB_LOL_LOOKUP' AS SOURCE,
                    PARSE_JSON(?) AS RAW_JSON
                ) s
                ON t.ICAO_HEX = s.ICAO_HEX
                WHEN MATCHED THEN UPDATE SET
                  t.REGISTRATION = COALESCE(s.REGISTRATION, t.REGISTRATION),
                  t.TYPE = COALESCE(s.TYPE, t.TYPE),
                  t.AIRCRAFT_DESC = COALESCE(s.AIRCRAFT_DESC, t.AIRCRAFT_DESC),
                  t.UPDATED_AT = s.UPDATED_AT,
                  t.SOURCE = s.SOURCE,
                  t.RAW_JSON = s.RAW_JSON
                WHEN NOT MATCHED THEN INSERT (ICAO_HEX, REGISTRATION, TYPE, AIRCRAFT_DESC, UPDATED_AT, SOURCE, RAW_JSON)
                VALUES (s.ICAO_HEX, s.REGISTRATION, s.TYPE, s.AIRCRAFT_DESC, s.UPDATED_AT, s.SOURCE, s.RAW_JSON)
            ''' % (db_schema)
            session.sql(merge_sql, params=[hx, reg, typ, desc, now, raw]).collect()
            updated += 1
        except Exception:
            errors += 1

    return "Enriched aircraft meta for %d hexes (errors=%d)" % (updated, errors)
$$;

CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_BACKFILL_ADSB_AIRCRAFT_DESC(p_days_back INT)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
  v_days INT;
  v_rows NUMBER(38,0);
BEGIN
  v_days := COALESCE(:p_days_back, 2);

  UPDATE {database}.{schema}.ADSB_DATA a
  SET
    AIRCRAFT_DESC = COALESCE(NULLIF(TRIM(a.AIRCRAFT_DESC), ''), m.AIRCRAFT_DESC),
    TYPE = COALESCE(NULLIF(TRIM(a.TYPE), ''), m.TYPE),
    REGISTRATION = COALESCE(NULLIF(TRIM(a.REGISTRATION), ''), m.REGISTRATION)
  FROM {database}.{schema}.HELPER_AIRCRAFT_META m
  WHERE a.ICAO_HEX = m.ICAO_HEX
    AND a.TIMESTAMP >= DATEADD('day', -:v_days, CURRENT_TIMESTAMP())
    AND (
      a.AIRCRAFT_DESC IS NULL OR TRIM(a.AIRCRAFT_DESC) = ''
      OR a.TYPE IS NULL OR TRIM(a.TYPE) = ''
      OR a.REGISTRATION IS NULL OR TRIM(a.REGISTRATION) = ''
    );

  v_rows := SQLROWCOUNT;
  RETURN 'Backfilled ADSB_DATA aircraft fields for last ' || v_days || ' days (rows=' || v_rows || ')';
END;
$$;

-- Wrapper so the TASK body is a single CALL (installer statement-splitting safe)
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_ENRICH_AIRCRAFT_META_AND_BACKFILL()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
  CALL {database}.{schema}.PROC_ENRICH_AIRCRAFT_META(200, 2, 24);
  CALL {database}.{schema}.PROC_BACKFILL_ADSB_AIRCRAFT_DESC(2);
  RETURN 'Aircraft meta enriched + ADSB_DATA backfilled';
END;
$$;

CREATE OR REPLACE TASK {database}.{schema}.TASK_ENRICH_AIRCRAFT_META_HOURLY
  WAREHOUSE = {warehouse}
  SCHEDULE = '60 MINUTE'
  ALLOW_OVERLAPPING_EXECUTION = FALSE
AS
  CALL {database}.{schema}.PROC_ENRICH_AIRCRAFT_META_AND_BACKFILL();

-- -----------------------------------------------------------------------------
-- Ingestion Procedure
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_INGEST_ADSB_REALTIME()
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'requests')
HANDLER = 'ingest'
EXTERNAL_ACCESS_INTEGRATIONS = ({eai_adsb_lol})
AS
$$
import requests
from datetime import datetime

def ingest(session):
    # API endpoint for aircraft within bounding box
    url = "{api_url}"
    
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return "API Error: " + str(e)
    
    aircraft = data.get('ac', [])
    if not aircraft:
        return "No aircraft data"
    
    now = datetime.utcnow()
    rows = []
    
    for ac in aircraft:
        if not ac.get('lat') or not ac.get('lon'):
            continue
        
        hex_code = ac.get('hex', '')
        flight = (ac.get('flight') or '').strip()
        
        # Handle 'ground' altitude
        alt_baro = ac.get('alt_baro')
        if alt_baro == 'ground':
            alt_baro = 0
        elif alt_baro is not None:
            try:
                alt_baro = int(alt_baro)
            except:
                alt_baro = None

        rows.append([
            hex_code,
            flight,
            ac.get('r', ''),
            ac.get('t', ''),
            ac.get('desc'),
            float(ac.get('lat')),
            float(ac.get('lon')),
            alt_baro,
            ac.get('alt_geom'),
            ac.get('gs'),
            ac.get('track'),
            ac.get('true_heading'),
            ac.get('baro_rate'),
            ac.get('squawk'),
            ac.get('category'),
            now,
            now
        ])
    
    if rows:
        from snowflake.snowpark.types import StructType, StructField, StringType, FloatType, IntegerType, TimestampType
        schema = StructType([
            StructField("HEX", StringType()),
            StructField("FLIGHT", StringType()),
            StructField("REGISTRATION", StringType()),
            StructField("AIRCRAFT_TYPE", StringType()),
            StructField("AIRCRAFT_DESC", StringType()),
            StructField("LAT", FloatType()),
            StructField("LON", FloatType()),
            StructField("ALT_BARO", IntegerType()),
            StructField("ALT_GEOM", IntegerType()),
            StructField("GROUND_SPEED", FloatType()),
            StructField("TRACK", FloatType()),
            StructField("TRUE_HEADING", FloatType()),
            StructField("VERTICAL_RATE", IntegerType()),
            StructField("SQUAWK", StringType()),
            StructField("CATEGORY", StringType()),
            StructField("TIMESTAMP", TimestampType()),
            StructField("INGESTED_AT", TimestampType())
        ])
        df = session.create_dataframe(rows, schema=schema)
        df.write.mode('append').save_as_table('{adsb_raw_table}')
    
    return "Inserted " + str(len(rows)) + " records"
$$;

-- -----------------------------------------------------------------------------
-- ETL to ADSB_DATA (canonical)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_ETL_ADSB_TO_DATA()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
    -- Use MERGE to make ADSB_DATA duplicate-proof even under repeated calls.
    MERGE INTO {database}.{schema}.ADSB_DATA s
    USING (
        SELECT
            -- CONCAT returns NULL if any input is NULL; historical data often has missing flight callsign.
            -- Make FLIGHT_KEY always non-null (stable per aircraft + (optional) flight + hour bucket).
            MD5(CONCAT(
                COALESCE(UPPER(hex), ''),
                ':',
                COALESCE(UPPER(TRIM(flight)), ''),
                ':',
                TO_VARCHAR(timestamp, 'YYYYMMDDHH24')
            )) AS FLIGHT_KEY,
            UPPER(hex) AS ICAO_HEX,
            UPPER(registration) AS REGISTRATION,
            aircraft_type AS TYPE,
            aircraft_desc AS AIRCRAFT_DESC,
            UPPER(TRIM(flight)) AS FLIGHT,
            timestamp AS TIMESTAMP,
            ST_MAKEPOINT(lon, lat) AS LOCATION,
            track AS TRACK,
            true_heading AS TRUE_HEADING,
            ground_speed AS VELOCITY,
            alt_baro AS ALTITUDE_BARO,
            alt_geom AS ALTITUDE_GEOM,
            vertical_rate AS VERTICAL_RATE,
            squawk AS SQUAWK,
            category AS CATEGORY,
            'ADSB_LOL' AS SOURCE,
            ingested_at AS INGESTED_AT
        FROM {database}.{schema}.HELPER_ADSB_LOL_RAW
        WHERE hex IS NOT NULL AND timestamp IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY UPPER(hex), timestamp
          ORDER BY ingested_at DESC
        ) = 1
    ) r
    ON s.ICAO_HEX = r.ICAO_HEX
   AND s.TIMESTAMP = r.TIMESTAMP
    WHEN MATCHED AND s.FLIGHT_KEY IS NULL THEN UPDATE SET
        FLIGHT_KEY = r.FLIGHT_KEY
    WHEN NOT MATCHED THEN INSERT (
        FLIGHT_KEY, ICAO_HEX, REGISTRATION, TYPE, AIRCRAFT_DESC, FLIGHT, TIMESTAMP, LOCATION, TRACK, TRUE_HEADING,
        VELOCITY, ALTITUDE_BARO, ALTITUDE_GEOM, VERTICAL_RATE, SQUAWK, CATEGORY, SOURCE, INGESTED_AT
    ) VALUES (
        r.FLIGHT_KEY, r.ICAO_HEX, r.REGISTRATION, r.TYPE, r.AIRCRAFT_DESC, r.FLIGHT, r.TIMESTAMP, r.LOCATION, r.TRACK, r.TRUE_HEADING,
        r.VELOCITY, r.ALTITUDE_BARO, r.ALTITUDE_GEOM, r.VERTICAL_RATE, r.SQUAWK, r.CATEGORY, r.SOURCE, r.INGESTED_AT
    );

    -- Backfill safety: if older loads produced NULL FLIGHT_KEY (because flight/callsign was missing),
    -- compute it directly in-place.
    UPDATE {database}.{schema}.ADSB_DATA
    SET FLIGHT_KEY = MD5(CONCAT(
        COALESCE(ICAO_HEX, ''),
        ':',
        COALESCE(UPPER(TRIM(FLIGHT)), ''),
        ':',
        TO_VARCHAR(TIMESTAMP, 'YYYYMMDDHH24')
    ))
    WHERE FLIGHT_KEY IS NULL
      AND ICAO_HEX IS NOT NULL
      AND TIMESTAMP IS NOT NULL;

    RETURN 'ETL Complete';
END;
$$;

-- -----------------------------------------------------------------------------
-- Cleanup helper: remove accidental duplicates in ADSB_DATA by (ICAO_HEX,TIMESTAMP)
-- This is safe: it retains the newest INGESTED_AT per key within the specified window.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_DEDUP_ADSB_DATA(p_days_back INT)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
  v_days INT;
  v_rows NUMBER(38,0);
BEGIN
  v_days := COALESCE(:p_days_back, 2);

  DELETE FROM {database}.{schema}.ADSB_DATA
  WHERE (ICAO_HEX, TIMESTAMP, INGESTED_AT) IN (
    SELECT ICAO_HEX, TIMESTAMP, INGESTED_AT
    FROM (
      SELECT
        ICAO_HEX,
        TIMESTAMP,
        INGESTED_AT,
        ROW_NUMBER() OVER (
          PARTITION BY ICAO_HEX, TIMESTAMP
          ORDER BY INGESTED_AT DESC
        ) AS rn
      FROM {database}.{schema}.ADSB_DATA
      WHERE TIMESTAMP::DATE >= DATEADD('day', -:v_days, CURRENT_DATE())
        AND ICAO_HEX IS NOT NULL
        AND TIMESTAMP IS NOT NULL
    )
    WHERE rn > 1
  );

  v_rows := SQLROWCOUNT;
  RETURN 'Deduped ADSB_DATA for last ' || v_days || ' days (deleted_rows=' || v_rows || ')';
END;
$$;

-- -----------------------------------------------------------------------------
-- Wrapper procedure for task (combines ingest + ETL)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_ADSB_INGEST_AND_ETL()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
    CALL {database}.{schema}.PROC_INGEST_ADSB_REALTIME();
    CALL {database}.{schema}.PROC_ETL_ADSB_TO_DATA();
    -- Extra safety: keep latest data duplicate-free so downstream MERGEs (enrichment) can't fail.
    CALL {database}.{schema}.PROC_DEDUP_ADSB_DATA(2);
    RETURN 'Ingest and ETL complete';
END;
$$;

-- -----------------------------------------------------------------------------
-- Scheduled Task (every minute)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TASK {database}.{schema}.TASK_INGEST_ADSB
  WAREHOUSE = {warehouse}
  SCHEDULE = '1 MINUTE'
  ALLOW_OVERLAPPING_EXECUTION = FALSE
AS
  CALL {database}.{schema}.PROC_ADSB_INGEST_AND_ETL();

-- Task is created SUSPENDED. To start:
-- ALTER TASK {database}.{schema}.TASK_INGEST_ADSB RESUME;

-- NOTE: Do NOT run an initial ingestion call during install.
-- In Streamlit execution context this may fail transiently due to external API timing,
-- and it isn't required because the task will run once resumed.
-- To run manually later:
--   CALL {database}.{schema}.PROC_ADSB_INGEST_AND_ETL();

-- =============================================================================
-- ADSB.LOL HISTORICAL BACKFILL
-- Source: adsb.lol globe_history (GitHub releases)
-- https://github.com/adsblol/globe_history
-- License: ODbL 1.0
-- =============================================================================

-- -----------------------------------------------------------------------------
-- External Network Access (for GitHub API, adsb.lol, and aircraft lookup)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE NETWORK RULE {database}.{schema}.{schema}_github_rule
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('api.github.com:443', 'github.com:443', 'objects.githubusercontent.com:443', 'release-assets.githubusercontent.com:443');

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION {eai_github}
  ALLOWED_NETWORK_RULES = ({database}.{schema}.{schema}_github_rule)
  ENABLED = TRUE;



-- =============================================================================
-- STAGE-BASED HISTORICAL ADS-B DATA PIPELINE (SQL-optimized)
-- Downloads TAR files to internal stage, extracts to NDJSON, 
-- then uses SQL FLATTEN + ST_DWITHIN for parallel filtering
-- =============================================================================

-- Internal stage for downloaded TAR files and extracted NDJSON
CREATE STAGE IF NOT EXISTS {database}.{schema}.ADSB_HISTORY_STAGE;

-- Tracking table for backfill status
CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS (
    data_date DATE PRIMARY KEY,
    download_status VARCHAR(20) DEFAULT 'pending',
    downloaded_at TIMESTAMP_NTZ,
    extracted_at TIMESTAMP_NTZ,
    loaded_at TIMESTAMP_NTZ,
    processed_at TIMESTAMP_NTZ,
    downloaded_parts INT,
    downloaded_bytes NUMBER(38,0),
    aircraft_extracted INT,
    rows_loaded INT,
    aircraft_found INT,
    points_inserted INT,
    error_message VARCHAR(500)
);

-- Backward/forward compatible schema upgrades
ALTER TABLE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS ADD COLUMN IF NOT EXISTS loaded_at TIMESTAMP_NTZ;
ALTER TABLE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS ADD COLUMN IF NOT EXISTS downloaded_parts INT;
ALTER TABLE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS ADD COLUMN IF NOT EXISTS downloaded_bytes NUMBER(38,0);
ALTER TABLE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS ADD COLUMN IF NOT EXISTS rows_loaded INT;

-- Interim table for raw aircraft JSON (one row per aircraft)
CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_ADSB_HISTORY_INTERIM (
    data_date DATE,
    raw_json VARIANT,
    loaded_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- =============================================================================
-- Procedure: Download TAR files to internal stage
-- Downloads split TAR parts from globe_history_YYYY to stage for later processing
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_DOWNLOAD_TO_STAGE(p_date VARCHAR)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'requests')
HANDLER = 'download_to_stage'
EXTERNAL_ACCESS_INTEGRATIONS = ({eai_github})
AS
$$
import requests
from datetime import datetime
from io import BytesIO
import string

def download_to_stage(session, p_date):
    '''Download TAR parts from adsb.lol to internal stage.'''
    try:
        date_obj = datetime.strptime(p_date, '%Y-%m-%d')
        year = date_obj.year
        date_dot = date_obj.strftime('%Y.%m.%d')
    except:
        return f"Invalid date: {{p_date}}"
    
    # Tag formats have changed over time; try a small set of known patterns.
    # NOTE: If the GitHub repo for the computed year doesn't exist yet (or a tag doesn't exist),
    # the first request will 404. We treat that as "try next tag".
    tags_to_try = [
        f"v{{date_dot}}-planes-readsb-prod-0",
        f"v{{date_dot}}-planes-readsb-prod",
        f"v{{date_dot}}-planes-readsb-prod-1",
    ]
    stage_path = f"@{database}.{schema}.ADSB_HISTORY_STAGE/{{p_date}}"
    
    # Suffixes are .tar.aa, .tar.ab, ... continue until 404
    suffixes = [a + b for a in string.ascii_lowercase for b in string.ascii_lowercase]

    chosen_tag = None
    parts_downloaded = 0
    total_bytes = 0
    saw_404 = False
    incomplete_err = None

    for tag in tags_to_try:
        base_url = f"https://github.com/adsblol/globe_history_{{year}}/releases/download/{{tag}}"

        # Resume-safe: skip parts that already exist on stage for this tag
        existing_suffixes = set()
        try:
            existing = session.sql(f"LIST {{stage_path}} PATTERN='.*{{tag}}\\\\.tar\\\\..*'").collect()
            for row in existing or []:
                try:
                    name = str(row[0])
                    size = int(row[1]) if row[1] is not None else 0
                    total_bytes += size
                    parts_downloaded += 1
                    existing_suffixes.add(name.split('.tar.')[-1])
                except Exception:
                    continue
        except Exception:
            existing_suffixes = set()

        # Try downloading missing parts for this tag
        saw_404 = False
        incomplete_err = None
        started_any = False

        for suffix in suffixes:
            if suffix in existing_suffixes:
                continue
            part_url = f"{{base_url}}/{{tag}}.tar.{{suffix}}"
            stage_file = f"{{stage_path}}/{{tag}}.tar.{{suffix}}"

            try:
                with requests.get(part_url, stream=True, timeout=600) as resp:
                    if resp.status_code == 404:
                        saw_404 = True
                        break
                    if resp.status_code in (401, 403, 429):
                        incomplete_err = f"HTTP {{resp.status_code}}"
                        break
                    if resp.status_code != 200:
                        continue

                    started_any = True
                    # Prefer true streaming: avoid buffering multi-GB parts in memory.
                    try:
                        if hasattr(resp, "raw") and resp.raw is not None:
                            try:
                                resp.raw.decode_content = False
                            except Exception:
                                pass
                            session.file.put_stream(resp.raw, stage_file, auto_compress=False, overwrite=True)
                            cl = resp.headers.get("Content-Length")
                            if cl and str(cl).isdigit():
                                total_bytes += int(cl)
                        else:
                            raise Exception("resp.raw not available")
                    except Exception:
                        buffer = BytesIO()
                        for chunk in resp.iter_content(chunk_size=10*1024*1024):
                            if not chunk:
                                continue
                            buffer.write(chunk)
                        buffer.seek(0)
                        total_bytes += buffer.getbuffer().nbytes
                        session.file.put_stream(buffer, stage_file, auto_compress=False, overwrite=True)

                parts_downloaded += 1
            except Exception as e:
                incomplete_err = str(e)[:200]
                break

        # If split TAR parts don't exist for this tag (404 on first part), try single-file artifacts.
        # Some days/tags may publish a single .tar.gz (or .tar) instead of split .tar.aa parts.
        if (parts_downloaded == 0) and saw_404 and (not started_any) and (not (incomplete_err and incomplete_err.startswith("HTTP "))):
            for ext in (".tar.gz", ".tgz", ".tar"):
                # NOTE: This code runs inside the Snowflake Python proc. Escape braces so the
                # installer's outer f-string doesn't try to evaluate base_url/tag/ext.
                one_url = f"{{base_url}}/{{tag}}{{ext}}"
                one_dest = f"{{stage_path}}/{{tag}}{{ext}}"
                try:
                    with requests.get(one_url, stream=True, timeout=600) as resp:
                        if resp.status_code == 404:
                            continue
                        if resp.status_code in (401, 403, 429):
                            # Escape braces so the installer's outer f-string doesn't evaluate `resp`.
                            incomplete_err = f"HTTP {{resp.status_code}}"
                            break
                        if resp.status_code != 200:
                            continue

                        try:
                            if hasattr(resp, "raw") and resp.raw is not None:
                                try:
                                    resp.raw.decode_content = False
                                except Exception:
                                    pass
                                session.file.put_stream(resp.raw, one_dest, auto_compress=False, overwrite=True)
                                cl = resp.headers.get("Content-Length")
                                if cl and str(cl).isdigit():
                                    total_bytes += int(cl)
                            else:
                                raise Exception("resp.raw not available")
                        except Exception:
                            buffer = BytesIO()
                            for chunk in resp.iter_content(chunk_size=10*1024*1024):
                                if not chunk:
                                    continue
                                buffer.write(chunk)
                            buffer.seek(0)
                            total_bytes += buffer.getbuffer().nbytes
                            session.file.put_stream(buffer, one_dest, auto_compress=False, overwrite=True)

                    parts_downloaded += 1
                    chosen_tag = tag
                    saw_404 = True  # treat as complete artifact
                    break
                except Exception as e:
                    incomplete_err = str(e)[:200]
                    break

            if chosen_tag:
                break

        # If we downloaded anything (or had existing parts), lock onto this tag.
        if parts_downloaded > 0:
            chosen_tag = tag
            break

        # If we got throttled/forbidden, stop early and record failure.
        if incomplete_err and incomplete_err.startswith("HTTP "):
            break

        # If we never saw a 404 and never downloaded anything, try next tag anyway.
        # If we DID see 404 immediately (first suffix), this tag likely doesn't exist; try next tag.
        continue
    
    if parts_downloaded == 0:
        # Distinguish "no release yet" from "blocked/throttled" (403/429).
        if incomplete_err and incomplete_err.startswith("HTTP "):
            try:
                session.sql(f'''
                    MERGE INTO {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS t
                    USING (SELECT '{{p_date}}'::DATE AS data_date) s ON t.data_date = s.data_date
                    WHEN MATCHED THEN UPDATE SET download_status = 'failed', error_message = 'GitHub download blocked: {{incomplete_err}}'
                    WHEN NOT MATCHED THEN INSERT (data_date, download_status, error_message)
                      VALUES (s.data_date, 'failed', 'GitHub download blocked: {{incomplete_err}}')
                ''').collect()
            except Exception:
                pass
            return f"Download blocked: {{incomplete_err}}"

        # Most commonly: the daily release for this date isn't published yet (e.g., "today").
        # Track as "not_available_yet" so an automated retry can pick it up later.
        try:
            session.sql(f'''
                MERGE INTO {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS t
                USING (SELECT '{{p_date}}'::DATE AS data_date) s ON t.data_date = s.data_date
                WHEN MATCHED THEN UPDATE SET
                  download_status = IFF(LOWER(t.download_status) IN ('extracted','loaded','processed'), t.download_status, 'not_available_yet'),
                  error_message = IFF(
                    LOWER(t.download_status) IN ('extracted','loaded','processed'),
                    error_message,
                    'No TAR parts found (release not published yet?)'
                  )
                WHEN NOT MATCHED THEN INSERT (data_date, download_status, error_message)
                  VALUES (s.data_date, 'not_available_yet', 'No TAR parts found (release not published yet?)')
            ''').collect()
        except Exception:
            pass
        return f"No TAR parts found for {{p_date}}"

    # Only mark fully "downloaded" when we see the first 404 (end-of-parts).
    # Otherwise, treat as partial/incomplete so downstream steps don't try to extract a truncated TAR.
    status = 'downloaded' if saw_404 else 'downloaded_partial'
    err_msg = (incomplete_err or '')
    try:
        session.sql(f'''
            MERGE INTO {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS t
            USING (SELECT '{{p_date}}'::DATE AS data_date) s ON t.data_date = s.data_date
            WHEN MATCHED THEN UPDATE SET 
                -- IMPORTANT: never downgrade status once a later stage has completed.
                download_status = IFF(LOWER(t.download_status) IN ('extracted','loaded','processed'), t.download_status, '{{status}}'),
                downloaded_at = CURRENT_TIMESTAMP(),
                downloaded_parts = {{parts_downloaded}},
                downloaded_bytes = {{total_bytes}},
                error_message = IFF(
                    LOWER(t.download_status) IN ('extracted','loaded','processed'),
                    error_message,
                    IFF('{{status}}' = 'downloaded_partial', LEFT('{{err_msg}}', 200), error_message)
                )
            WHEN NOT MATCHED THEN INSERT (data_date, download_status, downloaded_at, downloaded_parts, downloaded_bytes, error_message) 
                VALUES (s.data_date, '{{status}}', CURRENT_TIMESTAMP(), {{parts_downloaded}}, {{total_bytes}}, IFF('{{status}}' = 'downloaded_partial', LEFT('{{err_msg}}', 200), NULL))
        ''').collect()
    except Exception:
        pass

    if status == 'downloaded_partial':
        return f"Partial download: {{parts_downloaded}} parts ({{total_bytes/1024/1024:.1f}} MB) to {{stage_path}}. Retry to continue."
    return f"Downloaded {{parts_downloaded}} parts ({{total_bytes/1024/1024:.1f}} MB) to {{stage_path}}"
$$;

-- =============================================================================
-- Procedure: Extract TAR to batched NDJSON on stage (STREAMING)
-- PERFORMANCE: reduces stage writes from ~50k/day to ~tens/day
-- NOTE: We still avoid JSON parsing; we only gzip-decompress each trace file to get JSON bytes
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_EXTRACT_TO_NDJSON(p_date VARCHAR)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'extract_to_stage'
AS
$$
import tarfile
from io import BytesIO
from snowflake.snowpark.files import SnowflakeFile
import os
import gzip

class ChainedFiles:
    '''Chain multiple file objects into one seamless stream.'''
    def __init__(self, file_handles):
        self.files = iter(file_handles)
        self.current = next(self.files, None)
    
    def read(self, n=-1):
        if self.current is None:
            return b''
        chunks = []
        remaining = n
        while self.current is not None and (remaining != 0):
            data = self.current.read(remaining if remaining > 0 else -1)
            if not data:
                self.current = next(self.files, None)
            else:
                chunks.append(data)
                if remaining > 0:
                    remaining -= len(data)
        return b''.join(chunks)
    
    def close(self):
        if self.current:
            try: self.current.close()
            except: pass
        for f in self.files:
            try: f.close()
            except: pass

def extract_to_stage(session, p_date):
    '''Extract TAR and write batched NDJSON (.ndjson.gz) files to stage.
    
    STREAMING MODE:
    - Chains TAR parts without loading all into memory
    - Reads each member content immediately (required for streaming)
    - Buffers only one trace file at a time (+ one batch buffer)
    
    OUTPUT:
      @<database>.<schema>.ADSB_HISTORY_STAGE/<p_date>/ndjson/batch_0001.ndjson.gz
      @<database>.<schema>.ADSB_HISTORY_STAGE/<p_date>/ndjson/batch_0002.ndjson.gz
      ...
    '''
    stage_path = f"@{database}.{schema}.ADSB_HISTORY_STAGE/{{p_date}}"
    
    try:
        files_result = session.sql(f"LIST {{stage_path}}").collect()
        if not files_result:
            return f"No files found in stage for {{p_date}}"
    except Exception as e:
        return f"Error listing stage: {{str(e)[:100]}}"
    
    # Only process .tar files, skip extracted traces
    tar_files = sorted([f"@{database}.{schema}.{{row[0]}}" for row in files_result 
                       if '.tar.' in row[0] and '/traces/' not in row[0]])
    
    if not tar_files:
        return f"No TAR files found for {{p_date}}"

    ndjson_dir = f"@{database}.{schema}.ADSB_HISTORY_STAGE/{{p_date}}/ndjson/"
    # Smart resume:
    # - If NDJSON batches already exist AND status indicates extracted/loaded/processed, skip extraction.
    # - If batches exist but status does not, treat as partial and restart extraction for this day.
    try:
        existing_batches = session.sql(f"LIST {{ndjson_dir}} PATTERN='.*\\\\.ndjson\\\\.gz'").collect()
    except Exception:
        existing_batches = []

    if existing_batches:
        try:
            st = session.sql(f'''
                SELECT download_status, extracted_at
                FROM {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS
                WHERE data_date = '{{p_date}}'::DATE
                LIMIT 1
            ''').collect()
            if st:
                status = (st[0][0] or '').lower()
                extracted_at = st[0][1]
                if status in ('extracted', 'loaded', 'processed') and extracted_at is not None:
                    return f"Already extracted (found {{len(existing_batches)}} NDJSON batches) in {{ndjson_dir}}"
        except Exception:
            pass

        # Partial/unknown state: restart this day (safe, deterministic)
        try:
            session.sql(f"REMOVE {{ndjson_dir}};").collect()
        except Exception:
            pass
    
    # Open all TAR parts as file handles
    file_handles = []
    for file_path in tar_files:
        try:
            fh = SnowflakeFile.open(file_path, 'rb', require_scoped_url=False)
            file_handles.append(fh)
        except Exception as e:
            for h in file_handles:
                try: h.close()
                except: pass
            return f"Error opening {{file_path}}: {{str(e)[:200]}}"
    
    if not file_handles:
        return f"No TAR files could be opened for {{p_date}}"
    
    chained = ChainedFiles(file_handles)
    aircraft_written = 0
    skipped = 0

    # Tuning knobs (favor stability and fewer stage writes)
    min_member_bytes = 1024          # skip very small compressed members (likely empty stubs)
    batch_max_aircraft = 2000        # flush after N aircraft JSON objects
    batch_max_compressed_bytes = 64 * 1024 * 1024  # flush when gzip buffer exceeds ~64MB

    batch_idx = 1
    batch_buf = BytesIO()
    gz_out = gzip.GzipFile(fileobj=batch_buf, mode='wb')

    def flush_batch():
        nonlocal batch_idx, batch_buf, gz_out
        gz_out.close()
        batch_buf.seek(0)
        dest = f"@{database}.{schema}.ADSB_HISTORY_STAGE/{{p_date}}/ndjson/batch_{{batch_idx:04d}}.ndjson.gz"
        session.file.put_stream(batch_buf, dest, auto_compress=False, overwrite=True)
        batch_idx += 1
        batch_buf = BytesIO()
        gz_out = gzip.GzipFile(fileobj=batch_buf, mode='wb')
    
    try:
        # Streaming mode 'r|*' - must read member content immediately
        with tarfile.open(fileobj=chained, mode='r|*') as tar:
            for member in tar:
                name = member.name
                
                # Skip non-trace files - accept both .json and .json.gz
                if (name.startswith('./heatmap') or 
                    name.startswith('./acas') or 
                    name.startswith('./LICENSE') or 
                    name.startswith('./README') or
                    'trace_full_' not in name or 
                    not (name.endswith('.json') or name.endswith('.json.gz'))):
                    skipped += 1
                    continue
                
                # Heuristic: extremely small members are almost always empty traces/stubs
                try:
                    if int(getattr(member, 'size', 0) or 0) < min_member_bytes:
                        skipped += 1
                        continue
                except Exception:
                    pass

                f = tar.extractfile(member)
                if f is None:
                    continue
                
                # CRITICAL: In streaming mode, must read content IMMEDIATELY
                # before iterating to next member
                raw_bytes = f.read()

                # Files are commonly gzipped even when the extension is .json.
                # We avoid JSON parsing; just decompress to raw JSON bytes, then write NDJSON line.
                try:
                    json_bytes = gzip.decompress(raw_bytes)
                except Exception:
                    json_bytes = raw_bytes

                # Write one JSON object per line (NDJSON)
                gz_out.write(json_bytes.strip())
                # IMPORTANT: write newline as a byte without using a backslash escape in this embedded code.
                gz_out.write(bytes([10]))
                aircraft_written += 1

                # Flush periodically to keep memory bounded and reduce stage writes
                if (aircraft_written % batch_max_aircraft) == 0:
                    flush_batch()
                elif batch_buf.tell() >= batch_max_compressed_bytes:
                    flush_batch()
    
    except Exception as e:
        return f"TAR error: {{str(e)[:200]}}"
    finally:
        try:
            # flush remaining
            if aircraft_written % batch_max_aircraft != 0 and batch_buf.tell() > 0:
                flush_batch()
        except Exception:
            pass
        chained.close()
    
    if aircraft_written == 0:
        try:
            session.sql(f'''
                MERGE INTO {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS t
                USING (SELECT '{{p_date}}'::DATE AS data_date) s ON t.data_date = s.data_date
                WHEN MATCHED THEN UPDATE SET download_status = 'extract_failed', error_message = 'No aircraft traces extracted', extracted_at = CURRENT_TIMESTAMP()
                WHEN NOT MATCHED THEN INSERT (data_date, download_status, error_message, extracted_at)
                    VALUES (s.data_date, 'extract_failed', 'No aircraft traces extracted', CURRENT_TIMESTAMP())
            ''').collect()
        except Exception:
            pass
        return f"No aircraft traces extracted for {{p_date}}"
    
    # Update status
    try:
        session.sql(f'''
            UPDATE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS 
            SET download_status = 'extracted', 
                extracted_at = CURRENT_TIMESTAMP(),
                aircraft_extracted = {{aircraft_written}}
            WHERE data_date = '{{p_date}}'
        ''').collect()
    except:
        pass
    
    return f"Extracted {{aircraft_written}} aircraft traces to NDJSON batches (streaming, skipped {{skipped}} aux files)"
$$;

-- =============================================================================
-- Procedure: Load individual JSON files to interim table
-- Uses COPY INTO with COMPRESSION=AUTO - Snowflake handles gzip decompression
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_LOAD_NDJSON_TO_INTERIM(p_date VARCHAR)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'load_json_files'
AS
$$
def load_json_files(session, p_date):
    '''Load batched NDJSON (.ndjson.gz) files using COPY INTO.
    
    Snowflake handles gzip decompression and JSON parsing; each NDJSON line becomes one VARIANT row.
    '''
    # Clear any existing data for this date
    session.sql(f"DELETE FROM {database}.{schema}.HELPER_ADSB_HISTORY_INTERIM WHERE data_date = '{{p_date}}'").collect()
    
    stage_dir = f"@{database}.{schema}.ADSB_HISTORY_STAGE/{{p_date}}/ndjson/"

    try:
        listed = session.sql(f"LIST {{stage_dir}} PATTERN='.*\\\\.ndjson\\\\.gz'").collect()
    except Exception as e:
        return f"Error listing ndjson batches: {{str(e)[:200]}}"

    if not listed:
        msg = f"No NDJSON batch files found under {{stage_dir}}"
        try:
            session.sql(f'''
                UPDATE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS
                SET download_status = 'load_failed',
                    error_message = '{{msg[:200]}}'
                WHERE data_date = '{{p_date}}'
            ''').collect()
        except Exception:
            pass
        return msg
    
    try:
        copy_sql = f'''
            COPY INTO {database}.{schema}.HELPER_ADSB_HISTORY_INTERIM (data_date, raw_json, loaded_at)
            FROM (
                SELECT 
                    '{{p_date}}'::DATE,
                    $1,
                    CURRENT_TIMESTAMP()
                FROM {{stage_dir}}
            )
            FILE_FORMAT = (TYPE = JSON COMPRESSION = GZIP)
            PATTERN = '.*\\\\.ndjson\\\\.gz'
            ON_ERROR = CONTINUE
        '''
        session.sql(copy_sql).collect()

        rows_loaded = session.sql(
            f"SELECT COUNT(*) FROM {database}.{schema}.HELPER_ADSB_HISTORY_INTERIM WHERE data_date = '{{p_date}}'::DATE"
        ).collect()[0][0]

        try:
            session.sql(f'''
                UPDATE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS
                SET loaded_at = CURRENT_TIMESTAMP(),
                    rows_loaded = {{rows_loaded}},
                    download_status = IFF(download_status = 'extracted', 'loaded', download_status)
                WHERE data_date = '{{p_date}}'
            ''').collect()
        except Exception:
            pass

        return f"Loaded {{rows_loaded}} aircraft to interim table from NDJSON batches"
    except Exception as e:
        try:
            session.sql(f'''
                UPDATE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS
                SET download_status = 'load_failed',
                    error_message = '{{str(e)[:200]}}'
                WHERE data_date = '{{p_date}}'
            ''').collect()
        except Exception:
            pass
        return f"Error loading JSON files: {{str(e)[:200]}}"
$$;

-- =============================================================================
-- Procedure: Filter and insert using SQL ST_DWITHIN (parallel processing)
-- This is where the magic happens - Snowflake parallelizes the filtering
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_FILTER_AND_INSERT_SQL(p_date VARCHAR)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
    v_aircraft_count INT;
    v_points_inserted INT;
BEGIN
    -- Count aircraft in interim
    SELECT COUNT(*) INTO v_aircraft_count FROM {database}.{schema}.HELPER_ADSB_HISTORY_INTERIM WHERE data_date = :p_date::DATE;

    -- Restart-safe processing: remove any previously inserted points for this day (user-approved).
    DELETE FROM {database}.{schema}.HELPER_ADSB_LOL_RAW
    WHERE timestamp::DATE = :p_date::DATE;
    
    -- Insert filtered points using ST_DWITHIN (50km = 50000 meters)
    -- This runs in parallel across all Snowflake workers
    INSERT INTO {database}.{schema}.HELPER_ADSB_LOL_RAW (
        hex, flight, registration, aircraft_type, aircraft_desc,
        lat, lon, alt_baro, alt_geom,
        ground_speed, track, true_heading, vertical_rate,
        squawk, category, timestamp, ingested_at
    )
    WITH airport AS (
        SELECT geometry FROM {database}.{schema}.PROPERTIES_AIRPORT LIMIT 1
    )
    SELECT 
        UPPER(i.raw_json:icao::VARCHAR) AS hex,
        -- Historical ADSB.lol "trace_full" files often do NOT have a top-level "flight".
        -- Instead, callsign/flight appears inside the per-point metadata object within the trace array
        -- (commonly at pt.value[8]:flight). Use that, and propagate within the aircraft stream.
        COALESCE(
            NULLIF(UPPER(TRIM(i.raw_json:flight::VARCHAR)), ''),
            NULLIF(UPPER(TRIM(pt.value[8]:flight::VARCHAR)), ''),
            NULLIF(
                MAX(NULLIF(UPPER(TRIM(pt.value[8]:flight::VARCHAR)), ''))
                  OVER (PARTITION BY UPPER(i.raw_json:icao::VARCHAR)),
                ''
            )
        ) AS flight,
        UPPER(TRIM(i.raw_json:r::VARCHAR)) AS registration,
        i.raw_json:t::VARCHAR AS aircraft_type,
        -- Historical schema: aircraft description is top-level "desc"
        -- Use quoted key access for robustness and normalize blanks to NULL.
        NULLIF(TRIM(COALESCE(
            i.raw_json:"desc"::VARCHAR,
            i.raw_json:desc::VARCHAR
        )), '') AS aircraft_desc,
        pt.value[1]::FLOAT AS lat,
        pt.value[2]::FLOAT AS lon,
        CASE 
            WHEN pt.value[3]::VARCHAR = 'ground' OR pt.value[3] IS NULL THEN 0
            ELSE TRY_CAST(pt.value[3]::VARCHAR AS INT)
        END AS alt_baro,
        TRY_CAST(pt.value[8]:alt_geom::VARCHAR AS INT) AS alt_geom,
        pt.value[4]::FLOAT AS ground_speed,
        COALESCE(
            TRY_CAST(pt.value[8]:track::VARCHAR AS FLOAT),
            pt.value[5]::FLOAT
        ) AS track,
        COALESCE(
            TRY_CAST(pt.value[8]:true_heading::VARCHAR AS FLOAT),
            pt.value[8]:true_heading::FLOAT
        ) AS true_heading,
        COALESCE(
            TRY_CAST(pt.value[8]:baro_rate::VARCHAR AS INT),
            TRY_CAST(pt.value[8]:geom_rate::VARCHAR AS INT),
            TRY_CAST(pt.value[7]::VARCHAR AS INT)
        ) AS vertical_rate,
        COALESCE(
            NULLIF(UPPER(TRIM(pt.value[8]:squawk::VARCHAR)), ''),
            NULLIF(
                MAX(NULLIF(UPPER(TRIM(pt.value[8]:squawk::VARCHAR)), ''))
                  OVER (PARTITION BY UPPER(i.raw_json:icao::VARCHAR)),
                ''
            )
        ) AS squawk,
        COALESCE(
            NULLIF(UPPER(TRIM(pt.value[8]:category::VARCHAR)), ''),
            NULLIF(
                MAX(NULLIF(UPPER(TRIM(pt.value[8]:category::VARCHAR)), ''))
                  OVER (PARTITION BY UPPER(i.raw_json:icao::VARCHAR)),
                ''
            )
        ) AS category,
        -- pt.value[0] is seconds (often fractional) since raw_json:timestamp epoch
        TIMESTAMPADD(
            'millisecond',
            -- pt.value[0] is VARIANT->FLOAT; ROUND() returns numeric already.
            -- Using TRY_CAST here can fail compilation depending on inferred numeric types, so use a plain CAST.
            CAST(ROUND(COALESCE(pt.value[0]::FLOAT, 0) * 1000) AS INT),
            TO_TIMESTAMP(i.raw_json:timestamp::NUMBER)
        ) AS timestamp,
        CURRENT_TIMESTAMP() AS ingested_at
    FROM {database}.{schema}.HELPER_ADSB_HISTORY_INTERIM i,
         LATERAL FLATTEN(input => i.raw_json:trace) pt,
         airport a
    WHERE i.data_date = :p_date::DATE
      AND i.raw_json:trace IS NOT NULL
      AND ARRAY_SIZE(i.raw_json:trace) > 0
      AND pt.value[1] IS NOT NULL
      AND pt.value[2] IS NOT NULL
      AND ST_DWITHIN(
          ST_MAKEPOINT(pt.value[2]::FLOAT, pt.value[1]::FLOAT),
          a.geometry,
          50000
      );

    -- Exact inserted row count for this run
    v_points_inserted := SQLROWCOUNT;
    
    -- Update status
    UPDATE {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS 
    SET download_status = 'processed', 
        processed_at = CURRENT_TIMESTAMP(),
        aircraft_found = :v_aircraft_count,
        points_inserted = :v_points_inserted
    WHERE data_date = :p_date::DATE;
    
    -- Clean up interim table for this date
    DELETE FROM {database}.{schema}.HELPER_ADSB_HISTORY_INTERIM WHERE data_date = :p_date::DATE;
    
    -- Run ETL to silver
    CALL {database}.{schema}.PROC_ETL_ADSB_TO_DATA();
    
    RETURN 'Filtered ' || v_aircraft_count || ' aircraft, inserted ' || v_points_inserted || ' points within 50km';
END;
$$;

-- =============================================================================
-- Procedure: Combined process (Extract + Load + Filter)
-- Entry point that orchestrates the 3-step process
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_PROCESS_FROM_STAGE(p_date VARCHAR)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'process_day'
AS
$$
def process_day(session, p_date):
    # Snowflake Scripting does NOT allow "CALL ... INTO var" inside SQL procedures.
    # This Python orchestrator keeps the same behavior but safely captures return values.

    # Skip if already processed and raw has data for this date
    try:
        # IMPORTANT: avoid triple-quoted strings here because this procedure body itself is embedded
        # inside the installer's triple-quoted SQL templates.
        already_sql = (
            "SELECT IFF( "
            "  EXISTS ( "
            "    SELECT 1 FROM {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS "
            "    WHERE data_date = '" + p_date + "'::DATE AND LOWER(download_status) = 'processed' "
            "  ) "
            "  AND EXISTS ( "
            "    SELECT 1 FROM {database}.{schema}.HELPER_ADSB_LOL_RAW "
            "    WHERE timestamp::DATE = '" + p_date + "'::DATE LIMIT 1 "
            "  ), "
            "  1, 0 "
            ")"
        )
        already = session.sql(already_sql).collect()
        if already and int(already[0][0]) == 1:
            return "Already processed " + str(p_date)
    except Exception:
        # If this check fails for any reason, continue with processing (still restart-safe).
        pass

    def call1(sql):
        res = session.sql(sql).collect()
        return (res[0][0] if res else None) or ""

    # Step 0: Ensure TAR parts exist (download is resume-safe)
    download_msg = call1("CALL {database}.{schema}.PROC_DOWNLOAD_TO_STAGE('" + p_date + "')")
    low0 = (download_msg or "").lower()
    if ("download failed:" in low0) or ("no tar parts" in low0) or ("partial download:" in low0):
        return download_msg

    # Step 1: Extract TAR to stage (streaming)
    extract_msg = call1("CALL {database}.{schema}.PROC_EXTRACT_TO_NDJSON('" + p_date + "')")
    low = extract_msg.lower()
    if low.startswith("error") or low.startswith("tar error:") or ("no aircraft traces" in low):
        return download_msg + " | " + extract_msg

    # Step 2: Load extracted NDJSON batches
    load_msg = call1("CALL {database}.{schema}.PROC_LOAD_NDJSON_TO_INTERIM('" + p_date + "')")
    low = load_msg.lower()
    if low.startswith("error") or ("error loading" in low):
        return download_msg + " | " + extract_msg + " | " + load_msg

    # Step 3: Filter with SQL and insert
    filter_msg = call1("CALL {database}.{schema}.PROC_FILTER_AND_INSERT_SQL('" + p_date + "')")
    return download_msg + " | " + extract_msg + " | " + load_msg + " | " + filter_msg
$$;

-- =============================================================================
-- Procedure: Backfill last {adsb_history_backfill_days} UTC days (download + process), ending yesterday
-- Configurable via installer UI.
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_BACKFILL_ADSB_WEEK()
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'backfill_week'
AS
$$
from datetime import datetime, timedelta

def backfill_week(session):
    '''Backfill last N UTC days ending yesterday (N injected at install time).'''
    results = []
    
    end_date = datetime.utcnow().date() - timedelta(days=1)  # Yesterday
    n_days = {adsb_history_backfill_days}
    if n_days is None or int(n_days) < 1:
        return "Backfill skipped (adsb_history_backfill_days < 1)"
    start_date = end_date - timedelta(days=int(n_days) - 1)
    
    # Phase 1: Download all days
    results.append("=== DOWNLOADING ===")
    current = start_date
    while current <= end_date:
        date_str = current.strftime('%Y-%m-%d')
        try:
            result = session.sql(f"CALL {database}.{schema}.PROC_DOWNLOAD_TO_STAGE('{{date_str}}')").collect()
            msg = result[0][0] if result else "No result"
            results.append(f"{{date_str}}: {{msg}}")
        except Exception as e:
            results.append(f"{{date_str}}: Download error - {{str(e)[:100]}}")
        current += timedelta(days=1)
    
    # Phase 2: Process all days
    results.append("=== PROCESSING ===")
    current = start_date
    while current <= end_date:
        date_str = current.strftime('%Y-%m-%d')
        try:
            result = session.sql(f"CALL {database}.{schema}.PROC_PROCESS_FROM_STAGE('{{date_str}}')").collect()
            msg = result[0][0] if result else "No result"
            results.append(f"{{date_str}}: {{msg}}")
        except Exception as e:
            results.append(f"{{date_str}}: Process error - {{str(e)[:100]}}")
        current += timedelta(days=1)

    # Cleanup ONLY when all days are processed successfully
    try:
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        processed_cnt = session.sql(
            f"SELECT COUNT(*) FROM {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS "
            f"WHERE data_date BETWEEN '{{start_str}}'::DATE AND '{{end_str}}'::DATE "
            f"AND LOWER(download_status) = 'processed'"
        ).collect()[0][0]
    except Exception:
        processed_cnt = 0

    if processed_cnt == int(n_days):
        # Avoid nested f-string braces here (this Python proc is generated by a Python f-string)
        results.append("=== CLEANUP (all %d days processed) ===" % int(n_days))
        current = start_date
        while current <= end_date:
            date_str = current.strftime('%Y-%m-%d')
            try:
                session.sql(f"CALL {database}.{schema}.PROC_CLEANUP_STAGE('{{date_str}}')").collect()
                results.append(f"{{date_str}}: cleaned stage")
            except Exception as e:
                results.append(f"{{date_str}}: cleanup error - {{str(e)[:100]}}")
            current += timedelta(days=1)
    else:
        results.append(f"=== CLEANUP SKIPPED (processed {{processed_cnt}}/{adsb_history_backfill_days} days) ===")
    
    return "\\n".join(results)
$$;

-- =============================================================================
-- Procedure: Kick off historical backfill as a one-time background task
-- Runs server-side, so Streamlit can be closed; task self-suspends when done.
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_START_BACKFILL_LAST_WEEK()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
  -- Create/replace a one-time task that runs soon (every minute) and self-suspends after the first run.
  -- NOTE: Some Snowflake accounts/regions don't support SYSTEM$TASK_FORCE_RUN; we avoid it for portability.
  EXECUTE IMMEDIATE '
    CREATE OR REPLACE TASK {database}.{schema}.TASK_ADSB_BACKFILL_ONCE
      WAREHOUSE = {warehouse}
      SCHEDULE = ''1 MINUTE''
      -- 5-day backfill can take a while (downloads + extract + SQL filter). Give it room.
      USER_TASK_TIMEOUT_MS = 86400000
      ALLOW_OVERLAPPING_EXECUTION = FALSE
    AS
      CALL {database}.{schema}.PROC_RUN_BACKFILL_ONCE();
  ';

  EXECUTE IMMEDIATE 'ALTER TASK {database}.{schema}.TASK_ADSB_BACKFILL_ONCE RESUME';
  RETURN 'Started TASK_ADSB_BACKFILL_ONCE. It will run within ~1 minute and then self-suspend. Monitor {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS.';
END;
$$;

-- Wrapper procedure invoked by the task; self-suspends the task after completion
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_RUN_BACKFILL_ONCE()
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run_once'
AS
$$
def run_once(session):
    msg = ""
    try:
        res = session.sql("CALL {database}.{schema}.PROC_BACKFILL_ADSB_WEEK()").collect()
        msg = (res[0][0] if res else None) or ""
        
        # After backfill completes, enrich the full window with schedule data
        try:
            enrich_res = session.sql("CALL {database}.{schema}.PROC_ENRICH_ADSB_WITH_SCHEDULE({adsb_history_backfill_days})").collect()
            enrich_msg = (enrich_res[0][0] if enrich_res else None) or ""
            msg += " | " + enrich_msg
        except Exception as e:
            msg += " | Enrichment failed: " + str(e)[:200]
    except Exception as e:
        msg = "Backfill failed: " + str(e)[:200]
    # Always try to self-suspend the one-time task, even on errors.
    try:
        session.sql("ALTER TASK {database}.{schema}.TASK_ADSB_BACKFILL_ONCE SUSPEND").collect()
    except Exception:
        pass
    return msg
$$;

-- =============================================================================
-- Continuous retry backfill (UTC): keep trying yesterday + today until available,
-- and trigger enrichment + derived refresh after a day is successfully processed.
-- This closes the "start-day gap" (midnight -> ingestion start time) as soon as
-- the daily history release for "today" becomes available (often the next day).
-- =============================================================================

CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_RUN_BACKFILL_RETRY_UTC()
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run_retry'
AS
$$
from datetime import datetime, timedelta

def _get_status(session, date_str):
    try:
        rows = session.sql(
            "SELECT LOWER(download_status) AS st FROM {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS "
            "WHERE data_date = '%s'::DATE" % (date_str,)
        ).collect()
        return (rows[0][0] if rows else None) or None
    except Exception:
        return None

def _ensure_row(session, date_str):
    try:
        session.sql(
            "MERGE INTO {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS t "
            "USING (SELECT '%s'::DATE AS data_date) s ON t.data_date = s.data_date "
            "WHEN NOT MATCHED THEN INSERT (data_date, download_status) VALUES (s.data_date, 'pending')"
            % (date_str,)
        ).collect()
    except Exception:
        pass

def _get_config_backfill_days(session):
    # Read configured adsb_history_backfill_days from HELPER_MONITOR_LAST_REFRESH.
    try:
        rows = session.sql(
            "SELECT COALESCE(row_count_24h, 7) AS val FROM {database}.{schema}.HELPER_MONITOR_LAST_REFRESH "
            "WHERE table_name = 'CONFIG_ADSB_BACKFILL_DAYS'"
        ).collect()
        return int(rows[0][0]) if rows else 7
    except Exception:
        return 7

def run_retry(session):
    today = datetime.utcnow().date()
    dates = [today - timedelta(days=1), today]  # yesterday (expected) + today (best-effort)
    results = []

    # Read configured backfill window to use for enrichment lookback
    config_backfill_days = _get_config_backfill_days(session)
    max_enrich_days = config_backfill_days + 1  # +1 to cover edge cases

    for d in dates:
        date_str = d.strftime('%Y-%m-%d')
        _ensure_row(session, date_str)

        before = _get_status(session, date_str)
        if before == 'processed':
            results.append("%s: already processed" % (date_str,))
            continue

        # Process (download/extract/load/filter); this is resume-safe per date.
        try:
            r = session.sql("CALL {database}.{schema}.PROC_PROCESS_FROM_STAGE('%s')" % (date_str,)).collect()
            msg = (r[0][0] if r else None) or ""
        except Exception as e:
            msg = "process failed: " + str(e)[:200]

        after = _get_status(session, date_str)
        results.append("%s: %s (status=%s)" % (date_str, msg, after))

        # If this run newly completed the day, trigger enrichment + refresh now.
        if after == 'processed' and before != 'processed':
            try:
                days_back = (today - d).days + 1
                if days_back < 1:
                    days_back = 1
                # Use configured backfill window + 1 to ensure historical data gets enriched
                if days_back > max_enrich_days:
                    days_back = max_enrich_days
                session.sql("CALL {database}.{schema}.PROC_ENRICH_ADSB_WITH_SCHEDULE(%d)" % (int(days_back),)).collect()
                session.sql("CALL {database}.{schema}.PROC_REFRESH_DERIVED()").collect()
                results.append("%s: triggered enrich+refresh (days_back=%d, config=%d)" % (date_str, int(days_back), config_backfill_days))
            except Exception as e:
                results.append("%s: enrich/refresh failed: %s" % (date_str, str(e)[:200]))

    return "\\n".join(results)
$$;

-- Task wrapper: keep TASK body as a single CALL (installer statement-splitting safe)
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_START_BACKFILL_RETRY_UTC()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
  EXECUTE IMMEDIATE '
    CREATE OR REPLACE TASK {database}.{schema}.TASK_ADSB_BACKFILL_RETRY_HOURLY
      WAREHOUSE = {warehouse}
      SCHEDULE = ''60 MINUTE''
      USER_TASK_TIMEOUT_MS = 21600000
      ALLOW_OVERLAPPING_EXECUTION = FALSE
    AS
      CALL {database}.{schema}.PROC_RUN_BACKFILL_RETRY_UTC();
  ';

  EXECUTE IMMEDIATE 'ALTER TASK {database}.{schema}.TASK_ADSB_BACKFILL_RETRY_HOURLY RESUME';
  RETURN 'Started TASK_ADSB_BACKFILL_RETRY_HOURLY (yesterday+today UTC retry + enrich). Monitor HELPER_ADSB_BACKFILL_STATUS.';
END;
$$;

-- =============================================================================
-- Procedure: Cleanup stage after processing
-- =============================================================================
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_CLEANUP_STAGE(p_date VARCHAR)
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
    REMOVE @{database}.{schema}.ADSB_HISTORY_STAGE/:p_date/;
    RETURN 'Cleaned up ' || :p_date;
END;
$$;

-- =============================================================================
-- USAGE:
-- Download one day to stage:
--   CALL {database}.{schema}.PROC_DOWNLOAD_TO_STAGE('2025-12-15');
--
-- Process from stage:
--   CALL {database}.{schema}.PROC_PROCESS_FROM_STAGE('2025-12-15');
--
-- Backfill full week (download + process):
--   CALL {database}.{schema}.PROC_BACKFILL_ADSB_WEEK();
--
-- Check status:
--   SELECT * FROM {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS;
--
-- Cleanup stage:
--   CALL {database}.{schema}.PROC_CLEANUP_STAGE('2025-12-15');
-- =============================================================================
"""


def generate_derived_sql(airport: dict, database: str, schema: str, warehouse: str, adsb_history_backfill_days: int = 5) -> str:
    """Generate derived analytics SQL."""
    installer_sha = _get_git_sha_short()
    installer_generated_at = datetime.utcnow().isoformat()
    adsb_history_backfill_days = int(adsb_history_backfill_days or 5)
    return f"""-- =============================================================================
-- DERIVED ANALYTICS FOR {airport['name']} ({airport['iata_code']})
-- Database: {database}.{schema}
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Install audit (versioning / provenance)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_INSTALL_AUDIT (
  installed_at TIMESTAMP_NTZ,
  installer_git_sha STRING,
  installer_generated_at TIMESTAMP_NTZ,
  airport_code STRING,
  database_name STRING,
  schema_name STRING,
  notes STRING
);

INSERT INTO {database}.{schema}.HELPER_INSTALL_AUDIT
SELECT
  CURRENT_TIMESTAMP(),
  '{installer_sha}',
  TO_TIMESTAMP_NTZ('{installer_generated_at}'),
  '{airport['iata_code']}',
  '{database}',
  '{schema}',
  'derived install';

-- -----------------------------------------------------------------------------
-- Dashboard prerequisites (ensure objects exist even in troubleshooting mode)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {database}.{schema}.PROPERTIES_GATES (gate_id STRING, gate_name STRING, gate_geom GEOGRAPHY);
CREATE TABLE IF NOT EXISTS {database}.{schema}.PROPERTIES_RUNWAYS (
  runway_id STRING,
  runway_geog GEOGRAPHY
);

-- PROPERTIES_RUNWAYS is the only runway object we need (single unioned GEOGRAPHY row).

-- -----------------------------------------------------------------------------
-- Prerequisites
-- PROPERTIES_RUNWAYS is created as exactly one row during base install, with fallback to airport centroid.
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- 0. ADSB_DATA_LOCAL (airport-relevant points only)
-- A point is included if its flight-day is either:
--   - Local O/D (schedule enrichment says origin or destination is this airport), OR
--   - "Touched airport": any near-airport ground-like point that day (alt<=50ft, speed<=40kts, within 5km)
-- This is a derived convenience layer for dashboards; ADSB_DATA remains the raw point truth.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.ADSB_DATA_LOCAL
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH airport AS (
  SELECT
    UPPER(airport_code) AS airport_code,
    geometry AS airport_geom
  FROM {database}.{schema}.PROPERTIES_AIRPORT
  LIMIT 1
),
pts AS (
  SELECT
    a.*,
    a.TIMESTAMP::DATE AS service_date,
    COALESCE(NULLIF(TRIM(a.FLIGHT), ''), a.ICAO_HEX) AS flight_id
  FROM {database}.{schema}.ADSB_DATA a
  WHERE a.ICAO_HEX IS NOT NULL
    AND a.TIMESTAMP IS NOT NULL
),
flags AS (
  SELECT
    p.service_date,
    p.flight_id,
    MAX(IFF(COALESCE(p.IS_LOCAL_OD, FALSE), 1, 0)) AS is_local_od_any,
    MAX(
      IFF(
        airport.airport_geom IS NOT NULL
        AND p.LOCATION IS NOT NULL
        AND p.ALTITUDE_BARO IS NOT NULL AND p.ALTITUDE_BARO <= 50
        AND COALESCE(p.VELOCITY, 0) <= 40
        AND ST_DWITHIN(p.LOCATION, airport.airport_geom, 5000),
        1, 0
      )
    ) AS touched_airport_any
  FROM pts p
  CROSS JOIN airport
  GROUP BY 1, 2
),
relevant AS (
  SELECT service_date, flight_id
  FROM flags
  WHERE is_local_od_any = 1 OR touched_airport_any = 1
)
SELECT p.*
FROM pts p
JOIN relevant r
  ON r.service_date = p.service_date
 AND r.flight_id = p.flight_id;

-- Keep the canonical name only (avoid confusion).
DROP VIEW IF EXISTS {database}.{schema}.ADSB_DATA_LOVAL;

-- -----------------------------------------------------------------------------
-- 1. Gate Analysis derived tables (also reused by Runway Crossings)
-- -----------------------------------------------------------------------------
-- Canonical join keys:
--   - service_date: TIMESTAMP::DATE (UTC date)
--   - aircraft_day_id: MD5(ICAO_HEX || ':' || service_date)
--   - ground_session_id: MD5(ICAO_HEX || ':' || service_date || ':' || session_seq)
-- These keys avoid reliance on callsign/flight_key for historical data.

CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_AIRCRAFT_GROUND_SESSIONS
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH ground AS (
  SELECT
    ICAO_HEX,
    REGISTRATION,
    TIMESTAMP::DATE AS service_date,
    TIMESTAMP AS ts,
    LOCATION,
    VELOCITY,
    ALTITUDE_BARO
  FROM {database}.{schema}.ADSB_DATA_LOCAL
  WHERE ICAO_HEX IS NOT NULL
    AND TIMESTAMP IS NOT NULL
    AND LOCATION IS NOT NULL
    AND ALTITUDE_BARO IS NOT NULL
    AND ALTITUDE_BARO <= 50
    AND COALESCE(VELOCITY, 0) <= 40
),
lagged AS (
  SELECT
    *,
    DATEDIFF('minute', LAG(ts) OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts), ts) AS gap_min
  FROM ground
),
sessioned AS (
  SELECT
    *,
    SUM(IFF(COALESCE(gap_min, 999999) > 20, 1, 0))
      OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts ROWS UNBOUNDED PRECEDING) AS session_seq
  FROM lagged
),
agg AS (
  SELECT
    ICAO_HEX,
    service_date,
    session_seq,
    MD5(CONCAT(ICAO_HEX, ':', TO_VARCHAR(service_date), ':', TO_VARCHAR(session_seq))) AS ground_session_id,
    MIN(ts) AS start_ts,
    MAX(ts) AS end_ts,
    DATEDIFF('second', MIN(ts), MAX(ts)) AS dwell_seconds,
    MAX(REGISTRATION) AS registration,
    COUNT(*) AS points
  FROM sessioned
  GROUP BY 1, 2, 3
)
SELECT * FROM agg;

CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_ADSB_GROUND_POINTS
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH ground AS (
  SELECT
    flight_key,
    ICAO_HEX,
    REGISTRATION,
    FLIGHT,
    TIMESTAMP::DATE AS service_date,
    TIMESTAMP AS ts,
    LOCATION,
    VELOCITY,
    ALTITUDE_BARO
  FROM {database}.{schema}.ADSB_DATA_LOCAL
  -- Altitude on the ground can be noisy (sometimes small positive/negative values).
  -- Treat "ground" as near-zero altitude + low speed.
  WHERE timestamp IS NOT NULL
    AND altitude_baro IS NOT NULL
    AND altitude_baro <= 50
    AND COALESCE(velocity, 0) <= 40
),
with_lag AS (
  SELECT *,
    TIMESTAMPDIFF('second', LAG(ts) OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts), ts) AS lag_seconds,
    DATEDIFF('minute', LAG(ts) OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts), ts) AS gap_min
  FROM ground
),
with_session AS (
  SELECT
    *,
    SUM(IFF(COALESCE(gap_min, 999999) > 20, 1, 0))
      OVER (PARTITION BY ICAO_HEX, service_date ORDER BY ts ROWS UNBOUNDED PRECEDING) AS session_seq
  FROM with_lag
)
SELECT
  MD5(CONCAT(w.ICAO_HEX, ':', TO_VARCHAR(w.service_date), ':', TO_VARCHAR(w.session_seq))) AS ground_session_id,
  MD5(CONCAT(w.ICAO_HEX, ':', TO_VARCHAR(w.service_date))) AS aircraft_day_id,
  w.service_date,
  w.session_seq,
  w.ICAO_HEX,
  w.REGISTRATION,
  w.flight_key,
  w.flight,
  w.ts,
  w.LOCATION,
  w.velocity,
  COALESCE(w.lag_seconds, 0) AS lag_seconds,
  g.gate_name AS closest_gate_name
FROM with_session w
-- Gates from Overture Infrastructure are POINT markers; use a wider tolerance than "jetway line" geometry.
-- Keep this aligned with dashboard-side dwell approximation (default ~120m).
LEFT JOIN {database}.{schema}.PROPERTIES_GATES g ON ST_DWITHIN(w.LOCATION, g.gate_geom, 120)
QUALIFY ROW_NUMBER() OVER (PARTITION BY w.ICAO_HEX, w.service_date, w.ts ORDER BY ST_DISTANCE(w.LOCATION, g.gate_geom) ASC NULLS LAST) = 1;

-- -----------------------------------------------------------------------------
-- 2. Gate Analysis summaries
-- -----------------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_FLIGHT_GATE_TIME
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH per_gate AS (
  SELECT
    ground_session_id,
    aircraft_day_id,
    service_date,
    ICAO_HEX,
    MAX(flight) AS flight_number,
    closest_gate_name AS gate_name,
    SUM(lag_seconds) AS dwell_seconds
  FROM {database}.{schema}.GATE_ANALYSIS_ADSB_GROUND_POINTS
  WHERE closest_gate_name IS NOT NULL
  GROUP BY 1, 2, 3, 4, closest_gate_name
)
SELECT
  -- Backward-compat: expose a non-null flight_key by using the ground session id.
  ground_session_id AS flight_key,
  ground_session_id,
  aircraft_day_id,
  service_date,
  ICAO_HEX,
  flight_number,
  gate_name,
  dwell_seconds
FROM per_gate
QUALIFY ROW_NUMBER() OVER (PARTITION BY ground_session_id ORDER BY dwell_seconds DESC) = 1;

-- -----------------------------------------------------------------------------
-- 2b. GATE_UTIL_DAILY (used by Gate Analysis)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_GATE_UTIL_DAILY
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
SELECT
  service_date AS date,
  closest_gate_name AS gate_name,
  SUM(lag_seconds)/60.0 AS dwell_minutes,
  COUNT(DISTINCT ground_session_id) AS flights
FROM {database}.{schema}.GATE_ANALYSIS_ADSB_GROUND_POINTS
WHERE closest_gate_name IS NOT NULL
GROUP BY date, gate_name;

-- -----------------------------------------------------------------------------
-- 2c. GATE_AIRLINE_DWELL_DAILY (used by Gate Analysis)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_GATE_AIRLINE_DWELL_DAILY
  TARGET_LAG = '60 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH dim_icao AS (
  SELECT
    TRIM(UPPER(AIRLINE_ICAO)) AS airline_icao,
    MAX(NULLIF(TRIM(AIRLINE_IATA), '')) AS airline_iata,
    MAX(NULLIF(TRIM(AIRLINE_NAME), '')) AS airline_name
  FROM {database}.{schema}.HELPER_AIRLINE_DIM
  WHERE AIRLINE_ICAO IS NOT NULL AND TRIM(AIRLINE_ICAO) <> ''
  GROUP BY 1
),
dim_iata AS (
  SELECT
    TRIM(UPPER(AIRLINE_IATA)) AS airline_iata,
    MAX(NULLIF(TRIM(AIRLINE_ICAO), '')) AS airline_icao,
    MAX(NULLIF(TRIM(AIRLINE_NAME), '')) AS airline_name
  FROM {database}.{schema}.HELPER_AIRLINE_DIM
  WHERE AIRLINE_IATA IS NOT NULL AND TRIM(AIRLINE_IATA) <> ''
  GROUP BY 1
),
by_session AS (
  SELECT
    g.service_date AS date,
    g.ground_session_id,
    g.ICAO_HEX,
    g.closest_gate_name AS gate_name,
    SUM(g.lag_seconds)/60.0 AS dwell_minutes,
    -- Pull airline metadata from schedule-enriched ADSB points (more reliable than schedule.registration)
    COALESCE(
      MAX(NULLIF(TRIM(a.AIRLINE_ICAO), '')),
      MAX(di.airline_icao)
    ) AS airline_icao,
    COALESCE(
      MAX(NULLIF(TRIM(a.AIRLINE_IATA), '')),
      MAX(dj.airline_iata)
    ) AS airline_iata,
    COALESCE(
      MAX(NULLIF(TRIM(a.AIRLINE_NAME), '')),
      MAX(di.airline_name),
      MAX(dj.airline_name)
    ) AS airline_name
  FROM {database}.{schema}.GATE_ANALYSIS_ADSB_GROUND_POINTS g
  LEFT JOIN {database}.{schema}.ADSB_DATA_LOCAL a
    ON a.ICAO_HEX = g.ICAO_HEX
   AND a.TIMESTAMP = g.ts
  -- Fallback: derive airline from callsign prefix when ADSB enrichment is missing
  LEFT JOIN dim_icao di
    ON di.airline_icao = REGEXP_SUBSTR(UPPER(TRIM(g.flight)), '^[A-Z]{{3}}')
  LEFT JOIN dim_iata dj
    ON dj.airline_iata = REGEXP_SUBSTR(UPPER(TRIM(g.flight)), '^[A-Z]{{2}}')
  WHERE g.closest_gate_name IS NOT NULL
  GROUP BY
    g.service_date,
    g.ground_session_id,
    g.ICAO_HEX,
    g.closest_gate_name
)
SELECT
  s.date,
  s.gate_name,
  COALESCE(s.airline_icao, s.airline_iata, 'UNK') AS airline_code,
  MAX(s.airline_name) AS airline_name,
  SUM(s.dwell_minutes) AS dwell_minutes,
  COUNT(DISTINCT s.ground_session_id) AS flights
FROM by_session s
GROUP BY 1,2,3;

-- -----------------------------------------------------------------------------
-- 3. Flight Traffic derived tables (used by Traffic Analysis)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_ADSB_DAILY
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
SELECT
  TIMESTAMP::DATE AS date,
  COUNT(DISTINCT ICAO_HEX) AS unique_aircraft,
  COUNT(DISTINCT FLIGHT) AS unique_flights,
  COUNT(*) AS total_records,
  AVG(ALTITUDE_BARO) AS avg_altitude,
  AVG(VELOCITY) AS avg_speed
FROM {database}.{schema}.ADSB_DATA_LOCAL
GROUP BY date;

CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_ADSB_HOURLY
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
SELECT
  DATE_TRUNC('HOUR', TIMESTAMP) AS hour,
  COUNT(DISTINCT ICAO_HEX) AS aircraft_count,
  COUNT(*) AS data_points
FROM {database}.{schema}.ADSB_DATA_LOCAL
GROUP BY hour;

-- -----------------------------------------------------------------------------
-- 3b. Flight Tracker dropdown helper (all days)
-- Precompute per-flight-per-day header fields for fast UI: Airline + Origin/Destination
-- -----------------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.FLIGHT_TRACKER_FLIGHT_LIST
  TARGET_LAG = '15 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH airport AS (
  SELECT
    UPPER(airport_code) AS airport_code,
    geometry AS airport_geom
  FROM {database}.{schema}.PROPERTIES_AIRPORT
  LIMIT 1
),
base AS (
  SELECT
    TIMESTAMP::DATE AS service_date,
    COALESCE(NULLIF(TRIM(FLIGHT), ''), ICAO_HEX) AS flight_id,
    ICAO_HEX,
    TIMESTAMP AS ts,
    LOCATION AS location,
    ALTITUDE_BARO AS altitude_baro,
    VELOCITY AS velocity,
    NULLIF(TRIM(SCHEDULE_FLIGHT_NUMBER), '') AS schedule_flight_number,
    NULLIF(TRIM(AIRLINE_NAME), '') AS airline_name,
    NULLIF(TRIM(ORIGIN_AIRPORT), '') AS origin_airport,
    NULLIF(TRIM(DESTINATION_AIRPORT), '') AS destination_airport,
    COALESCE(IS_LOCAL_OD, FALSE) AS is_local_od,
    COALESCE(MATCH_CONFIDENCE, -1) AS match_confidence
  FROM {database}.{schema}.ADSB_DATA_LOCAL
  WHERE ICAO_HEX IS NOT NULL
    AND TIMESTAMP IS NOT NULL
),
agg AS (
  SELECT
    service_date,
    flight_id,
    COUNT(*) AS points,
    MIN(ts) AS first_seen_ts,
    MAX(ts) AS last_seen_ts,
    MAX(IFF(is_local_od, 1, 0)) AS is_local_od_any,
    MAX(
      IFF(
        airport.airport_geom IS NOT NULL
        AND location IS NOT NULL
        AND altitude_baro IS NOT NULL AND altitude_baro <= 50
        AND COALESCE(velocity, 0) <= 40
        AND ST_DWITHIN(location, airport.airport_geom, 5000),
        1, 0
      )
    ) AS touched_airport_any
  FROM base
  CROSS JOIN airport
  GROUP BY 1, 2
),
best AS (
  SELECT
    service_date,
    flight_id,
    schedule_flight_number,
    airline_name,
    origin_airport,
    destination_airport,
    is_local_od,
    match_confidence
  FROM base
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY service_date, flight_id
    ORDER BY
      IFF(is_local_od, 1, 0) DESC,
      match_confidence DESC,
      IFF(airline_name IS NULL, 0, 1) DESC,
      IFF(origin_airport IS NULL OR destination_airport IS NULL, 0, 1) DESC,
      ts DESC
  ) = 1
)
SELECT
  a.service_date,
  a.flight_id,
  a.points,
  a.first_seen_ts,
  a.last_seen_ts,
  b.schedule_flight_number,
  b.airline_name,
  b.origin_airport,
  b.destination_airport,
  b.match_confidence,
  IFF(a.is_local_od_any = 1, TRUE, FALSE) AS is_local_od,
  IFF(a.touched_airport_any = 1, TRUE, FALSE) AS touched_airport
FROM agg a
LEFT JOIN best b
  ON b.service_date = a.service_date
 AND b.flight_id = a.flight_id
WHERE a.is_local_od_any = 1 OR a.touched_airport_any = 1;

CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_AIRLINE_TRAFFIC_DAILY
  TARGET_LAG = '60 MINUTE'
  WAREHOUSE = {warehouse}
AS
SELECT
  TIMESTAMP::DATE AS date,
  SUBSTR(FLIGHT, 1, 3) AS airline_code,
  COUNT(DISTINCT ICAO_HEX) AS aircraft_count,
  COUNT(DISTINCT FLIGHT) AS flight_count,
  COUNT(*) AS data_points
FROM {database}.{schema}.ADSB_DATA_LOCAL
WHERE FLIGHT IS NOT NULL
GROUP BY date, airline_code;

-- Schedule-vs-actual delay rollup (used by Traffic Analysis)
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_AIRLINE_DELAY_DAILY
  TARGET_LAG = '60 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH schedule AS (
  SELECT
    FLIGHT_DATE AS travel_date,
    AIRLINE_NAME AS airline,
    FLIGHT_NUMBER AS flight_number,
    DEPARTURE_SCHEDULED AS scheduled_time
  FROM {database}.{schema}.FLIGHT_SCHEDULE
  WHERE FLIGHT_DATE >= DATEADD('day', -30, CURRENT_DATE())
    AND DEPARTURE_SCHEDULED IS NOT NULL
),
actual AS (
  SELECT
    TIMESTAMP::DATE AS date,
    SUBSTR(FLIGHT, 1, 3) AS airline,
    REGEXP_SUBSTR(FLIGHT, '[0-9]+') AS flight_number,
    MIN(TIMESTAMP) AS first_seen
  FROM {database}.{schema}.ADSB_DATA_LOCAL
  WHERE TIMESTAMP >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    AND FLIGHT IS NOT NULL
  GROUP BY 1, 2, 3
),
joined AS (
  SELECT
    a.date,
    COALESCE(s.airline, a.airline) AS airline,
    TIMESTAMPDIFF('minute', s.scheduled_time, a.first_seen) AS delay_minutes
  FROM actual a
  LEFT JOIN schedule s
    ON s.travel_date = a.date
   AND TO_VARCHAR(s.flight_number) = TO_VARCHAR(a.flight_number)
)
SELECT
  date,
  airline,
  SUM(IFF(delay_minutes > 15, delay_minutes, 0)) AS total_delay_minutes,
  SUM(IFF(delay_minutes > 15, 1, 0)) AS delayed_flights,
  SUM(IFF(delay_minutes < -15, ABS(delay_minutes), 0)) AS total_early_minutes,
  SUM(IFF(delay_minutes < -15, 1, 0)) AS early_flights
FROM joined
WHERE delay_minutes IS NOT NULL
GROUP BY date, airline;

-- -----------------------------------------------------------------------------
-- 4. Runway Crossings derived tables
-- -----------------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE {database}.{schema}.RUNWAY_CROSSINGS_DETAILED
  TARGET_LAG = '60 MINUTE'
  WAREHOUSE = {warehouse}
AS
WITH runway_union AS (
  SELECT
    runway_id,
    runway_geog AS rw
  FROM {database}.{schema}.PROPERTIES_RUNWAYS
  WHERE runway_geog IS NOT NULL
),
pts AS (
  SELECT flight_key, TIMESTAMP AS ts, LOCATION AS geom, VELOCITY, ALTITUDE_BARO
  FROM {database}.{schema}.ADSB_DATA_LOCAL
  -- Limit scope for incremental refresh cost; adjust if you backfill more history.
  WHERE TIMESTAMP >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    AND LOCATION IS NOT NULL AND ALTITUDE_BARO IS NOT NULL
),
pts_tagged AS (
  SELECT
    p.*,
    r.runway_id,
    IFF(ST_INTERSECTS(p.geom, r.rw), TRUE, FALSE) AS inside_runway,
    LAG(IFF(ST_INTERSECTS(p.geom, r.rw), TRUE, FALSE))
      OVER (PARTITION BY p.flight_key, r.runway_id ORDER BY p.ts) AS prev_inside,
    LEAD(IFF(ST_INTERSECTS(p.geom, r.rw), TRUE, FALSE))
      OVER (PARTITION BY p.flight_key, r.runway_id ORDER BY p.ts) AS next_inside
  FROM pts p CROSS JOIN runway_union r
),
grouped AS (
  SELECT *, SUM(IFF(inside_runway AND NVL(prev_inside, FALSE) = FALSE, 1, 0))
            OVER (PARTITION BY flight_key, runway_id ORDER BY ts ROWS UNBOUNDED PRECEDING) AS grp_raw
  FROM pts_tagged
),
inside_labeled AS (
  SELECT *, IFF(inside_runway, grp_raw, NULL) AS inside_grp,
    ROW_NUMBER() OVER (PARTITION BY flight_key, runway_id, IFF(inside_runway, grp_raw, NULL) ORDER BY ts) AS rn_asc,
    ROW_NUMBER() OVER (PARTITION BY flight_key, runway_id, IFF(inside_runway, grp_raw, NULL) ORDER BY ts DESC) AS rn_desc
  FROM grouped WHERE inside_runway
),
entry_rows AS (
  SELECT runway_id, flight_key, inside_grp AS event_id, ts AS t_entry, geom AS entry_geom
  FROM inside_labeled
  WHERE rn_asc = 1 AND COALESCE(prev_inside, FALSE) = FALSE
),
exit_rows AS (
  SELECT runway_id, flight_key, inside_grp AS event_id, ts AS t_exit, geom AS exit_geom
  FROM inside_labeled
  WHERE rn_desc = 1 AND COALESCE(next_inside, FALSE) = FALSE
),
stats AS (
  SELECT runway_id, flight_key, inside_grp AS event_id,
    MAX(COALESCE(VELOCITY, 0)) AS max_speed_kts,
    COUNT(*) AS inside_points
  FROM inside_labeled
  GROUP BY 1, 2, 3
),
events AS (
  SELECT e.runway_id, e.flight_key, e.event_id, e.t_entry, x.t_exit, e.entry_geom, x.exit_geom,
    ST_DISTANCE(e.entry_geom, x.exit_geom) AS chord_m,
    DATEDIFF('second', e.t_entry, x.t_exit) AS duration_s,
    s.max_speed_kts, s.inside_points,
    CASE WHEN (ST_Y(x.exit_geom) - ST_Y(e.entry_geom)) > 0.00005 THEN 'S→N'
         WHEN (ST_Y(x.exit_geom) - ST_Y(e.entry_geom)) < -0.00005 THEN 'N→S'
         ELSE 'uncertain' END AS direction,
    ST_CENTROID(ST_MAKELINE(e.entry_geom, x.exit_geom)) AS midpoint_geom
  FROM entry_rows e
  JOIN exit_rows x USING (runway_id, flight_key, event_id)
  JOIN stats s USING (runway_id, flight_key, event_id)
)
SELECT * FROM events
-- Loosen thresholds slightly: runway polygons and ADS-B ground speeds can be noisy.
WHERE max_speed_kts <= 80 AND duration_s <= 300 AND chord_m <= 500 AND direction <> 'uncertain';

-- -----------------------------------------------------------------------------
-- 3b. Monitoring tables (used by Monitoring page)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_MONITOR_LAST_REFRESH (
  table_name STRING,
  last_refreshed_at TIMESTAMP_NTZ,
  row_count_24h NUMBER(38,0),
  max_ts TIMESTAMP_NTZ,
  status STRING,
  details STRING
);

CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_QA_COUNTS_DAILY (
  metric_date DATE,
  metric_name STRING,
  metric_value NUMBER(38,0)
);

-- Ensure columns exist if table was created by an older installer version
ALTER TABLE {database}.{schema}.HELPER_MONITOR_LAST_REFRESH ADD COLUMN IF NOT EXISTS row_count_24h NUMBER(38,0);
ALTER TABLE {database}.{schema}.HELPER_MONITOR_LAST_REFRESH ADD COLUMN IF NOT EXISTS max_ts TIMESTAMP_NTZ;
ALTER TABLE {database}.{schema}.HELPER_MONITOR_LAST_REFRESH ADD COLUMN IF NOT EXISTS status STRING;
ALTER TABLE {database}.{schema}.HELPER_MONITOR_LAST_REFRESH ADD COLUMN IF NOT EXISTS details STRING;

-- Store installer config: adsb_history_backfill_days (used by backfill retry enrichment)
MERGE INTO {database}.{schema}.HELPER_MONITOR_LAST_REFRESH t
USING (SELECT 'CONFIG_ADSB_BACKFILL_DAYS' AS table_name, {adsb_history_backfill_days} AS row_count_24h) s
ON t.table_name = s.table_name
WHEN MATCHED THEN UPDATE SET row_count_24h = s.row_count_24h, last_refreshed_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (table_name, row_count_24h, last_refreshed_at) 
                      VALUES (s.table_name, s.row_count_24h, CURRENT_TIMESTAMP());

CREATE TABLE IF NOT EXISTS {database}.{schema}.HELPER_INGEST_AUDIT (
  run_id STRING,
  airport_code STRING,
  window_start TIMESTAMP_NTZ,
  window_end TIMESTAMP_NTZ,
  rows_raw NUMBER(38,0),
  rows_inserted NUMBER(38,0),
  rows_deduped NUMBER(38,0),
  status STRING,
  error_message STRING,
  created_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- -----------------------------------------------------------------------------
-- 3c. Ops/performance placeholders (avoid dashboard hard errors; can be replaced later)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {database}.{schema}.H2H_CONFLICT_PAIRS (
  event_a_id STRING,
  event_b_id STRING,
  flight_a STRING,
  flight_b STRING,
  aircraft_a STRING,
  aircraft_b STRING,
  op_a STRING,
  op_b STRING,
  runway_mode STRING,
  a_start TIMESTAMP_NTZ,
  a_end TIMESTAMP_NTZ,
  b_start TIMESTAMP_NTZ,
  b_end TIMESTAMP_NTZ,
  min_gap_seconds NUMBER(38,0)
);

CREATE OR REPLACE VIEW {database}.{schema}.V_AIR_OPS_TIMELINE AS
SELECT CAST(NULL AS DATE) AS service_date, CAST(NULL AS STRING) AS airline_name
WHERE 1=0;

CREATE OR REPLACE VIEW {database}.{schema}.V_AIR_OPS_DAILY_KPIS AS
SELECT
  CAST(NULL AS DATE) AS service_date,
  CAST(NULL AS STRING) AS airline_name,
  CAST(NULL AS NUMBER(38,0)) AS ops,
  CAST(NULL AS FLOAT) AS med_taxi_out_min,
  CAST(NULL AS FLOAT) AS med_taxi_in_min,
  CAST(NULL AS FLOAT) AS med_dep_runway_occ_min,
  CAST(NULL AS FLOAT) AS med_arr_runway_occ_min,
  CAST(NULL AS FLOAT) AS on_time_dep_out_15m_rate,
  CAST(NULL AS FLOAT) AS on_time_arr_in_15m_rate,
  CAST(NULL AS BOOLEAN) AS head_to_head
WHERE 1=0;

-- -----------------------------------------------------------------------------
-- 4. Refresh procedure
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_REFRESH_DERIVED()
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
  v_adsb_cnt_24h NUMBER(38,0);
  v_adsb_max_ts TIMESTAMP_NTZ;
  v_sched_cnt_window NUMBER(38,0);
BEGIN
  -- Core table freshness (avoid full-table scans by limiting to recent window where possible)
  SELECT COUNT(*), MAX(TIMESTAMP)
    INTO :v_adsb_cnt_24h, :v_adsb_max_ts
  FROM {database}.{schema}.ADSB_DATA_LOCAL
  WHERE TIMESTAMP >= DATEADD('day', -1, CURRENT_TIMESTAMP());

  SELECT COUNT(*)
    INTO :v_sched_cnt_window
  FROM {database}.{schema}.FLIGHT_SCHEDULE
  WHERE FLIGHT_DATE BETWEEN DATEADD('day', -2, CURRENT_DATE()) AND DATEADD('day', 2, CURRENT_DATE());

  MERGE INTO {database}.{schema}.HELPER_MONITOR_LAST_REFRESH t
  USING (
    SELECT 'ADSB_DATA_LOCAL' AS table_name,
           CURRENT_TIMESTAMP() AS ts,
           :v_adsb_cnt_24h AS row_count_24h,
           :v_adsb_max_ts AS max_ts,
           IFF(:v_adsb_max_ts IS NOT NULL AND :v_adsb_max_ts >= DATEADD('hour', -2, CURRENT_TIMESTAMP()), 'OK', 'STALE') AS status,
           IFF(:v_adsb_max_ts IS NULL, 'No relevant ADS-B data yet', NULL) AS details
    UNION ALL
    SELECT 'FLIGHT_SCHEDULE', CURRENT_TIMESTAMP(), :v_sched_cnt_window, NULL,
           IFF(:v_sched_cnt_window > 0, 'OK', 'EMPTY'),
           IFF(:v_sched_cnt_window = 0, 'No schedule rows in current +/-2 day window', NULL)
  ) s
  ON t.table_name = s.table_name
  WHEN MATCHED THEN UPDATE SET
    last_refreshed_at = s.ts,
    row_count_24h = s.row_count_24h,
    max_ts = s.max_ts,
    status = s.status,
    details = s.details
  WHEN NOT MATCHED THEN INSERT (table_name, last_refreshed_at, row_count_24h, max_ts, status, details)
    VALUES (s.table_name, s.ts, s.row_count_24h, s.max_ts, s.status, s.details);

  -- QA completeness metrics for last 24h (integer percent 0-100)
  MERGE INTO {database}.{schema}.HELPER_QA_COUNTS_DAILY t
  USING (
    WITH base AS (
      SELECT
        COUNT(*) AS cnt,
        COUNT_IF(FLIGHT IS NOT NULL AND TRIM(FLIGHT) <> '') AS nn_flight,
        COUNT_IF(TRACK IS NOT NULL) AS nn_track,
        COUNT_IF(TRUE_HEADING IS NOT NULL) AS nn_true_heading,
        COUNT_IF(SQUAWK IS NOT NULL AND TRIM(SQUAWK) <> '') AS nn_squawk,
        COUNT_IF(CATEGORY IS NOT NULL AND TRIM(CATEGORY) <> '') AS nn_category,
        COUNT_IF(AIRCRAFT_DESC IS NOT NULL AND TRIM(AIRCRAFT_DESC) <> '') AS nn_aircraft_desc,
        COUNT_IF(ALTITUDE_GEOM IS NOT NULL) AS nn_alt_geom,
        COUNT_IF(VERTICAL_RATE IS NOT NULL) AS nn_vertical_rate,
        COUNT_IF(SCHEDULE_FLIGHT_KEY IS NOT NULL) AS nn_matched,
        COUNT(DISTINCT ICAO_HEX) AS unique_aircraft
      FROM {database}.{schema}.ADSB_DATA_LOCAL
      WHERE TIMESTAMP >= DATEADD('day', -1, CURRENT_TIMESTAMP())
    ),
    legs AS (
      SELECT COUNT(*) AS leg_cnt
      FROM {database}.{schema}.HELPER_FLIGHT_LEG
      WHERE SERVICE_DATE >= DATEADD('day', -1, CURRENT_DATE())
    ),
    leg_matches AS (
      SELECT COUNT(*) AS matched_leg_cnt
      FROM {database}.{schema}.HELPER_FLIGHT_MATCH_RESULT
      WHERE SERVICE_DATE >= DATEADD('day', -1, CURRENT_DATE())
    )
    SELECT CURRENT_DATE() AS metric_date, 'adsb_points_24h' AS metric_name, cnt::NUMBER(38,0) AS metric_value FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_flight_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_flight/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_track_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_track/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_true_heading_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_true_heading/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_squawk_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_squawk/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_category_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_category/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_aircraft_desc_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_aircraft_desc/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_alt_geom_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_alt_geom/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'adsb_vertical_rate_nonnull_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_vertical_rate/cnt))::NUMBER(38,0) FROM base
    -- Flight matching health metrics
    UNION ALL SELECT CURRENT_DATE(), 'match_rate_pct_24h', IFF(cnt=0, NULL, ROUND(100*nn_matched/cnt))::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'unique_aircraft_24h', unique_aircraft::NUMBER(38,0) FROM base
    UNION ALL SELECT CURRENT_DATE(), 'flight_legs_24h', leg_cnt::NUMBER(38,0) FROM legs
    UNION ALL SELECT CURRENT_DATE(), 'matched_legs_24h', matched_leg_cnt::NUMBER(38,0) FROM leg_matches
    UNION ALL SELECT CURRENT_DATE(), 'leg_match_rate_pct_24h', IFF((SELECT leg_cnt FROM legs)=0, NULL, ROUND(100*(SELECT matched_leg_cnt FROM leg_matches)/(SELECT leg_cnt FROM legs)))::NUMBER(38,0)
  ) s
  ON t.metric_date = s.metric_date AND t.metric_name = s.metric_name
  WHEN MATCHED THEN UPDATE SET metric_value = s.metric_value
  WHEN NOT MATCHED THEN INSERT (metric_date, metric_name, metric_value) VALUES (s.metric_date, s.metric_name, s.metric_value);

  RETURN 'Monitoring + QA updated';
END;
$$;

-- -----------------------------------------------------------------------------
-- Smoke check (fail the installer loudly if core invariants aren't met)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_SMOKE_CHECK(p_grace_minutes STRING)
RETURNS STRING
LANGUAGE JAVASCRIPT
EXECUTE AS CALLER
AS
$$
function scalar(sqlText) {{
  var stmt = snowflake.createStatement({{sqlText}});
  var rs = stmt.execute();
  rs.next();
  return rs.getColumnValue(1);
}}

// Snowflake JS sprocs reliably expose parameters via `arguments[]`
var p_grace_minutes = arguments[0];

function minutesSinceInstall() {{
  try {{
    var mins = scalar(`SELECT DATEDIFF('minute', MAX(installed_at), CURRENT_TIMESTAMP()) FROM {database}.{schema}.HELPER_INSTALL_AUDIT`);
    if (mins === null) return 999999;
    return mins;
  }} catch (e) {{
    return 999999;
  }}
}}

var grace = 10;
if (p_grace_minutes !== null) {{
  var parsed = parseInt(p_grace_minutes, 10);
  if (!isNaN(parsed)) grace = parsed;
}}
var minsSince = minutesSinceInstall();

// Runways: at least 1 row and non-null geometry (may be split into multiple polygons)
var runwayCnt = scalar(`SELECT COUNT(*) FROM {database}.{schema}.PROPERTIES_RUNWAYS`);
if (runwayCnt < 1) {{
  throw `Smoke check failed: PROPERTIES_RUNWAYS must have at least 1 row, got ${{runwayCnt}}`;
}}
var runwayNonNull = scalar(`SELECT COUNT_IF(runway_geog IS NOT NULL) FROM {database}.{schema}.PROPERTIES_RUNWAYS`);
if (runwayNonNull < 1) {{
  throw `Smoke check failed: PROPERTIES_RUNWAYS.runway_geog is NULL`;
}}

// ADS-B freshness: expect points within last 2 hours once tasks are running
var maxTs = scalar(`SELECT MAX(TIMESTAMP) FROM {database}.{schema}.ADSB_DATA_LOCAL`);
if (maxTs === null) {{
  if (minsSince <= grace) {{
    return `WAITING_FOR_ADSB_DATA (installed ${{minsSince}} min ago; grace=${{grace}}m)`;
  }}
  throw `Smoke check failed: ADSB_DATA_LOCAL is empty (MAX(TIMESTAMP) is NULL)`;
}}
var fresh = scalar(`SELECT IFF(MAX(TIMESTAMP) >= DATEADD('hour', -2, CURRENT_TIMESTAMP()), 1, 0) FROM {database}.{schema}.ADSB_DATA_LOCAL`);
if (fresh !== 1) {{
  if (minsSince <= grace) {{
    return `WAITING_FOR_ADSB_DATA (stale during grace window; installed ${{minsSince}} min ago; grace=${{grace}}m; max_ts=${{maxTs}})`;
  }}
  throw `Smoke check failed: ADSB_DATA_LOCAL appears stale (no points in last 2 hours). max_ts=${{maxTs}}`;
}}

// Flight schedule: check if data exists (optional, may be empty if no API key provided)
var schedCnt = scalar(`SELECT COUNT(*) FROM {database}.{schema}.FLIGHT_SCHEDULE WHERE FLIGHT_DATE BETWEEN DATEADD('day', -2, CURRENT_DATE()) AND DATEADD('day', 2, CURRENT_DATE())`);
// Note: schedule may be empty if API key was not provided during install

// Tasks should be STARTED (check for flight schedule task separately)
snowflake.createStatement({{sqlText: `SHOW TASKS IN SCHEMA {database}.{schema}`}}).execute();
var schedTaskExists = scalar(`SELECT COUNT_IF(\"name\"='TASK_FLIGHT_SCHEDULE_HOURLY') FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))`);
var requiredTasksRunning = scalar(`SELECT COUNT_IF(LOWER(\"state\")='started' AND \"name\" IN ('TASK_INGEST_ADSB','TASK_ENRICH_ADSB_HOURLY','TASK_REFRESH_DERIVED_15MIN')) FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))`);
var schedTaskRunning = scalar(`SELECT COUNT_IF(LOWER(\"state\")='started' AND \"name\"='TASK_FLIGHT_SCHEDULE_HOURLY') FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))`);

// Core tasks (ADS-B ingestion, enrichment, derived refresh) must be running
if (requiredTasksRunning < 3) {{
  throw `Smoke check failed: not all core ADS-B tasks are STARTED (started=${{requiredTasksRunning}}/3)`;
}}

// Flight schedule task is optional (only exists if API key was provided)
if (schedTaskExists > 0 && schedTaskRunning === 0) {{
  throw `Smoke check failed: TASK_FLIGHT_SCHEDULE_HOURLY exists but is not STARTED`; 
}}

return 'OK';
$$;

CREATE OR REPLACE TASK {database}.{schema}.TASK_REFRESH_DERIVED_15MIN
  WAREHOUSE = {warehouse}
  SCHEDULE = '15 MINUTE'
  ALLOW_OVERLAPPING_EXECUTION = FALSE
AS
  CALL {database}.{schema}.PROC_REFRESH_DERIVED();

-- Verify derived tables
SELECT 'GATE_ANALYSIS_ADSB_GROUND_POINTS' AS tbl, COUNT(*) AS cnt FROM {database}.{schema}.GATE_ANALYSIS_ADSB_GROUND_POINTS
UNION ALL SELECT 'GATE_ANALYSIS_AIRCRAFT_GROUND_SESSIONS', COUNT(*) FROM {database}.{schema}.GATE_ANALYSIS_AIRCRAFT_GROUND_SESSIONS
UNION ALL SELECT 'GATE_ANALYSIS_FLIGHT_GATE_TIME', COUNT(*) FROM {database}.{schema}.GATE_ANALYSIS_FLIGHT_GATE_TIME
UNION ALL SELECT 'GATE_ANALYSIS_GATE_UTIL_DAILY', COUNT(*) FROM {database}.{schema}.GATE_ANALYSIS_GATE_UTIL_DAILY
UNION ALL SELECT 'GATE_ANALYSIS_GATE_AIRLINE_DWELL_DAILY', COUNT(*) FROM {database}.{schema}.GATE_ANALYSIS_GATE_AIRLINE_DWELL_DAILY
UNION ALL SELECT 'ADSB_DATA_LOCAL', COUNT(*) FROM {database}.{schema}.ADSB_DATA_LOCAL
UNION ALL SELECT 'HELPER_FLIGHT_LEG', COUNT(*) FROM {database}.{schema}.HELPER_FLIGHT_LEG
UNION ALL SELECT 'HELPER_FLIGHT_MATCH_CANDIDATES', COUNT(*) FROM {database}.{schema}.HELPER_FLIGHT_MATCH_CANDIDATES
UNION ALL SELECT 'HELPER_FLIGHT_MATCH_RESULT', COUNT(*) FROM {database}.{schema}.HELPER_FLIGHT_MATCH_RESULT
UNION ALL SELECT 'HELPER_RECURRING_CALLSIGN_PRIOR', COUNT(*) FROM {database}.{schema}.HELPER_RECURRING_CALLSIGN_PRIOR
UNION ALL SELECT 'FLIGHT_TRAFFIC_FACT_ADSB_DAILY', COUNT(*) FROM {database}.{schema}.FLIGHT_TRAFFIC_FACT_ADSB_DAILY
UNION ALL SELECT 'FLIGHT_TRAFFIC_FACT_ADSB_HOURLY', COUNT(*) FROM {database}.{schema}.FLIGHT_TRAFFIC_FACT_ADSB_HOURLY
UNION ALL SELECT 'FLIGHT_TRACKER_FLIGHT_LIST', COUNT(*) FROM {database}.{schema}.FLIGHT_TRACKER_FLIGHT_LIST
UNION ALL SELECT 'FLIGHT_TRAFFIC_FACT_AIRLINE_TRAFFIC_DAILY', COUNT(*) FROM {database}.{schema}.FLIGHT_TRAFFIC_FACT_AIRLINE_TRAFFIC_DAILY
UNION ALL SELECT 'FLIGHT_TRAFFIC_FACT_AIRLINE_DELAY_DAILY', COUNT(*) FROM {database}.{schema}.FLIGHT_TRAFFIC_FACT_AIRLINE_DELAY_DAILY
UNION ALL SELECT 'RUNWAY_CROSSINGS_DETAILED', COUNT(*) FROM {database}.{schema}.RUNWAY_CROSSINGS_DETAILED
UNION ALL SELECT 'FLIGHT_SCHEDULE', COUNT(*) FROM {database}.{schema}.FLIGHT_SCHEDULE
UNION ALL SELECT 'HELPER_FLIGHT_SCHEDULE_RAW', COUNT(*) FROM {database}.{schema}.HELPER_FLIGHT_SCHEDULE_RAW;

-- =============================================================================
-- START AUTOMATED TASKS
-- =============================================================================

-- Start the ADS-B ingestion task (real-time data every minute)
ALTER TASK {database}.{schema}.TASK_INGEST_ADSB RESUME;

-- Start ADS-B enrichment task (adds schedule flight number/key to points)
ALTER TASK {database}.{schema}.TASK_ENRICH_ADSB_HOURLY RESUME;

-- Note: TASK_FLIGHT_SCHEDULE_HOURLY is resumed in 04_flight_schedule_v5.sql
-- (only exists if API key was provided during install)

-- Start derived refresh (15 min)
ALTER TASK {database}.{schema}.TASK_REFRESH_DERIVED_15MIN RESUME;

-- Start aircraft meta enrichment (hourly) - keeps ADSB_DATA.AIRCRAFT_DESC populated
ALTER TASK {database}.{schema}.TASK_ENRICH_AIRCRAFT_META_HOURLY RESUME;

-- Resume dynamic tables (incremental refresh)
ALTER DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_AIRCRAFT_GROUND_SESSIONS RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_ADSB_GROUND_POINTS RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_FLIGHT_GATE_TIME RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_GATE_UTIL_DAILY RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.GATE_ANALYSIS_GATE_AIRLINE_DWELL_DAILY RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.ADSB_DATA_LOCAL RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_ADSB_DAILY RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_ADSB_HOURLY RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.FLIGHT_TRACKER_FLIGHT_LIST RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_AIRLINE_TRAFFIC_DAILY RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.FLIGHT_TRAFFIC_FACT_AIRLINE_DELAY_DAILY RESUME;
ALTER DYNAMIC TABLE {database}.{schema}.RUNWAY_CROSSINGS_DETAILED RESUME;

-- Run one enrichment pass immediately (populates schedule association fields on ADSB_DATA)
CALL {database}.{schema}.PROC_ENRICH_ADSB_WITH_SCHEDULE(2);

-- Heartbeat
CALL {database}.{schema}.PROC_REFRESH_DERIVED();

-- Fail fast if something is clearly wrong
CALL {database}.{schema}.PROC_SMOKE_CHECK('10');

-- Final verification
SELECT 'Setup complete! Tasks are now running automatically.' AS status;
"""


def generate_flight_schedule_sql(
    airport: dict,
    database: str,
    schema: str,
    warehouse: str,
    api_key: str,
    backfill_days: int = 7,
) -> str:
    """Generate Flight Schedule ingestion SQL (aviationstack)."""
    # Pre-compute values for embedding in Python procedure code
    schedule_raw_table = f"{database}.{schema}.HELPER_FLIGHT_SCHEDULE_RAW"
    # External Access Integrations are ACCOUNT-level objects; must be per-airport to avoid collisions.
    eai_aviationstack = re.sub(r"[^A-Za-z0-9_]", "_", f"{database}_{schema}_AVIATIONSTACK_EAI").upper()
    backfill_days = int(backfill_days or 0)
    # Keep at least the historical window we previously used (2 days) unless explicitly larger.
    backfill_days = max(2, backfill_days)
    
    return f"""-- =============================================================================
-- FLIGHT SCHEDULE INGESTION FOR {airport['name']} ({airport['iata_code']})
-- Database: {database}.{schema}
-- Source: aviationstack API
-- =============================================================================

-- -----------------------------------------------------------------------------
-- External Network Access (for aviationstack API)
-- Note: Basic plan uses HTTP (port 80), not HTTPS
-- -----------------------------------------------------------------------------
CREATE OR REPLACE NETWORK RULE {database}.{schema}.{schema}_aviationstack_rule
  TYPE = HOST_PORT
  MODE = EGRESS
  VALUE_LIST = ('api.aviationstack.com:80');

-- Create secret for API key
CREATE OR REPLACE SECRET {database}.{schema}.aviationstack_key
  TYPE = GENERIC_STRING
  SECRET_STRING = '{api_key}';

-- Create external access integration
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION {eai_aviationstack}
  ALLOWED_NETWORK_RULES = ({database}.{schema}.{schema}_aviationstack_rule)
  ALLOWED_AUTHENTICATION_SECRETS = ({database}.{schema}.aviationstack_key)
  ENABLED = TRUE;

-- -----------------------------------------------------------------------------
-- Note: HELPER_FLIGHT_SCHEDULE_RAW and FLIGHT_SCHEDULE tables are created
-- in 01_base_v5.sql to ensure they exist even if API key is not provided.
-- This allows the installer to complete successfully without an API key.
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- Ingestion Procedure
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_INGEST_FLIGHT_SCHEDULE(p_airport VARCHAR, p_date VARCHAR)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'requests')
HANDLER = 'ingest'
EXTERNAL_ACCESS_INTEGRATIONS = ({eai_aviationstack})
SECRETS = ('api_key' = {database}.{schema}.aviationstack_key)
AS
$$
import requests
import _snowflake
from datetime import datetime

def parse_ts(ts_str):
    if not ts_str: return None
    try: return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except: return None

def fetch_flights(api_key, airport, date, direction):
    # Fetch flights with pagination support
    all_flights = []
    offset = 0
    limit = 100
    # Safety ceiling: prevents runaway loops if the API pagination metadata is wrong.
    # Should be comfortably above even very busy airports.
    max_flights = 10000
    while True:
        suffix = ("&dep_iata=" if direction == 'dep' else "&arr_iata=") + airport
        url = "http://api.aviationstack.com/v1/flights?access_key=" + api_key + suffix + "&flight_date=" + date + "&limit=" + str(limit) + "&offset=" + str(offset)
        try:
            resp = requests.get(url, timeout=60)
            data = resp.json()
        except: break
        if 'error' in data: break
        flights = data.get('data', [])
        if not flights: break
        all_flights.extend(flights)
        pagination = data.get('pagination', dict())
        total = pagination.get('total', None)
        # Stop when we've reached the API-reported total, or when the page is smaller than our limit.
        # (Some plans / responses may omit total.)
        if total is not None and offset + limit >= int(total): break
        if len(flights) < limit: break
        if len(all_flights) >= max_flights: break
        offset += limit
    return all_flights

def ingest(session, p_airport, p_date):
    api_key = _snowflake.get_generic_secret_string('api_key')
    departures = fetch_flights(api_key, p_airport, p_date, 'dep')
    arrivals = fetch_flights(api_key, p_airport, p_date, 'arr')
    
    seen = set()
    all_flights = []
    for f in departures + arrivals:
        flt = f.get('flight', dict())
        dep = f.get('departure', dict())
        key = (flt.get('iata'), dep.get('scheduled'))
        if key not in seen:
            seen.add(key)
            all_flights.append(f)
    
    if not all_flights:
        return "No flights found for " + p_airport + " on " + p_date
    
    now = datetime.utcnow()
    rows = []
    for f in all_flights:
        dep = f.get('departure', dict())
        arr = f.get('arrival', dict())
        airline = f.get('airline', dict())
        flt = f.get('flight', dict())
        aircraft = f.get('aircraft') or dict()
        cs = flt.get('codeshared') or dict()
        
        # Aviationstack does not always provide FLIGHT_ICAO/FLIGHT_IATA.
        # When missing, synthesize from airline code + flight number (helps match ADS-B callsigns like SKW3482).
        flight_number = flt.get('number')
        flight_iata = flt.get('iata')
        flight_icao = flt.get('icao')
        airline_iata = airline.get('iata')
        airline_icao = airline.get('icao')
        if (not flight_iata) and airline_iata and flight_number:
            flight_iata = str(airline_iata).strip().upper() + str(flight_number).strip()
        if (not flight_icao) and airline_icao and flight_number:
            flight_icao = str(airline_icao).strip().upper() + str(flight_number).strip()
        
        rows.append([
            datetime.strptime(p_date, '%Y-%m-%d').date(),
            f.get('flight_status'),
            dep.get('iata'),
            parse_ts(dep.get('scheduled')),
            parse_ts(dep.get('estimated')),
            parse_ts(dep.get('actual')),
            dep.get('delay'),
            dep.get('terminal'),
            dep.get('gate'),
            arr.get('iata'),
            parse_ts(arr.get('scheduled')),
            parse_ts(arr.get('estimated')),
            parse_ts(arr.get('actual')),
            arr.get('delay'),
            arr.get('terminal'),
            arr.get('gate'),
            airline.get('name'),
            airline_iata,
            airline_icao,
            flight_number,
            flight_iata,
            flight_icao,
            aircraft.get('registration'),
            aircraft.get('iata'),
            aircraft.get('icao'),
            cs.get('airline_name'),
            cs.get('flight_iata'),
            f, # Dict is automatically converted to VARIANT
            now
        ])
    
    if rows:
        from snowflake.snowpark.types import StructType, StructField, StringType, DateType, TimestampType, IntegerType, VariantType
        schema = StructType([
            StructField("FLIGHT_DATE", DateType()), StructField("FLIGHT_STATUS", StringType()),
            StructField("DEPARTURE_AIRPORT", StringType()), StructField("DEPARTURE_SCHEDULED", TimestampType()),
            StructField("DEPARTURE_ESTIMATED", TimestampType()), StructField("DEPARTURE_ACTUAL", TimestampType()),
            StructField("DEPARTURE_DELAY", IntegerType()), StructField("DEPARTURE_TERMINAL", StringType()),
            StructField("DEPARTURE_GATE", StringType()), StructField("ARRIVAL_AIRPORT", StringType()),
            StructField("ARRIVAL_SCHEDULED", TimestampType()), StructField("ARRIVAL_ESTIMATED", TimestampType()),
            StructField("ARRIVAL_ACTUAL", TimestampType()), StructField("ARRIVAL_DELAY", IntegerType()),
            StructField("ARRIVAL_TERMINAL", StringType()), StructField("ARRIVAL_GATE", StringType()),
            StructField("AIRLINE_NAME", StringType()), StructField("AIRLINE_IATA", StringType()),
            StructField("AIRLINE_ICAO", StringType()), StructField("FLIGHT_NUMBER", StringType()),
            StructField("FLIGHT_IATA", StringType()), StructField("FLIGHT_ICAO", StringType()),
            StructField("AIRCRAFT_REGISTRATION", StringType()), StructField("AIRCRAFT_IATA", StringType()),
            StructField("AIRCRAFT_ICAO", StringType()), StructField("CODESHARED_AIRLINE", StringType()),
            StructField("CODESHARED_FLIGHT_IATA", StringType()), StructField("RAW_JSON", VariantType()),
            StructField("INGESTED_AT", TimestampType())
        ])
        df = session.create_dataframe(rows, schema=schema)
        df.write.mode('append').save_as_table('{schedule_raw_table}')
    
    return "Inserted " + str(len(rows)) + " flights for " + p_airport + " on " + p_date
$$;

-- -----------------------------------------------------------------------------
-- ETL to canonical FLIGHT_SCHEDULE (single source of truth)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_ETL_SCHEDULE_TO_FLIGHT_SCHEDULE()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
    MERGE INTO {database}.{schema}.FLIGHT_SCHEDULE t
    USING (
        SELECT
            MD5(CONCAT(COALESCE(flight_iata, 'UNKNOWN'), ':', COALESCE(TO_VARCHAR(departure_scheduled), 'UNKNOWN'), ':', COALESCE(TO_VARCHAR(arrival_scheduled), 'UNKNOWN'))) AS FLIGHT_KEY,
            flight_date, flight_status, departure_airport, arrival_airport,
            departure_scheduled, departure_estimated, departure_actual,
            departure_delay, departure_terminal, departure_gate,
            arrival_scheduled, arrival_estimated, arrival_actual,
            arrival_delay, arrival_terminal, arrival_gate,
            airline_name, airline_iata, airline_icao,
            flight_number, flight_iata, flight_icao,
            aircraft_registration,
            IFF(codeshared_airline IS NOT NULL, TRUE, FALSE) AS IS_CODESHARE
        FROM {database}.{schema}.HELPER_FLIGHT_SCHEDULE_RAW
        QUALIFY ROW_NUMBER() OVER (PARTITION BY flight_iata, departure_scheduled, arrival_scheduled ORDER BY ingested_at DESC) = 1
    ) s
    ON t.FLIGHT_KEY = s.FLIGHT_KEY
    WHEN MATCHED THEN UPDATE SET
        FLIGHT_STATUS = s.flight_status,
        DEPARTURE_ESTIMATED = s.departure_estimated,
        DEPARTURE_ACTUAL = s.departure_actual,
        DEPARTURE_DELAY = s.departure_delay,
        ARRIVAL_ESTIMATED = s.arrival_estimated,
        ARRIVAL_ACTUAL = s.arrival_actual,
        ARRIVAL_DELAY = s.arrival_delay,
        UPDATED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (
        FLIGHT_KEY, FLIGHT_DATE, FLIGHT_STATUS, DEPARTURE_AIRPORT, ARRIVAL_AIRPORT,
        DEPARTURE_SCHEDULED, DEPARTURE_ESTIMATED, DEPARTURE_ACTUAL, DEPARTURE_DELAY,
        DEPARTURE_TERMINAL, DEPARTURE_GATE,
        ARRIVAL_SCHEDULED, ARRIVAL_ESTIMATED, ARRIVAL_ACTUAL, ARRIVAL_DELAY,
        ARRIVAL_TERMINAL, ARRIVAL_GATE,
        AIRLINE_NAME, AIRLINE_IATA, AIRLINE_ICAO,
        FLIGHT_NUMBER, FLIGHT_IATA, FLIGHT_ICAO, AIRCRAFT_REGISTRATION, IS_CODESHARE
    ) VALUES (
        s.FLIGHT_KEY, s.flight_date, s.flight_status, s.departure_airport, s.arrival_airport,
        s.departure_scheduled, s.departure_estimated, s.departure_actual, s.departure_delay,
        s.departure_terminal, s.departure_gate,
        s.arrival_scheduled, s.arrival_estimated, s.arrival_actual, s.arrival_delay,
        s.arrival_terminal, s.arrival_gate,
        s.airline_name, s.airline_iata, s.airline_icao,
        s.flight_number, s.flight_iata, s.flight_icao, s.aircraft_registration, s.IS_CODESHARE
    );
    RETURN 'ETL Complete';
END;
$$;

-- -----------------------------------------------------------------------------
-- Historical Backfill Procedure (fetches past N days of data)
-- Note: Each day = 1 API call. With 10K/month limit, use conservatively.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_BACKFILL_FLIGHT_SCHEDULE(p_days_back INT)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
    v_date DATE DEFAULT CURRENT_DATE();
    v_end_date DATE DEFAULT CURRENT_DATE();
    v_start_date DATE;
    v_count INT DEFAULT 0;
    v_date_str VARCHAR;
BEGIN
    v_start_date := DATEADD('day', -p_days_back, CURRENT_DATE());
    v_date := v_start_date;
    
    WHILE (v_date <= v_end_date) DO
        v_date_str := TO_VARCHAR(v_date, 'YYYY-MM-DD');
        BEGIN
            CALL {database}.{schema}.PROC_INGEST_FLIGHT_SCHEDULE('{airport['iata_code']}', :v_date_str);
            v_count := v_count + 1;
        EXCEPTION
            WHEN OTHER THEN
                -- Log error but continue with next day
                v_count := v_count;
        END;
        v_date := DATEADD('day', 1, v_date);
    END WHILE;
    
    -- Run ETL once at the end
    CALL {database}.{schema}.PROC_ETL_SCHEDULE_TO_FLIGHT_SCHEDULE();
    
    RETURN 'Backfill complete: processed ' || v_count::VARCHAR || ' days from ' || TO_VARCHAR(v_start_date) || ' to ' || TO_VARCHAR(v_end_date);
END;
$$;

-- -----------------------------------------------------------------------------
-- Window Backfill Procedure (past + future)
-- Loads from CURRENT_DATE() - p_days_back through CURRENT_DATE() + p_days_forward (inclusive).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_BACKFILL_FLIGHT_SCHEDULE_WINDOW(p_days_back INT, p_days_forward INT)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
    v_date DATE DEFAULT CURRENT_DATE();
    v_end_date DATE;
    v_start_date DATE;
    v_count INT DEFAULT 0;
    v_date_str VARCHAR;
BEGIN
    v_start_date := DATEADD('day', -p_days_back, CURRENT_DATE());
    v_end_date := DATEADD('day', p_days_forward, CURRENT_DATE());
    v_date := v_start_date;
    
    WHILE (v_date <= v_end_date) DO
        v_date_str := TO_VARCHAR(v_date, 'YYYY-MM-DD');
        BEGIN
            CALL {database}.{schema}.PROC_INGEST_FLIGHT_SCHEDULE('{airport['iata_code']}', :v_date_str);
            v_count := v_count + 1;
        EXCEPTION
            WHEN OTHER THEN
                v_count := v_count;
        END;
        v_date := DATEADD('day', 1, v_date);
    END WHILE;
    
    CALL {database}.{schema}.PROC_ETL_SCHEDULE_TO_FLIGHT_SCHEDULE();
    RETURN 'Backfill window complete: processed ' || v_count::VARCHAR || ' days from ' || TO_VARCHAR(v_start_date) || ' to ' || TO_VARCHAR(v_end_date);
END;
$$;

-- -----------------------------------------------------------------------------
-- Wrapper procedure for task (combines ingest + ETL)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {database}.{schema}.PROC_FLIGHT_SCHEDULE_INGEST_AND_ETL()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
    CALL {database}.{schema}.PROC_INGEST_FLIGHT_SCHEDULE('{airport['iata_code']}', CURRENT_DATE()::VARCHAR);
    CALL {database}.{schema}.PROC_ETL_SCHEDULE_TO_FLIGHT_SCHEDULE();
    RETURN 'Flight schedule ingest and ETL complete';
END;
$$;

-- -----------------------------------------------------------------------------
-- Scheduled Task (hourly updates)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TASK {database}.{schema}.TASK_FLIGHT_SCHEDULE_HOURLY
  WAREHOUSE = {warehouse}
  SCHEDULE = 'USING CRON 0 * * * * UTC'
  ALLOW_OVERLAPPING_EXECUTION = FALSE
AS
  CALL {database}.{schema}.PROC_FLIGHT_SCHEDULE_INGEST_AND_ETL();

-- -----------------------------------------------------------------------------
-- RUN INITIAL BACKFILL (window: last 2 days + next 2 days)
-- -----------------------------------------------------------------------------
CALL {database}.{schema}.PROC_BACKFILL_FLIGHT_SCHEDULE_WINDOW({backfill_days}, 2);

-- -----------------------------------------------------------------------------
-- START THE TASK
-- -----------------------------------------------------------------------------
ALTER TASK {database}.{schema}.TASK_FLIGHT_SCHEDULE_HOURLY RESUME;

-- Verify
SELECT 'Flight schedule setup complete. Task is now running.' AS status;
"""


def generate_all_sql(
    airport: dict,
    database: str,
    schema: str,
    warehouse: str,
    api_key: str = None,
    git_repo_stage_base: str = "@AVIA_INSTALLER.PUBLIC.AVIA_FLEET_REPO/branches/poc-v5-adsb-tar-sql",
    adsb_history_backfill_days: int = 5,
) -> dict:
    """Generate all SQL files.
    
    Execution order:
    1. Base infrastructure (database, schema, geometry tables)
    2. ADS-B setup (tables, procedures, real-time ingestion)
    3. ADS-B History backfill (downloads TAR files, processes with ST_DWITHIN filtering)
    4. Flight Schedule (loads schedule data with aircraft registrations) 
    5. Derived analytics tables
    
    ADS-B data is loaded first (real-time + history) before schedule.
    """
    files = {
        '01_base_v5.sql': generate_base_sql(airport, database, schema, warehouse, git_repo_stage_base),
    }
    
    # ADS-B setup runs SECOND (starts real-time ingestion immediately)
    files['02_adsb_v5.sql'] = generate_adsb_sql(
        airport,
        database,
        schema,
        warehouse,
        adsb_history_backfill_days=adsb_history_backfill_days,
    )
    
    # ADS-B History backfill runs THIRD (right after ADS-B setup)
    # Uses STAGE-BASED approach: download to stage, then process with SQL ST_DWITHIN
    files['03_adsb_history_backfill.sql'] = f"""-- =============================================================================
-- ADSB.LOL HISTORICAL BACKFILL FOR {airport['name']} ({airport['iata_code']})
-- Stage-based approach: Download TAR files to stage, then process with optimizations
-- Source: https://github.com/adsblol/globe_history_YYYY (ODbL 1.0 License)
-- =============================================================================

-- Backfill recent history as a one-time background task (last {int(adsb_history_backfill_days)} UTC days ending yesterday).
-- Safe to close Streamlit after this starts; progress is tracked in HELPER_ADSB_BACKFILL_STATUS.
CALL {database}.{schema}.PROC_START_BACKFILL_LAST_WEEK();

-- Start continuous retry for yesterday+today UTC, and trigger enrichment+derived refresh
-- after a day completes. This closes the "start-day gap" as soon as today's history
-- becomes available (often the next day).
CALL {database}.{schema}.PROC_START_BACKFILL_RETRY_UTC();

-- Verify results
SELECT 'HELPER_ADSB_LOL_RAW' AS tbl, COUNT(*) AS cnt FROM {database}.{schema}.HELPER_ADSB_LOL_RAW
UNION ALL SELECT 'ADSB_DATA', COUNT(*) FROM {database}.{schema}.ADSB_DATA;

-- Check backfill status
SELECT * FROM {database}.{schema}.HELPER_ADSB_BACKFILL_STATUS ORDER BY data_date;

-- Check task state / history
SHOW TASKS LIKE 'TASK_ADSB_BACKFILL_ONCE' IN SCHEMA {database}.{schema};
-- NOTE: INFORMATION_SCHEMA.TASK_HISTORY() can be restricted in some Streamlit execution contexts.
-- If you need history, run this in a worksheet (outside Streamlit), or use the Task Status widget.

-- NOTE: To backfill a specific day manually:
-- CALL {database}.{schema}.PROC_DOWNLOAD_TO_STAGE('2025-12-15');
-- CALL {database}.{schema}.PROC_PROCESS_FROM_STAGE('2025-12-15');
"""
    
    # Flight Schedule runs FOURTH (after ADS-B data is loaded)
    if api_key:
        # Flight schedule: last 2 days + next 2 days (window)
        # Align schedule backfill coverage with ADS-B history backfill window (best effort for matching).
        files['04_flight_schedule_v5.sql'] = generate_flight_schedule_sql(
            airport,
            database,
            schema,
            warehouse,
            api_key,
            int(adsb_history_backfill_days or 2),
        )
    
    # Derived analytics runs LAST (needs both Flight Schedule and ADS-B data)
    files['05_derived_v5.sql'] = generate_derived_sql(airport, database, schema, warehouse, adsb_history_backfill_days)
    
    return files


# ============================================================================
# TASK MONITOR (Snowflake only)
# ============================================================================

def render_task_monitor(database: str, schema: str):
    """Show task status + recent history for the selected airport DB/schema."""
    if not (IN_SNOWFLAKE and session):
        return

    st.divider()
    st.subheader("🧾 Task Status")

    col_a, col_b = st.columns([1, 3])
    with col_a:
        refresh = st.button("🔄 Refresh", use_container_width=True)
    with col_b:
        st.caption(f"Showing tasks in `{database}.{schema}`")

    if refresh:
        st.rerun()

    # Avoid burning inbound queries on every rerun: cache results briefly in session_state.
    # This also reduces the chance of hitting Streamlit-in-Snowflake inbound query limits.
    import time
    cache_ttl_s = 30
    cache_key = f"_task_status_cache::{database}.{schema}"
    cache = st.session_state.get(cache_key) or {}
    cache_age = (time.time() - float(cache.get("ts", 0) or 0)) if cache else 1e9

    # Current task state
    try:
        if refresh or ("tasks_df" not in cache) or (cache_age > cache_ttl_s):
            tasks_df = session.sql(f"SHOW TASKS IN SCHEMA {database}.{schema}").to_pandas()
            cache["tasks_df"] = tasks_df
            cache["ts"] = time.time()
            st.session_state[cache_key] = cache
        else:
            tasks_df = cache["tasks_df"]
        # Normalize column names.
        # In some environments pandas may preserve quotes in the column names (e.g. '"name"').
        def _norm_col(c):
            s = str(c).strip()
            # strip wrapping quotes repeatedly
            while (len(s) >= 2) and ((s[0] == s[-1]) and s[0] in ("'", '"')):
                s = s[1:-1].strip()
            return s.lower()
        tasks_df.columns = [_norm_col(c) for c in tasks_df.columns]
        if not tasks_df.empty:
            # Focus on relevant tasks (backfill + core ingestion tasks)
            interesting = {"TASK_ADSB_BACKFILL_ONCE", "TASK_INGEST_ADSB", "TASK_FLIGHT_SCHEDULE_HOURLY"}
            if "name" not in tasks_df.columns:
                st.warning(f"Could not fetch task status: missing column 'name'. Columns: {list(tasks_df.columns)[:20]}")
                tasks_df = None
            else:
                tasks_df["name_upper"] = tasks_df["name"].astype(str).str.upper()
        if tasks_df is not None and not tasks_df.empty:
            show_df = tasks_df[tasks_df["name_upper"].isin(interesting)].copy()
            if show_df.empty:
                show_df = tasks_df.copy()
            cols = [c for c in [
                "name", "state", "schedule", "warehouse", "last_suspended_on",
                "last_succeeded_on", "last_failed_on", "error_message"
            ] if c in show_df.columns]
            st.dataframe(show_df[cols], use_container_width=True, hide_index=True)
        elif tasks_df is not None:
            st.info("No tasks found in this schema.")
    except Exception as e:
        st.warning(f"Could not fetch task status: {str(e)[:200]}")

    # Recent task history can be permission-restricted in Streamlit contexts, and it costs extra queries.
    # Make it explicit/opt-in.
    show_history = st.checkbox(
        "Show recent task history (may require extra permissions)",
        value=False,
        help="Uses INFORMATION_SCHEMA.TASK_HISTORY(); may fail depending on your Streamlit execution context."
    )
    if show_history:
        try:
            hist_cache_key = f"_task_history_cache::{database}.{schema}"
            hcache = st.session_state.get(hist_cache_key) or {}
            h_age = (time.time() - float(hcache.get("ts", 0) or 0)) if hcache else 1e9
            if refresh or ("hist_df" not in hcache) or (h_age > cache_ttl_s):
                hist_df = session.sql(f"""
                    SELECT *
                    FROM TABLE({database}.INFORMATION_SCHEMA.TASK_HISTORY())
                    WHERE SCHEMA_NAME = '{schema}'
                      AND NAME ILIKE '%TASK_%'
                    ORDER BY SCHEDULED_TIME DESC
                    LIMIT 50
                """).to_pandas()
                hcache["hist_df"] = hist_df
                hcache["ts"] = time.time()
                st.session_state[hist_cache_key] = hcache
            else:
                hist_df = hcache["hist_df"]

            if not hist_df.empty:
                cols = [c for c in [
                    "name", "state", "scheduled_time", "query_start_time",
                    "completed_time", "error_code", "error_message"
                ] if c in hist_df.columns]
                st.caption("Recent task runs")
                st.dataframe(hist_df[cols], use_container_width=True, hide_index=True)
            else:
                st.caption("No recent history rows returned.")
        except Exception:
            # Keep UI clean; SHOW TASKS is the main view
            st.caption("Task history unavailable in this context.")


# ============================================================================
# MAIN APP
# ============================================================================

def main():
    st.markdown('<p class="main-header">✈️ Airport Analytics Installer</p>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">Generate and execute Snowflake setup scripts for any airport</p>', unsafe_allow_html=True)
    
    if IN_SNOWFLAKE:
        st.success("✅ Running in Snowflake Streamlit")
    else:
        st.info("ℹ️ Running locally (SQL execution disabled)")
    
    # Load airports
    airports_df = load_airports()
    
    if airports_df.empty:
        st.error("No airports loaded.")
        st.markdown(
            "This installer requires Snowflake Streamlit access to "
            "`OVERTURE_MAPS__BASE.CARTO.INFRASTRUCTURE`.\n\n"
            "Before running the installer, install these Snowflake Marketplace listings:\n"
            "- [Overture Maps - Base](https://app.snowflake.com/marketplace/listing/GZT0Z4CM1E9KV/carto-overture-maps-base)\n"
            "- [Overture Maps - Buildings](https://app.snowflake.com/marketplace/listing/GZT0Z4CM1E9KN/carto-overture-maps-buildings)\n"
            "- [Overture Maps - Transportation](https://app.snowflake.com/marketplace/listing/GZT0Z4CM1E9KJ/carto-overture-maps-transportation)\n"
        )
        return
    
    # Sidebar
    st.sidebar.header("⚙️ Configuration")
    
    # Use stable row indices as the selectbox value to avoid relying on IATA being present/unique.
    airport_idx = st.sidebar.selectbox(
        "🛫 Select Airport",
        options=list(airports_df.index),
        format_func=lambda i: (
            f"{airports_df.loc[i, 'AIRPORT_NAME']} "
            f"({airports_df.loc[i, 'AIRPORT_CODE_IATA'] or airports_df.loc[i, 'AIRPORT_CODE_ICAO']})"
        ),
    )
    selected_airport = airports_df.loc[airport_idx]
    # selected_airport is a Series row now
    
    st.sidebar.divider()
    st.sidebar.subheader("🔑 API Key")
    # Prefer secrets/env for deployments; allow optional override via UI.
    api_key_from_secrets = None
    try:
        if hasattr(st, "secrets") and ("AVIATIONSTACK_API_KEY" in st.secrets):
            api_key_from_secrets = str(st.secrets.get("AVIATIONSTACK_API_KEY") or "").strip() or None
    except Exception:
        api_key_from_secrets = None

    api_key_from_env = (os.getenv("AVIATIONSTACK_API_KEY", "") or "").strip() or None
    api_key_default = api_key_from_secrets or api_key_from_env

    if api_key_default:
        st.sidebar.success("✅ Aviationstack API key loaded (secrets/env)")
        api_key_override = st.sidebar.text_input(
            "Override Aviationstack API Key (optional)",
            type="password",
            value="",
            help="Leave blank to use Streamlit secrets/env. Get a key at aviationstack.com",
        )
        api_key = (api_key_override or "").strip() or api_key_default
    else:
        api_key = st.sidebar.text_input(
            "Aviationstack API Key",
            type="password",
            help="Required for flight schedule ingestion. Prefer Streamlit secrets: AVIATIONSTACK_API_KEY",
        )
        if not api_key:
            st.sidebar.caption("⚠️ Flight schedule requires API key")

    st.sidebar.divider()
    st.sidebar.subheader("🗂️ Historical Backfill")
    adsb_history_backfill_days = st.sidebar.number_input(
        "ADS-B history backfill days (UTC, ending yesterday)",
        min_value=0,
        max_value=30,
        value=5,
        step=1,
        help="How many full UTC days of ADS-B history to backfill on install (from globe_history GitHub releases). 0 disables the one-time history backfill.",
    )
    
    # Get warehouse from current session (uses the same warehouse as Streamlit app)
    if IN_SNOWFLAKE and session:
        try:
            warehouse_result = session.sql("SELECT CURRENT_WAREHOUSE()").collect()
            warehouse = warehouse_result[0][0] if warehouse_result else "COMPUTE_WH"
        except:
            warehouse = "COMPUTE_WH"
    else:
        warehouse = "COMPUTE_WH"
    
    
    
    # Main content
    col1, col2 = st.columns([2, 1])
    
    airport = {
        'name': selected_airport['AIRPORT_NAME'],
        'iata_code': selected_airport['AIRPORT_CODE_IATA'],
        'icao_code': selected_airport['AIRPORT_CODE_ICAO'],
        'airport_id': selected_airport.get('AIRPORT_ID'),
        # Geometry/centroid are fetched by id (keeps airport inventory query simpler).
        'geometry': None,
        'lat': None,
        'lon': None,
    }

    # Fetch shape/centroid from Overture for the selected airport record id.
    details = load_airport_geometry_by_id(airport.get("airport_id"))
    if details:
        airport["geometry"] = details.get("GEOMETRY")
        airport["lat"] = details.get("CENTER_LAT")
        airport["lon"] = details.get("CENTER_LON")
    else:
        st.warning("Could not fetch airport geometry for the selected record. Base install may fail until Overture data is available.")

    # Prefer IATA for naming, but allow ICAO fallback if IATA is missing.
    db_suffix = (airport.get('iata_code') or '').strip().upper() or (airport.get('icao_code') or '').strip().upper()
    database = f"AIRPORT_{db_suffix}"
    schema = "V5"
    
    with col1:
        st.subheader("📍 Selected Airport")
        st.metric("Database", f"{database}.{schema}")
        st.caption(f"{airport['name']} • {airport['iata_code']}/{airport['icao_code']}")
        if pd.notna(airport['lat']):
            st.caption(f"📍 {airport['lat']:.4f}°, {airport['lon']:.4f}°")

    # Git repo stage base for loading bundled CSVs (e.g., airlines.csv) via SQL COPY.
    # Must point at a Snowflake Git Repository object (not a schema stage).
    with st.expander("Advanced: Git repo stage path", expanded=False):
        git_repo_stage_base_input = st.text_input(
            "Git repo stage base",
            value="@AVIA_INSTALLER.PUBLIC.AVIA_FLEET_REPO/branches/poc-v5-adsb-tar-sql",
            help="Example: @REPO_NAME/branches/poc-v5-adsb-tar-sql (or fully-qualified @DB.SCHEMA.REPO_NAME/branches/poc-v5-adsb-tar-sql). Do not include a trailing slash.",
        )
        git_repo_stage_base = _normalize_git_repo_stage_base(git_repo_stage_base_input)
        st.caption(f"Normalized: `{git_repo_stage_base}`")
    
    st.divider()
    
    # Buttons
    col_btn1, col_btn2, col_btn3 = st.columns(3)
    
    with col_btn1:
        generate_clicked = st.button("🔨 Generate SQL", type="primary", use_container_width=True)
    
    with col_btn2:
        execute_clicked = st.button(
            "⚡ Execute in Snowflake",
            disabled=not IN_SNOWFLAKE,
            use_container_width=True,
            help="Run the SQL directly in Snowflake" if IN_SNOWFLAKE else "Only available in Snowflake Streamlit"
        )
    
    if generate_clicked or execute_clicked:
        with st.spinner("Generating SQL..."):
            sql_files = generate_all_sql(
                airport, database, schema, warehouse, 
                api_key if api_key else None,
                git_repo_stage_base=git_repo_stage_base,
                adsb_history_backfill_days=int(adsb_history_backfill_days),
            )
            
            st.session_state['sql_files'] = sql_files
            st.session_state['database'] = database
        
        st.success(f"✅ Generated {len(sql_files)} SQL files")

    # Preview generated SQL before execution
    if 'sql_files' in st.session_state and st.session_state['sql_files']:
        st.divider()
        st.subheader("🧾 SQL Preview")
        st.caption("Review the generated SQL below before clicking **Execute in Snowflake**.")
        for filename, sql_content in st.session_state['sql_files'].items():
            with st.expander(f"📄 {filename}", expanded=False):
                st.code(_mask_sql_secrets(sql_content), language="sql")
    
    # Execute if requested
    if execute_clicked and IN_SNOWFLAKE and 'sql_files' in st.session_state:
        st.divider()
        st.subheader("⚡ Executing SQL...")
        
        def split_sql_statements(sql_content):
            """Split SQL into statements, respecting $$ procedure blocks."""
            statements = []
            current = []
            in_dollar_block = False
            
            lines = sql_content.split('\n')
            for line in lines:
                stripped = line.strip()
                
                # Skip standalone comments and empty lines when not building a statement
                if not current and not in_dollar_block:
                    if not stripped or stripped.startswith('--'):
                        continue
                
                # Check for $$ delimiter
                if '$$' in line:
                    current.append(line)
                    dollar_count = line.count('$$')
                    if dollar_count % 2 == 1:  # Odd number toggles state
                        in_dollar_block = not in_dollar_block
                    
                    # If we just closed a block and line ends with ;, end statement
                    if not in_dollar_block and stripped.endswith(';'):
                        stmt = '\n'.join(current).strip()
                        if stmt:
                            statements.append(stmt)
                        current = []
                    continue
                
                current.append(line)
                
                # If we're not in a $$ block and line ends with ;, it's end of statement
                if not in_dollar_block and stripped.endswith(';'):
                    stmt = '\n'.join(current).strip()
                    if stmt and not stmt.startswith('--'):
                        statements.append(stmt)
                    current = []
            
            # Add any remaining content
            if current:
                stmt = '\n'.join(current).strip()
                if stmt and not stmt.startswith('--'):
                    statements.append(stmt)
            
            return statements
        
        overall_error_count = 0
        for filename, sql_content in st.session_state['sql_files'].items():
            with st.expander(f"📄 {filename}", expanded=True):
                try:
                    statements = split_sql_statements(sql_content)

                    # Remove USE statements entirely (we fully-qualify object names)
                    statements = [s for s in statements if not s.strip().upper().startswith('USE ')]
                    
                    st.info(f"Found {len(statements)} statements to execute")
                    
                    success_count = 0
                    error_count = 0
                    
                    # Create a list of placeholders for all statements
                    placeholders = []
                    for i, stmt in enumerate(statements):
                        placeholders.append(st.empty())
                        stmt_preview = stmt[:100].replace('\n', ' ') + ('...' if len(stmt) > 100 else '')
                        placeholders[i].write(f"⚪ {i+1}. `{stmt_preview}` (Pending)")
                    
                    for i, stmt in enumerate(statements):
                        # Show abbreviated statement
                        stmt_preview = stmt[:100].replace('\n', ' ') + ('...' if len(stmt) > 100 else '')
                        
                        # Update status to In Progress
                        placeholders[i].write(f"⏳ {i+1}. `{stmt_preview}` (Running...)")
                        
                        try:
                            session.sql(stmt).collect()
                            placeholders[i].write(f"✅ {i+1}. `{stmt_preview}`")
                            success_count += 1
                        except Exception as e:
                            placeholders[i].error(f"❌ {i+1}. `{stmt_preview}`\n   Error: {str(e)[:200]}")
                            error_count += 1
                    
                    if error_count == 0:
                        st.success(f"✅ All {success_count} statements executed successfully!")
                    else:
                        st.warning(f"⚠️ Completed with {error_count} errors out of {success_count + error_count} statements")
                    overall_error_count += error_count
                        
                except Exception as e:
                    st.error(f"Error processing file: {e}")
                    overall_error_count += 1

        # Airline reference is loaded via SQL (COPY INTO) during base install.
    
    # Display generated SQL
    if 'sql_files' in st.session_state:
        st.divider()
        st.subheader("📄 Generated SQL")
        
        tabs = st.tabs(list(st.session_state['sql_files'].keys()))
        for tab, (filename, content) in zip(tabs, st.session_state['sql_files'].items()):
            with tab:
                masked = _mask_sql_secrets(content)
                st.code(masked, language="sql")
                st.download_button(f"📥 Download {filename}", masked, file_name=filename, mime="text/plain")
    
    # Instructions - always show with the currently selected airport
    st.divider()
    st.subheader("📋 Next Steps")
    
    if IN_SNOWFLAKE:
        st.markdown(f"""
        1. Click **Execute in Snowflake** to run the SQL directly
        2. **Everything runs automatically:**
           - Flight Schedule window: last 2 days + next 2 days
           - ADS-B historical backfill: configurable (UTC days ending yesterday)
           - Tasks started (real-time ADS-B every minute, Flight Schedule hourly)
           - Derived tables refreshed
        3. Monitor the database: `{database}.V5`
        """)
    else:
        st.markdown(f"""
        1. Download the SQL files
        2. Run them in Snowflake worksheets in order (01_, 02_, 03_, 04_, 05_)
        3. **Everything runs automatically** - no manual steps needed!
        4. Deploy the dashboard Streamlit app pointing to `{database}.V5`
        """)

    # Task monitor for the selected airport (Snowflake only)
    render_task_monitor(database, schema)


if __name__ == "__main__":
    main()

