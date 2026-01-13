"""
Shared utility functions for the Flight Tracking Dashboard
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import math
import json
from snowflake.snowpark.context import get_active_session

# =============================================================================
# DATABASE SELECTION UTILITIES
# =============================================================================

@st.cache_data(ttl=60)
def get_available_airports():
    """
    Query Snowflake for available AIRPORT_* databases and return airport info.
    Returns a list of dicts with 'database', 'iata_code', 'airport_name'.
    """
    session = get_active_session()
    
    def _has_v5_airport_geometry(db_name: str) -> bool:
        """Return True if db has V5.PROPERTIES_AIRPORT (our installer baseline object)."""
        try:
            df = session.sql(f"""
                SELECT 1
                FROM {db_name}.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = 'V5'
                  AND TABLE_NAME = 'PROPERTIES_AIRPORT'
                LIMIT 1
            """).to_pandas()
            return df is not None and not df.empty
        except Exception:
            return False

    try:
        # Query for airport databases in the AIRPORT_XXX format (3-char suffix).
        # This avoids picking non-airport helper DBs like AIRPORT_INSTALLER.
        databases_df = session.sql("""
            SELECT DATABASE_NAME
            FROM INFORMATION_SCHEMA.DATABASES
            WHERE REGEXP_LIKE(DATABASE_NAME, '^AIRPORT_[A-Z0-9]{3}$')
            ORDER BY DATABASE_NAME
        """).to_pandas()
        
        airports = []
        for _, row in databases_df.iterrows():
            db_name = row['DATABASE_NAME']

            # Only treat databases as "airports" if they look installed (have V5.PROPERTIES_AIRPORT)
            if not _has_v5_airport_geometry(db_name):
                continue

            # Extract IATA code from database name (AIRPORT_SAN -> SAN)
            iata_code = db_name.replace('AIRPORT_', '')
            
            # Get airport name from PROPERTIES_AIRPORT (created by installer; always present for valid airports)
            try:
                geom_df = session.sql(f"""
                    SELECT airport_name
                    FROM {db_name}.V5.PROPERTIES_AIRPORT
                    LIMIT 1
                """).to_pandas()
                airport_name = str(geom_df['AIRPORT_NAME'].iloc[0]) if geom_df is not None and len(geom_df) > 0 else iata_code
            except Exception:
                airport_name = iata_code
            
            airports.append({
                'database': db_name,
                'iata_code': iata_code,
                'airport_name': airport_name
            })
        
        return airports
    except Exception as e:
        st.warning(f"Could not query available airports: {e}")
        return []


def get_selected_database():
    """
    Get the currently selected database from session state.
    Returns the database name (e.g., 'AIRPORT_SAN').
    """
    # Ensure selected_database is initialized AND valid
    airports = get_available_airports()
    valid_dbs = {a["database"] for a in airports} if airports else set()

    if ('selected_database' not in st.session_state) or (st.session_state.get('selected_database') not in valid_dbs):
        # V5-only dashboard: if no valid airports, return None and let pages stop with guidance.
        if airports:
            st.session_state['selected_database'] = airports[0]['database']
        else:
            return None
    
    return st.session_state.get('selected_database')


def get_schema():
    """Get the schema name (V5)."""
    return 'V5'


def get_full_table_name(table_name):
    """
    Get the fully qualified table name using selected database.
    Example: get_full_table_name('ADSB_DATA') -> 'AIRPORT_SAN.V5.ADSB_DATA'
    """
    return f"{get_selected_database()}.{get_schema()}.{table_name}"


def render_airport_selector(sidebar=True):
    """
    Render the airport selector dropdown.
    Call this at the top of each page that needs database selection.
    
    Args:
        sidebar: If True, render in sidebar. If False, render in main content.
    
    Returns:
        The selected database name.
    """
    airports = get_available_airports()
    
    if not airports:
        st.warning("No airport databases found.")
        return None
    
    # Use the database name as the widget value to avoid manual rerun loops.
    options = [a["database"] for a in airports]

    # Ensure selected_database is initialized and valid
    if st.session_state.get("selected_database") not in set(options):
        st.session_state["selected_database"] = options[0]

    container = st.sidebar if sidebar else st
    prev_db = st.session_state.get("_prev_selected_database")
    selected_db = container.selectbox(
        "🛫 Select Airport",
        options=options,
        index=options.index(st.session_state["selected_database"]),
        key="selected_database",
        format_func=lambda db_name: next(
            (f"{a['airport_name']} ({a['iata_code']})" for a in airports if a["database"] == db_name),
            db_name,
        ),
    )

    # Guardrail: Streamlit caches are global to the app process and do NOT automatically
    # incorporate session_state. If the selected DB changes, cached query results from
    # the previously selected airport can "bleed" into the UI. Clear caches on change.
    if prev_db != selected_db:
        st.session_state["_prev_selected_database"] = selected_db
        try:
            st.cache_data.clear()
        except Exception:
            pass
        try:
            st.cache_resource.clear()
        except Exception:
            pass

    return selected_db


# =============================================================================
# COLOR SCHEMES
# =============================================================================

# Color schemes
ALTITUDE_COLORS = {
    'low': [65, 182, 196],      # Cyan - 0-10,000 ft
    'medium': [127, 205, 187],   # Teal - 10,000-25,000 ft
    'high': [199, 233, 180],     # Light green - 25,000-35,000 ft
    'very_high': [237, 248, 177] # Yellow - 35,000+ ft
}

SPEED_COLORS = {
    'slow': [158, 202, 225],     # Light blue - 0-200 knots
    'medium': [49, 130, 189],    # Blue - 200-400 knots
    'fast': [222, 45, 38]        # Red - 400+ knots
}

def get_altitude_color(altitude):
    """
    Return RGB color based on altitude
    """
    try:
        alt = float(altitude)
        if alt < 10000:
            return ALTITUDE_COLORS['low']
        elif alt < 25000:
            return ALTITUDE_COLORS['medium']
        elif alt < 35000:
            return ALTITUDE_COLORS['high']
        else:
            return ALTITUDE_COLORS['very_high']
    except:
        return [128, 128, 128]  # Gray for unknown

def get_speed_color(speed):
    """
    Return RGB color based on speed
    """
    try:
        spd = float(speed)
        if spd < 200:
            return SPEED_COLORS['slow']
        elif spd < 400:
            return SPEED_COLORS['medium']
        else:
            return SPEED_COLORS['fast']
    except:
        return [128, 128, 128]  # Gray for unknown

def format_altitude(alt):
    """Format altitude with commas"""
    try:
        return f"{float(alt):,.0f} ft"
    except:
        return "N/A"

def format_speed(speed):
    """Format speed with units"""
    try:
        return f"{float(speed):.1f} knots"
    except:
        return "N/A"

def format_coordinates(lat, lon):
    """Format coordinates nicely"""
    try:
        lat_dir = "N" if lat >= 0 else "S"
        lon_dir = "E" if lon >= 0 else "W"
        return f"{abs(lat):.4f}°{lat_dir}, {abs(lon):.4f}°{lon_dir}"
    except:
        return "N/A"

def create_metric_card(label, value, delta=None, delta_color="normal"):
    """
    Create a styled metric display
    """
    st.metric(label=label, value=value, delta=delta, delta_color=delta_color)

def get_flight_phase(altitude, velocity):
    """
    Determine flight phase based on altitude and velocity
    """
    try:
        alt = float(altitude)
        vel = float(velocity)
        
        if alt < 1000:
            if vel < 50:
                return "Ground"
            else:
                return "Takeoff/Landing"
        elif alt < 10000:
            if vel > 200:
                return "Climbing"
            else:
                return "Descending"
        elif alt >= 10000:
            if vel < 100:
                return "Descending"
            else:
                return "Cruise"
        else:
            return "Unknown"
    except:
        return "Unknown"


def create_tooltip_html():
    """
    Create HTML template for map tooltips
    """
    return {
        "html": """
        <div style="background-color: rgba(20, 30, 48, 0.95); padding: 12px; border-radius: 8px; font-family: 'Segoe UI', sans-serif;">
            <div style="font-size: 16px; font-weight: bold; color: #4FC3F7; margin-bottom: 8px;">
                ✈️ {FLIGHT}
            </div>
            <div style="color: #B0BEC5; font-size: 13px; line-height: 1.6;">
                <div><strong>Registration:</strong> {REGISTRATION}</div>
                <div><strong>Altitude:</strong> {ALTITUDE_BARO} ft</div>
                <div><strong>Speed:</strong> {VELOCITY} knots</div>
                <div><strong>Heading:</strong> {TRACK}°</div>
            </div>
        </div>
        """,
        "style": {
            "backgroundColor": "transparent",
            "color": "white"
        }
    }

def calculate_map_bounds(data, lat_col='LAT', lon_col='LON', padding=0.1):
    """
    Calculate optimal map view state based on data bounding box
    
    Args:
        data: DataFrame with latitude and longitude columns
        lat_col: Name of latitude column
        lon_col: Name of longitude column
        padding: Percentage padding to add to bounds (0.1 = 10%)
    
    Returns:
        dict with latitude, longitude, and zoom level
    """
    if data is None or len(data) == 0:
        # Default to San Diego area
        return {
            'latitude': 32.7335,
            'longitude': -117.1896,
            'zoom': 9
        }
    
    try:
        # Get min/max bounds
        min_lat = data[lat_col].min()
        max_lat = data[lat_col].max()
        min_lon = data[lon_col].min()
        max_lon = data[lon_col].max()
        
        # Calculate center
        center_lat = (min_lat + max_lat) / 2
        center_lon = (min_lon + max_lon) / 2
        
        # Calculate span with padding
        lat_span = (max_lat - min_lat) * (1 + padding)
        lon_span = (max_lon - min_lon) * (1 + padding)
        
        # Handle edge case of single point or very small area
        if lat_span < 0.01:
            lat_span = 0.1
        if lon_span < 0.01:
            lon_span = 0.1
        
        # Calculate zoom level based on span
        # Zoom formula: larger span = lower zoom number
        max_span = max(lat_span, lon_span)
        
        if max_span > 10:
            zoom = 5
        elif max_span > 5:
            zoom = 6
        elif max_span > 2:
            zoom = 7
        elif max_span > 1:
            zoom = 8
        elif max_span > 0.5:
            zoom = 9
        elif max_span > 0.2:
            zoom = 10
        elif max_span > 0.1:
            zoom = 11
        elif max_span > 0.05:
            zoom = 12
        else:
            zoom = 13
        
        return {
            'latitude': center_lat,
            'longitude': center_lon,
            'zoom': zoom
        }
        
    except Exception as e:
        # Fallback to San Diego area
        return {
            'latitude': 32.7335,
            'longitude': -117.1896,
            'zoom': 9
        }

def get_airport_default_view(session, padding: float = 0.05):
    """
    Compute default map view (center and zoom) from the configured airport polygon bounding box.
    Returns a dict with latitude, longitude, zoom keys.
    """
    try:
        db = get_selected_database()
        schema = get_schema()
        q = """
        SELECT 
            min_lat,
            max_lat,
            min_lon,
            max_lon
        FROM {db}.{schema}.PROPERTIES_AIRPORT
        LIMIT 1
        """
        res = session.sql(q.format(db=db, schema=schema)).collect()
        if not res:
            # Fallback to static SAN approximate center
            return {'latitude': 32.7338, 'longitude': -117.1933, 'zoom': 13}
        r = res[0]
        min_lat = float(r['MIN_LAT'])
        max_lat = float(r['MAX_LAT'])
        min_lon = float(r['MIN_LON'])
        max_lon = float(r['MAX_LON'])
        # Create a tiny DataFrame compatible with calculate_map_bounds
        df = pd.DataFrame({'LAT': [min_lat, max_lat], 'LON': [min_lon, max_lon]})
        return calculate_map_bounds(df, padding=padding)
    except Exception:
        return {'latitude': 32.7338, 'longitude': -117.1933, 'zoom': 13}

@st.cache_data(ttl=3600)
def get_airport_bbox(_session):
    """Return bounding box + center for the selected airport from <db>.V5.PROPERTIES_AIRPORT.
    Used by pages to avoid hardcoded lat/lon bounds."""
    db = get_selected_database()
    schema = get_schema()
    try:
        q = f"""
        SELECT
          MIN_LAT, MAX_LAT, MIN_LON, MAX_LON,
          CENTER_LAT, CENTER_LON
        FROM {db}.{schema}.PROPERTIES_AIRPORT
        LIMIT 1
        """
        r = _session.sql(q).collect()
        if not r:
            raise Exception("No PROPERTIES_AIRPORT rows")
        row = r[0]
        return {
            "min_lat": float(row["MIN_LAT"]),
            "max_lat": float(row["MAX_LAT"]),
            "min_lon": float(row["MIN_LON"]),
            "max_lon": float(row["MAX_LON"]),
            "center_lat": float(row["CENTER_LAT"]),
            "center_lon": float(row["CENTER_LON"]),
        }
    except Exception:
        # Fallback to SAN-ish bounds to keep pages usable in partial installs
        return {
            "min_lat": 32.0,
            "max_lat": 33.5,
            "min_lon": -118.0,
            "max_lon": -116.5,
            "center_lat": 32.7338,
            "center_lon": -117.1933,
        }

def apply_custom_css():
    """
    Apply custom CSS styling to the dashboard - minimalistic light theme
    """
    st.markdown("""
    <style>
    /* Compact title styling */
    h1 {
        font-size: 1.8rem !important;
        font-weight: 600 !important;
        margin-bottom: 0.5rem !important;
        padding-top: 0.5rem !important;
    }
    
    h2 {
        font-size: 1.3rem !important;
        font-weight: 500 !important;
        margin-top: 1rem !important;
        margin-bottom: 0.5rem !important;
    }
    
    h3 {
        font-size: 1.1rem !important;
        font-weight: 500 !important;
        margin-top: 0.8rem !important;
        margin-bottom: 0.4rem !important;
    }
    
    /* Compact metric styling */
    [data-testid="stMetricValue"] {
        font-size: 1.5rem !important;
        font-weight: 600 !important;
    }
    
    [data-testid="stMetricLabel"] {
        font-size: 0.85rem !important;
    }
    
    /* Reduce spacing */
    .block-container {
        padding-top: 2rem !important;
        padding-bottom: 1rem !important;
    }
    
    /* Compact dividers */
    hr {
        margin-top: 1rem !important;
        margin-bottom: 1rem !important;
    }
    
    /* Compact expanders */
    .streamlit-expanderHeader {
        font-size: 0.95rem !important;
    }
    
    /* Keep default Streamlit multipage navigation visible */
    </style>
    """, unsafe_allow_html=True)

def get_date_range(session):
    """
    Get the min and max dates available in the dataset
    """
    db = get_selected_database()
    schema = get_schema()
    query = f"""
    SELECT 
        MIN(TIMESTAMP)::DATE as min_date,
        MAX(TIMESTAMP)::DATE as max_date
    FROM {db}.{schema}.ADSB_DATA_LOCAL
    """
    result = session.sql(query).collect()
    if result:
        return result[0]['MIN_DATE'], result[0]['MAX_DATE']
    return None, None


# =============================================================================
# REUSABLE TIME PERIOD FILTER (Last 7 / Last 30 / Custom)
# =============================================================================

@st.cache_data(ttl=300)
def get_table_date_bounds(_session, table_fqn: str, ts_col: str) -> tuple:
    """Return (min_date, max_date) for a table timestamp column.

    Args:
        table_fqn: Fully qualified table name, e.g. AIRPORT_YVR.V5.ADSB_DATA_LOCAL
        ts_col: Timestamp column name/expression, e.g. TIMESTAMP or t_entry
    """
    # Very small guardrail against accidental SQL injection via ts_col.
    # (table_fqn is constructed by our code, not user input.)
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\\.]+", ts_col or ""):
        raise ValueError(f"Invalid ts_col: {ts_col}")
    q = f"""
    SELECT
      MIN({ts_col})::DATE AS min_date,
      MAX({ts_col})::DATE AS max_date
    FROM {table_fqn}
    """
    r = _session.sql(q).collect()
    if not r:
        return None, None
    return r[0]["MIN_DATE"], r[0]["MAX_DATE"]


def render_time_period_filter(
    min_date,
    max_date,
    *,
    key_prefix: str,
    default_period: str = "Last 7 Days",
):
    """Reusable sidebar filter: Last 7 Days / Last 30 Days / Custom Range.

    Returns (start_date, end_date, selected_period).

    Notes:
    - Clamps defaults to available data bounds to avoid StreamlitAPIException.
    - Works with date objects (not datetimes).
    """
    from datetime import datetime as _dt, timedelta as _td

    def _clamp(d, lo, hi):
        if d is None:
            return None
        if lo is not None and d < lo:
            return lo
        if hi is not None and d > hi:
            return hi
        return d

    periods = ["Last 7 Days", "Last 30 Days", "Custom Range"]
    if default_period not in periods:
        default_period = "Last 7 Days"

    selected_period = st.radio(
        "Select Time Period",
        periods,
        index=periods.index(default_period),
        key=f"{key_prefix}__time_period",
    )

    # Establish safe min/max bounds
    fallback_today = _dt.now().date()
    lo = min_date
    hi = max_date
    if hi is None:
        hi = fallback_today
    if lo is None:
        lo = hi - _td(days=365)

    if selected_period == "Custom Range":
        # Default to last 7 days ending at hi (clamped)
        default_end = _clamp(hi, lo, hi)
        default_start = _clamp(default_end - _td(days=7), lo, hi)
        date_range = st.date_input(
            "Select Date Range",
            value=(default_start, default_end),
            min_value=lo,
            max_value=hi,
            key=f"{key_prefix}__date_range",
        )
        if isinstance(date_range, (list, tuple)) and len(date_range) >= 2:
            start_date, end_date = date_range[0], date_range[1]
        elif isinstance(date_range, (list, tuple)) and len(date_range) == 1:
            start_date = end_date = date_range[0]
        else:
            start_date = end_date = date_range
    else:
        days = 7 if selected_period == "Last 7 Days" else 30
        end_date = _clamp(hi, lo, hi)
        start_date = _clamp(end_date - _td(days=days), lo, hi)

    # Final clamp (paranoia)
    start_date = _clamp(start_date, lo, hi)
    end_date = _clamp(end_date, lo, hi)

    return start_date, end_date, selected_period


@st.cache_data(ttl=3600)
def get_airline_name_map(_session, start_date=None, end_date=None):
    """Return dict mapping airline code (ICAO and IATA) to full marketing carrier name.
    Derived on-demand from FLIGHT_SCHEDULE (we no longer create AIRLINE_NAME_MAP).
    """
    db = get_selected_database()
    schema = get_schema()
    # Optional date filtering: helps performance on large schedules
    where_parts = ["AIRLINE_ICAO IS NOT NULL", "AIRLINE_NAME IS NOT NULL"]
    if start_date and end_date:
        where_parts.append(f"FLIGHT_DATE BETWEEN '{start_date}' AND '{end_date}'")
    where_sql = "WHERE " + " AND ".join(where_parts)

    q = f"""
      WITH base AS (
      SELECT 
          TRIM(UPPER(AIRLINE_ICAO)) AS airline_icao,
          TRIM(UPPER(AIRLINE_IATA)) AS airline_iata,
          TRIM(AIRLINE_NAME) AS airline_name
    FROM {db}.{schema}.FLIGHT_SCHEDULE
    {where_sql}
      )
      SELECT code, MAX(airline_name) AS airline_name
      FROM (
        SELECT airline_icao AS code, airline_name FROM base WHERE airline_icao IS NOT NULL AND airline_icao <> ''
        UNION ALL
        SELECT airline_iata AS code, airline_name FROM base WHERE airline_iata IS NOT NULL AND airline_iata <> ''
      )
    GROUP BY 1
    """
    name_map = {}
    # 1) Prefer schedule-derived names (most accurate marketing name)
    try:
        df = _session.sql(q).to_pandas()
        if df is not None and not df.empty:
            name_map.update({str(r['CODE']): str(r['AIRLINE_NAME']) for _, r in df.iterrows()})
    except Exception:
        pass

    # 2) Fallback to standing airline dim (covers flights without schedule matches)
    try:
        dim_q = f"""
          SELECT code, MAX(airline_name) AS airline_name
          FROM (
            SELECT TRIM(UPPER(AIRLINE_ICAO)) AS code, TRIM(AIRLINE_NAME) AS airline_name
            FROM {db}.{schema}.HELPER_AIRLINE_DIM
            WHERE AIRLINE_ICAO IS NOT NULL AND TRIM(AIRLINE_ICAO) <> ''
            UNION ALL
            SELECT TRIM(UPPER(AIRLINE_IATA)) AS code, TRIM(AIRLINE_NAME) AS airline_name
            FROM {db}.{schema}.HELPER_AIRLINE_DIM
            WHERE AIRLINE_IATA IS NOT NULL AND TRIM(AIRLINE_IATA) <> ''
          )
          WHERE code IS NOT NULL AND code <> '' AND airline_name IS NOT NULL AND airline_name <> ''
          GROUP BY 1
        """
        dim_df = _session.sql(dim_q).to_pandas()
        if dim_df is not None and not dim_df.empty:
            for _, r in dim_df.iterrows():
                code = str(r['CODE'])
                if code and code not in name_map:
                    name_map[code] = str(r['AIRLINE_NAME'])
    except Exception:
        pass

    return name_map


@st.cache_data(ttl=300)
def get_flight_headers_from_schedule(_session, service_date, flight_ids, db_prefix: str | None = None):
    """Return per-flight header fields from FLIGHT_SCHEDULE for a given service_date.

    This is used to enrich per-flight lists/tooltips when ADSB enrichment hasn't populated
    airline + O/D yet. We match by FLIGHT_ICAO/FLIGHT_IATA with a ±1 day tolerance to
    account for UTC ADSB dates vs local schedule dates.

    Returns a DataFrame with columns:
      - flight_id
      - airline_name
      - origin_airport
      - destination_airport
      - schedule_flight_number
    """
    try:
        if not flight_ids:
            import pandas as _pd
            return _pd.DataFrame(columns=["FLIGHT_ID", "AIRLINE_NAME", "ORIGIN_AIRPORT", "DESTINATION_AIRPORT", "SCHEDULE_FLIGHT_NUMBER"])

        # Normalize inputs
        date_str = str(service_date)
        ids = [str(x).strip() for x in flight_ids if str(x).strip()]
        if not ids:
            import pandas as _pd
            return _pd.DataFrame(columns=["FLIGHT_ID", "AIRLINE_NAME", "ORIGIN_AIRPORT", "DESTINATION_AIRPORT", "SCHEDULE_FLIGHT_NUMBER"])

        if db_prefix is None:
            db = get_selected_database()
            schema = get_schema()
            db_prefix = f"{db}.{schema}"

        # Build a VALUES list safely (these values originate from our DB, but still escape quotes).
        safe_ids = [x.replace("'", "''") for x in ids[:2000]]
        values_sql = ", ".join(["('%s')" % x for x in safe_ids])

        q = f"""
        WITH ids AS (
          SELECT column1::STRING AS flight_id
          FROM VALUES {values_sql}
        ),
        candidates AS (
          SELECT
            i.flight_id,
            s.AIRLINE_NAME AS airline_name,
            s.DEPARTURE_AIRPORT AS origin_airport,
            s.ARRIVAL_AIRPORT AS destination_airport,
            s.FLIGHT_NUMBER AS schedule_flight_number,
            IFF(UPPER(TRIM(s.FLIGHT_ICAO)) = UPPER(TRIM(i.flight_id)), 0,
                IFF(UPPER(TRIM(s.FLIGHT_IATA)) = UPPER(TRIM(i.flight_id)), 1, 2)
            ) AS match_rank,
            ABS(DATEDIFF('day', s.FLIGHT_DATE, '{date_str}'::DATE)) AS date_diff,
            s.UPDATED_AT AS updated_at
          FROM ids i
          JOIN {db_prefix}.FLIGHT_SCHEDULE s
            ON s.FLIGHT_DATE BETWEEN DATEADD('day', -1, '{date_str}'::DATE) AND DATEADD('day', 1, '{date_str}'::DATE)
           AND (
                UPPER(TRIM(s.FLIGHT_ICAO)) = UPPER(TRIM(i.flight_id))
             OR UPPER(TRIM(s.FLIGHT_IATA)) = UPPER(TRIM(i.flight_id))
           )
        )
        SELECT
          flight_id AS flight_id,
          airline_name,
          origin_airport,
          destination_airport,
          schedule_flight_number
        FROM candidates
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY flight_id
          ORDER BY match_rank ASC, date_diff ASC, updated_at DESC
        ) = 1
        """
        return _session.sql(q).to_pandas()
    except Exception:
        import pandas as _pd
        return _pd.DataFrame(columns=["FLIGHT_ID", "AIRLINE_NAME", "ORIGIN_AIRPORT", "DESTINATION_AIRPORT", "SCHEDULE_FLIGHT_NUMBER"])


def render_navigation(current_page_label: str = "Flight Tracker") -> None:
    """
    Render a global navigation dropdown in the sidebar and switch pages when changed.
    """
    import streamlit as st  # local import to avoid tooling warnings

    page_label_to_path = {
        "Flight Tracker": "pages/1_Flight_Tracker.py",
        "Airport Activity": "pages/3_Airport_Activity.py",
        "Traffic Analysis": "pages/2_Traffic_Analysis.py",
        "Gate Analysis": "pages/4_Gate_Analysis.py",
        "Runway Crossings": "pages/9_Runway_Crossings.py",
        "Operations": "pages/5_Operations.py",
        "Monitoring": "pages/6_Monitoring.py",
        "Performance": "pages/7_Performance.py",
    }

    with st.sidebar:
        st.subheader("📍 Navigation")
        page_options = list(page_label_to_path.keys())
        try:
            default_index = page_options.index(current_page_label)
        except ValueError:
            default_index = 0
        selected_page = st.selectbox(
            "Go to page:",
            options=page_options,
            index=default_index,
            label_visibility="collapsed",
        )
        if selected_page != current_page_label:
            switch_fn = getattr(st, "switch_page", None)
            if callable(switch_fn):
                try:
                    switch_fn(page_label_to_path[selected_page])
                except Exception:
                    pass
            else:
                st.caption("Use the default sidebar navigation if this selector does not switch pages.")


# =============================================================================
# INFRASTRUCTURE LAYER UTILITIES
# =============================================================================

# Color mapping for infrastructure layer types (RGB values for pydeck)
INFRASTRUCTURE_COLORS = {
    # Aviation (aeroway)
    'runway': [60, 60, 60],           # Dark gray
    'taxiway': [255, 193, 7],         # Amber
    'taxilane': [255, 213, 79],       # Light amber
    'gate': [76, 175, 80],            # Green
    'apron': [158, 158, 158],         # Light gray
    'helipad': [156, 39, 176],        # Purple
    'jet_bridge': [0, 150, 136],      # Teal
    'stopway': [244, 67, 54],         # Red
    'aerodrome': [96, 125, 139],      # Blue gray
    # Barriers
    'fence': [121, 85, 72],           # Brown
    'wall': [78, 52, 46],             # Dark brown
    'lift_gate': [139, 195, 74],      # Light green
    'bollard': [255, 152, 0],         # Orange
    # Transportation
    'bridge': [63, 81, 181],          # Indigo
    'street_lamp': [255, 235, 59],    # Yellow
    'crossing': [255, 255, 255],      # White
    'stop': [244, 67, 54],            # Red
    'bus_stop': [33, 150, 243],       # Blue
    'traffic_signals': [76, 175, 80], # Green
    # Amenities
    'parking': [33, 150, 243],        # Blue
    'toilets': [0, 188, 212],         # Cyan
    'drinking_water': [3, 169, 244],  # Light blue
    'atm': [255, 193, 7],             # Amber
    # Default
    'default': [128, 128, 128],       # Gray
}

# Aeroway types considered "airport operations" for preset
AIRPORT_OPS_TYPES = {'runway', 'taxiway', 'taxilane', 'gate', 'apron', 'helipad', 'jet_bridge', 'stopway'}


def get_infrastructure_color(layer_type: str) -> list:
    """Get RGB color for a given infrastructure layer type."""
    return INFRASTRUCTURE_COLORS.get(layer_type, INFRASTRUCTURE_COLORS['default'])


@st.cache_data(ttl=3600)
def get_available_infrastructure_types(_session, db_prefix: str):
    """
    Query distinct infrastructure types from PROPERTIES_INFRASTRUCTURE.
    Returns DataFrame with columns: layer_type, is_aeroway, object_count
    """
    q = f"""
    SELECT 
        COALESCE(osm_aeroway, class) AS layer_type,
        osm_aeroway IS NOT NULL AS is_aeroway,
        COUNT(*) AS object_count
    FROM {db_prefix}.PROPERTIES_INFRASTRUCTURE
    WHERE geometry IS NOT NULL
    GROUP BY 1, 2
    ORDER BY object_count DESC
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame(columns=['LAYER_TYPE', 'IS_AEROWAY', 'OBJECT_COUNT'])


@st.cache_data(ttl=3600)
def get_infrastructure_layers(_session, db_prefix: str, layer_types: list, include_tags: bool = False):
    """
    Fetch infrastructure geometries for selected layer types.
    Returns DataFrame with geometry and metadata for rendering.
    
    Args:
        _session: Snowflake session
        db_prefix: Database prefix (e.g., 'AIRPORT_YVR.V5')
        layer_types: List of layer types to fetch
        include_tags: If True, include source_tags_json column for tooltip display
    """
    if not layer_types:
        return pd.DataFrame()
    
    # Escape single quotes in layer types
    safe_types = [t.replace("'", "''") for t in layer_types]
    types_sql = ",".join([f"'{t}'" for t in safe_types])
    
    # Optionally include source_tags_json for detailed tooltips
    tags_col = ",\n        source_tags_json" if include_tags else ""
    
    q = f"""
    SELECT 
        infrastructure_id,
        COALESCE(osm_aeroway, class) AS layer_type,
        COALESCE(osm_ref, osm_name, primary_name) AS label,
        geometry_type,
        ST_ASGEOJSON(geometry) AS geom_json,
        ST_Y(ST_CENTROID(geometry)) AS center_lat,
        ST_X(ST_CENTROID(geometry)) AS center_lon{tags_col}
    FROM {db_prefix}.PROPERTIES_INFRASTRUCTURE
    WHERE COALESCE(osm_aeroway, class) IN ({types_sql})
      AND geometry IS NOT NULL
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


def render_infrastructure_selector(session, db_prefix: str, sidebar: bool = True, 
                                    default_preset: str = "none", key_prefix: str = "infra"):
    """
    Render infrastructure layer selector with presets and custom multiselect.
    
    Args:
        session: Snowflake session
        db_prefix: Database prefix (e.g., 'AIRPORT_YVR.V5')
        sidebar: If True, render in sidebar
        default_preset: One of 'none', 'airport_ops', 'all', 'custom'
        key_prefix: Prefix for widget keys to avoid conflicts between pages
    
    Returns:
        dict with keys:
            - 'layers': List of selected layer types (strings)
            - 'show_tags': Boolean indicating if tags should be displayed in tooltips
    """
    container = st.sidebar if sidebar else st
    
    # Get available types
    types_df = get_available_infrastructure_types(session, db_prefix)
    
    if types_df.empty:
        container.info("No infrastructure data available.")
        return {'layers': [], 'show_tags': False}
    
    # Separate aeroway (aviation) vs other types
    aeroway_types = types_df[types_df['IS_AEROWAY'] == True].copy()
    other_types = types_df[types_df['IS_AEROWAY'] == False].copy()
    
    # Build options with counts
    def format_option(row):
        return f"{row['LAYER_TYPE']} ({int(row['OBJECT_COUNT'])})"
    
    aeroway_options = {format_option(row): row['LAYER_TYPE'] for _, row in aeroway_types.iterrows()}
    other_options = {format_option(row): row['LAYER_TYPE'] for _, row in other_types.iterrows()}
    
    all_layer_types = set(types_df['LAYER_TYPE'].tolist())
    airport_ops_available = all_layer_types & AIRPORT_OPS_TYPES
    
    # Preset selector
    container.subheader("🗺️ Map Layers")
    
    preset_options = ["None", "Airport Ops", "All", "Custom"]
    preset_index = {"none": 0, "airport_ops": 1, "all": 2, "custom": 3}.get(default_preset, 0)
    
    preset = container.radio(
        "Quick select:",
        options=preset_options,
        index=preset_index,
        horizontal=True,
        key=f"{key_prefix}_preset"
    )
    
    selected_layers = []
    
    if preset == "None":
        # No layers selected
        selected_layers = []
    
    elif preset == "Airport Ops":
        # Auto-select airport operations types that exist
        selected_layers = list(airport_ops_available)
        if selected_layers:
            container.caption(f"Showing: {', '.join(sorted(selected_layers))}")
    
    elif preset == "All":
        # Select everything
        selected_layers = list(all_layer_types)
        container.caption(f"{len(selected_layers)} layer types selected")
    
    else:  # Custom
        # Show multiselect widgets
        selected_aeroway = []
        selected_other = []
        
        if aeroway_options:
            # Default to airport ops types for custom
            default_aeroway = [k for k, v in aeroway_options.items() if v in airport_ops_available]
            selected_aeroway_labels = container.multiselect(
                "Aviation Infrastructure",
                options=list(aeroway_options.keys()),
                default=default_aeroway,
                key=f"{key_prefix}_aeroway"
            )
            selected_aeroway = [aeroway_options[lbl] for lbl in selected_aeroway_labels]
        
        if other_options:
            selected_other_labels = container.multiselect(
                "Other Features",
                options=list(other_options.keys()),
                default=[],
                key=f"{key_prefix}_other"
            )
            selected_other = [other_options[lbl] for lbl in selected_other_labels]
        
        selected_layers = selected_aeroway + selected_other
    
    # Show Tags checkbox - displays OSM source tags in tooltips when hovering
    show_tags = True
    if selected_layers:
        show_tags = container.checkbox(
            "Show Tags", 
            value=True, 
            key=f"{key_prefix}_show_tags",
            help="Display OSM source tags as key-value pairs when hovering over objects"
        )
    
    return {'layers': selected_layers, 'show_tags': show_tags}


def create_infrastructure_pydeck_layers(infra_df, show_tags: bool = False) -> list:
    """
    Convert infrastructure DataFrame into pydeck layers.
    
    This is a componentized function that handles all geometry parsing and 
    pydeck layer creation for infrastructure data. Use this instead of 
    duplicating rendering code in each page.
    
    Args:
        infra_df: DataFrame from get_infrastructure_layers() with columns:
                  GEOM_JSON, GEOMETRY_TYPE, LAYER_TYPE, LABEL,
                  and optionally SOURCE_TAGS_JSON (if include_tags=True was used)
        show_tags: If True, include source_tags key-value pairs in tooltips
    
    Returns:
        List of pydeck.Layer objects (ScatterplotLayer, PathLayer, PolygonLayer)
    
    Example:
        infra_result = utils.render_infrastructure_selector(session, db_prefix, ...)
        infra_df = utils.get_infrastructure_layers(session, db_prefix, 
                                                   infra_result['layers'],
                                                   include_tags=infra_result['show_tags'])
        layers.extend(utils.create_infrastructure_pydeck_layers(infra_df, 
                                                                show_tags=infra_result['show_tags']))
    """
    import pydeck as pdk
    
    if infra_df is None or infra_df.empty:
        return []
    
    layers = []
    
    # Geometry parsing helpers
    def _parse_polygon(geom_json):
        """Parse polygon coordinates from GeoJSON."""
        try:
            g = json.loads(geom_json)
            if g['type'] == 'Polygon':
                return g['coordinates'][0]
            if g['type'] == 'MultiPolygon':
                return g['coordinates'][0][0]
        except Exception:
            return None
    
    def _parse_path(geom_json):
        """Parse path/line coordinates from GeoJSON."""
        try:
            g = json.loads(geom_json)
            if g['type'] == 'LineString':
                return g['coordinates']
            if g['type'] == 'MultiLineString':
                return g['coordinates'][0]
            # fallback: outline polygon as path
            poly = _parse_polygon(geom_json)
            return poly
        except Exception:
            return None
    
    def _parse_point(geom_json):
        """Parse point coordinates from GeoJSON."""
        try:
            g = json.loads(geom_json)
            if g['type'] == 'Point':
                return g['coordinates']
        except Exception:
            return None
    
    # Normalize column names (handle both upper and lower case from Snowflake)
    geom_col = 'GEOM_JSON' if 'GEOM_JSON' in infra_df.columns else 'geom_json'
    type_col = 'GEOMETRY_TYPE' if 'GEOMETRY_TYPE' in infra_df.columns else 'geometry_type'
    layer_col = 'LAYER_TYPE' if 'LAYER_TYPE' in infra_df.columns else 'layer_type'
    label_col = 'LABEL' if 'LABEL' in infra_df.columns else 'label'
    tags_col = 'SOURCE_TAGS_JSON' if 'SOURCE_TAGS_JSON' in infra_df.columns else 'source_tags_json'
    has_tags = tags_col in infra_df.columns
    
    # Helper to format source_tags as HTML key-value pairs
    def _format_tags(tags_json):
        """Format source_tags_json as HTML list of key-value pairs."""
        if pd.isna(tags_json) or not tags_json:
            return ""
        try:
            # tags_json is a dict with 'key_value' array
            if isinstance(tags_json, str):
                tags_json = json.loads(tags_json)
            kv_list = tags_json.get('key_value', [])
            if not kv_list:
                return ""
            # Format as HTML list (limit to 15 tags to avoid huge tooltips)
            lines = []
            for kv in kv_list[:15]:
                key = kv.get('key', '')
                val = kv.get('value', '')
                if key and val:
                    lines.append(f"<b>{key}:</b> {val}")
            if len(kv_list) > 15:
                lines.append(f"<i>...and {len(kv_list) - 15} more</i>")
            return "<br/>".join(lines)
        except Exception:
            return ""
    
    # Add color based on layer type
    infra_df = infra_df.copy()  # Avoid modifying original
    infra_df['color'] = infra_df[layer_col].apply(get_infrastructure_color)
    infra_df['color_with_alpha'] = infra_df['color'].apply(lambda c: c + [180])
    # Polygon fills should be more transparent to not obscure other layers
    infra_df['color_fill'] = infra_df['color'].apply(lambda c: c + [50])
    
    # Create tooltip - optionally include source tags
    def _build_tooltip(row):
        base = f"<b>{row[layer_col].title()}</b>"
        if pd.notna(row.get(label_col)):
            base += f"<br/>{row[label_col]}"
        
        # Add tags if enabled and available
        if show_tags and has_tags:
            tags_html = _format_tags(row.get(tags_col))
            if tags_html:
                base += f"<br/><hr style='margin:4px 0'/>{tags_html}"
        
        return base
    
    infra_df['TOOLTIP'] = infra_df.apply(_build_tooltip, axis=1)
    # Also add lowercase version for pages that use {tooltip} instead of {TOOLTIP}
    infra_df['tooltip'] = infra_df['TOOLTIP']
    
    # Layer order: Polygons first (bottom), then Lines, then Points (top)
    # This ensures polygons don't cover other objects when hovering
    
    # 1. Polygons (runways, aprons, terminals, buildings, etc.) - rendered first (bottom)
    poly_types = infra_df[infra_df[type_col].isin(['Polygon', 'MultiPolygon'])].copy()
    if not poly_types.empty:
        poly_types['coords'] = poly_types[geom_col].apply(_parse_polygon)
        poly_types = poly_types.dropna(subset=['coords'])
        
        # Large boundary types (aerodrome, etc.) should be outline-only to not obscure other layers
        boundary_types = {'aerodrome', 'airport', 'international_airport'}
        
        # Boundary polygons - outline only, no fill (rendered first, at very bottom)
        boundary_polys = poly_types[poly_types[layer_col].str.lower().isin(boundary_types)]
        if not boundary_polys.empty:
            layers.append(pdk.Layer(
                'PolygonLayer',
                data=boundary_polys,
                get_polygon='coords',
                filled=False,
                extruded=False,
                get_line_color=[100, 100, 100, 150],  # Gray outline
                line_width_min_pixels=2,
                pickable=True,
                auto_highlight=True,
            ))
        
        # Regular polygons - transparent fill
        regular_polys = poly_types[~poly_types[layer_col].str.lower().isin(boundary_types)]
        if not regular_polys.empty:
            layers.append(pdk.Layer(
                'PolygonLayer',
                data=regular_polys,
                get_polygon='coords',
                filled=True,
                extruded=False,
                get_fill_color='color_fill',  # More transparent fill
                get_line_color='color',
                line_width_min_pixels=1,
                pickable=True,
                auto_highlight=True,
            ))
    
    # 2. Lines (taxiways as lines, jet bridges, roads, etc.) - middle layer
    line_types = infra_df[infra_df[type_col].isin(['LineString', 'MultiLineString'])].copy()
    if not line_types.empty:
        line_types['path'] = line_types[geom_col].apply(_parse_path)
        line_types = line_types.dropna(subset=['path'])
        if not line_types.empty:
            layers.append(pdk.Layer(
                'PathLayer',
                data=line_types,
                get_path='path',
                get_color='color_with_alpha',
                width_scale=1,
                width_min_pixels=2,
                pickable=True,
            ))
    
    # 3. Points (gates, helipads, street lamps, etc.) - rendered last (top)
    point_types = infra_df[infra_df[type_col] == 'Point'].copy()
    if not point_types.empty:
        point_types['coords'] = point_types[geom_col].apply(_parse_point)
        point_types = point_types.dropna(subset=['coords'])
        if not point_types.empty:
            point_types['lon'] = point_types['coords'].apply(lambda c: c[0])
            point_types['lat'] = point_types['coords'].apply(lambda c: c[1])
            layers.append(pdk.Layer(
                'ScatterplotLayer',
                data=point_types,
                get_position='[lon, lat]',
                get_color='color_with_alpha',
                get_radius=15,
                radius_min_pixels=3,
                radius_max_pixels=8,
                pickable=True,
                auto_highlight=True,
                highlight_color=[255, 159, 54, 255],
            ))
    
    return layers

