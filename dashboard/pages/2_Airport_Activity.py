"""
Airport Activity Page - Geographic traffic analysis and airport-centric views
Visualize traffic density, airport activity, and geographic patterns
"""

import streamlit as st
import pandas as pd
import pydeck as pdk
from snowflake.snowpark.context import get_active_session
from datetime import datetime, timedelta
import sys
import json
sys.path.append('..')
import utils

# Page configuration
st.set_page_config(
    page_title="Airport Activity",
    page_icon="🗺️",
    layout="wide"
)

utils.apply_custom_css()

# Get session
session = get_active_session()

# Get the selected database
db = utils.get_selected_database()
schema = utils.get_schema()
db_prefix = f"{db}.{schema}"

# Header
with st.sidebar:
    selected_db = utils.render_airport_selector(sidebar=True)
if not selected_db:
    st.warning("No V5 airport databases found yet. Run the installer first.")
    st.stop()
st.title("🗺️ Airport Activity & Geographic Analysis")
st.markdown("Visualize air traffic density, airport zones, and geographic patterns")

# Sidebar controls
sample_size = None
with st.sidebar:
    
    # Get date range
    min_date, max_date = utils.get_date_range(session)
    selected_start_date, selected_end_date, _period = utils.render_time_period_filter(
        min_date,
        max_date,
        key_prefix="airport_activity",
        default_period="Last 7 Days",
    )
    
    col1, col2 = st.columns(2)
    with col1:
        time_start = st.time_input(
            "Start Time",
            value=datetime.strptime("00:00", "%H:%M").time(),
            help="Start of time window"
        )
    with col2:
        time_end = st.time_input(
            "End Time",
            value=datetime.strptime("23:59", "%H:%M").time(),
            help="End of time window"
        )
    
    st.divider()
    
    # Visualization Type
    viz_type = st.radio(
        "Select Visualization",
        ["Heatmap", "Hexagon"],
        index=1,  # Default to Hexagon
        help="Choose how to visualize traffic density"
    )
    
    # Metric Type
    metric_type = st.radio(
        "What to Measure",
        ["Distinct Flights", "Time Spent"],
        help="Distinct Flights: Count unique aircraft | Time Spent: Number of datapoints (proxy for time in location)"
    )
    
    st.divider()
    
    # Map controls
    if viz_type == "Heatmap":
        heatmap_intensity = st.slider(
            "Colour Intensity",
            min_value=1,
            max_value=10,
            value=5,
            help="Adjust the colour intensity of the heatmap"
        )
    elif viz_type == "Hexagon":
        h3_resolution = st.selectbox(
            "H3 Resolution",
            options=[12, 13, 14, 15],
            index=1,
            help="Higher resolution for detailed airport activity analysis. 12 = larger hexagons, 15 = smaller hexagons"
        )
        hex_elevation = st.checkbox("Show 3D Elevation", value=True)
        default_hex_pct = st.session_state.get('hex_sample_pct', 10)
        hex_sample_pct = st.slider(
            "Sample % of points (pre-aggregation)",
            min_value=1,
            max_value=50,
            value=int(default_hex_pct),
            help="Randomly sample points before H3 aggregation to keep payload size reasonable"
        )
        st.session_state['hex_sample_pct'] = int(hex_sample_pct)
        st.session_state.setdefault('hex_max_cells', 4000)
    
    # Infrastructure Layers - use new dynamic selector
    infra_selection = utils.render_infrastructure_selector(
        session, db_prefix, 
        sidebar=True, 
        default_preset="all",
        key_prefix="airport_activity"
    )
    selected_infra_layers = infra_selection['layers']
    show_infra_tags = infra_selection['show_tags']
    
    show_all_flights = st.checkbox("Show All Flights", value=False, help="Render all flight paths in the selected time window")
    
    # Control random sample size for 'Show All Flights'
    if show_all_flights:
        bbox = utils.get_airport_bbox(session)
        min_lat = float(bbox["min_lat"]); max_lat = float(bbox["max_lat"])
        min_lon = float(bbox["min_lon"]); max_lon = float(bbox["max_lon"])

        @st.cache_data(ttl=300)
        def get_flights_count(_session, start_dt, end_dt, _min_lat, _max_lat, _min_lon, _max_lon):
            q = f"""
            SELECT COUNT(DISTINCT FLIGHT) AS cnt
            FROM {db_prefix}.ADSB_DATA_LOCAL
            WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
                AND LOCATION IS NOT NULL AND FLIGHT IS NOT NULL
                AND ST_Y(LOCATION) BETWEEN {_min_lat} AND {_max_lat}
                AND ST_X(LOCATION) BETWEEN {_min_lon} AND {_max_lon}
            """
            df = _session.sql(q).to_pandas()
            return int(df.iloc[0]['CNT']) if not df.empty else 0
        # Compute current window and bounds
        _start_dt = f"{selected_start_date} {time_start.strftime('%H:%M:%S')}"
        _end_dt = f"{selected_end_date} {time_end.strftime('%H:%M:%S')}"
        total_flights = get_flights_count(session, _start_dt, _end_dt, min_lat, max_lat, min_lon, max_lon)
        min_sample = 100 if total_flights >= 100 else max(1, total_flights)
        default_sample = 200 if total_flights >= 200 else max(min_sample, total_flights)
        default_sample = int(st.session_state.get('all_flights_sample_size', default_sample))
        max_sample = max(1, total_flights)
        sample_size = st.number_input(
            "Random sample size (flights)",
            min_value=int(min_sample),
            max_value=int(max_sample),
            value=int(default_sample),
            step=10,
            help="Number of flights randomly sampled to display paths"
        )
        st.session_state['all_flights_sample_size'] = int(sample_size)

# Altitude filter removed; analyze all data
alt_min, alt_max = 0, 100000

# Query function
@st.cache_data(ttl=300)
def get_geographic_data(_session, start_dt, end_dt, _min_lat, _max_lat, _min_lon, _max_lon):
    """
    Get all geographic data for heatmap visualization
    Returns all datapoints for the selected time interval - no limits
    """
    query = f"""
    SELECT 
        ST_Y(LOCATION) AS LAT,
        ST_X(LOCATION) AS LON,
        FLIGHT,
        REGISTRATION,
        ICAO_HEX,
        ALTITUDE_BARO,
        VELOCITY
    FROM {db_prefix}.ADSB_DATA_LOCAL
    WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
        AND LOCATION IS NOT NULL
        AND FLIGHT IS NOT NULL
        AND ST_Y(LOCATION) BETWEEN {_min_lat} AND {_max_lat}
        AND ST_X(LOCATION) BETWEEN {_min_lon} AND {_max_lon}
    """
    return _session.sql(query).to_pandas()


@st.cache_data(ttl=300)
def get_time_spent_heatmap_bins(_session, start_dt, end_dt, _min_lat, _max_lat, _min_lon, _max_lon):
    """
    Aggregate points for Time Spent heatmap to reduce payload size.
    Bins coordinates to ~100-150m using ROUND to 3 decimals and computes COUNT(*) as WEIGHT.
    """
    query = f"""
    SELECT 
        ROUND(ST_Y(LOCATION), 3) AS lat_bin,
        ROUND(ST_X(LOCATION), 3) AS lon_bin,
        COUNT(*) AS weight
    FROM {db_prefix}.ADSB_DATA_LOCAL
    WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
        AND LOCATION IS NOT NULL
        AND FLIGHT IS NOT NULL
        AND ST_Y(LOCATION) BETWEEN {_min_lat} AND {_max_lat}
        AND ST_X(LOCATION) BETWEEN {_min_lon} AND {_max_lon}
    GROUP BY lat_bin, lon_bin
    """
    return _session.sql(query).to_pandas()

@st.cache_data(ttl=300)
def get_time_spent_sample_points(_session, start_dt, end_dt, sample_percent: int = 10, _min_lat=None, _max_lat=None, _min_lon=None, _max_lon=None):
    """
    Return a random sample of points for the Time Spent heatmap to keep payload small
    and maintain a visually correct distribution of activity.
    """
    # Clamp percent to sane bounds
    pct = max(1, min(int(sample_percent), 50))
    query = f"""
    SELECT 
        ST_Y(LOCATION) AS LAT,
        ST_X(LOCATION) AS LON
    FROM {db_prefix}.ADSB_DATA_LOCAL SAMPLE BERNOULLI ({pct})
    WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
        AND LOCATION IS NOT NULL
        AND FLIGHT IS NOT NULL
        AND ST_Y(LOCATION) BETWEEN {_min_lat} AND {_max_lat}
        AND ST_X(LOCATION) BETWEEN {_min_lon} AND {_max_lon}
    """
    return _session.sql(query).to_pandas()

@st.cache_data(ttl=300)
def get_all_flight_points(_session, start_dt, end_dt, max_flights=200):
    """
    Return decimated points for a random subset of flights for point cloud rendering.
    Limits: up to max_flights; ~100 points per flight (keeps endpoints).
    """
    q = f"""
    WITH bbox AS (
        SELECT {utils.get_airport_bbox(session)["min_lat"]}::FLOAT AS min_lat,
               {utils.get_airport_bbox(session)["max_lat"]}::FLOAT AS max_lat,
               {utils.get_airport_bbox(session)["min_lon"]}::FLOAT AS min_lon,
               {utils.get_airport_bbox(session)["max_lon"]}::FLOAT AS max_lon
    ),
    base AS (
        SELECT 
            FLIGHT,
            TIMESTAMP,
            ST_X(LOCATION) AS LON,
            ST_Y(LOCATION) AS LAT,
            ALTITUDE_BARO,
            ROW_NUMBER() OVER (PARTITION BY FLIGHT ORDER BY TIMESTAMP) AS rn,
            COUNT(*) OVER (PARTITION BY FLIGHT) AS n
        FROM {db_prefix}.ADSB_DATA_LOCAL
        CROSS JOIN bbox b
        WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
            AND LOCATION IS NOT NULL AND FLIGHT IS NOT NULL
            AND ST_Y(LOCATION) BETWEEN b.min_lat AND b.max_lat
            AND ST_X(LOCATION) BETWEEN b.min_lon AND b.max_lon
    ),
    flights_pick AS (
        SELECT FLIGHT
        FROM (
            SELECT DISTINCT FLIGHT FROM base
            ORDER BY RANDOM()
            LIMIT {int(max_flights)}
        )
    ),
    decimated AS (
        SELECT b.FLIGHT, b.LAT, b.LON, b.ALTITUDE_BARO
        FROM base b
        JOIN flights_pick f ON f.FLIGHT = b.FLIGHT
        WHERE MOD(rn, GREATEST(1, CEIL(n/100))) = 1 OR rn = 1 OR rn = n
    )
    SELECT FLIGHT, LAT, LON, ALTITUDE_BARO
    FROM decimated
    """
    return _session.sql(q).to_pandas()

@st.cache_data(ttl=300)
def get_schedule_for_flights(_session, date, flight_numbers):
    """
    Fetch origin, destination, and seats for a set of flight numbers on the given date.
    flight_numbers should be a list of numeric strings (e.g., '1234').
    """
    if not flight_numbers:
        return pd.DataFrame(columns=['FLIGHT_NUMBER','ORIGIN_AIRPORT','DESTINATION_AIRPORT','SEATS'])
    # Deduplicate and build IN clause
    nums = sorted({str(n) for n in flight_numbers if str(n)})
    in_clause = ",".join([f"'{n}'" for n in nums])
    query = f"""
    SELECT 
        FLIGHT_NUMBER,
        DEPARTURE_AIRPORT AS ORIGIN_AIRPORT,
        ARRIVAL_AIRPORT AS DESTINATION_AIRPORT,
        NULL::FLOAT AS SEATS
    FROM {db_prefix}.FLIGHT_SCHEDULE
    WHERE FLIGHT_DATE = '{date}'::DATE
      AND FLIGHT_NUMBER IN ({in_clause})
    """
    try:
        return _session.sql(query).to_pandas()
    except Exception:
        return pd.DataFrame(columns=['FLIGHT_NUMBER','ORIGIN_AIRPORT','DESTINATION_AIRPORT','SEATS'])

@st.cache_data(ttl=300)
def get_h3_hexagon_data(_session, start_dt, end_dt, h3_resolution, metric_type, sample_percent: int = 10, max_cells: int = 4000):
    """
    Get all traffic data aggregated by H3 hexagons using Snowflake's native H3 functions
    Returns H3 cell strings with either distinct flight counts or datapoint counts
    Analyzes all datapoints for the given time interval - no limits
    metric_type: 'Distinct Flights' or 'Time Spent'
    """
    # Choose aggregation based on metric type
    if metric_type == "Distinct Flights":
        count_expr = "COUNT(DISTINCT FLIGHT) as metric_value"
    else:  # Time Spent
        count_expr = "COUNT(*) as metric_value"
    
    bbox = utils.get_airport_bbox(session)
    query = f"""
    WITH bbox AS (
        SELECT {float(bbox["min_lat"])}::FLOAT AS min_lat,
               {float(bbox["max_lat"])}::FLOAT AS max_lat,
               {float(bbox["min_lon"])}::FLOAT AS min_lon,
               {float(bbox["max_lon"])}::FLOAT AS max_lon
    ),
    points_with_h3 AS (
        SELECT 
            ST_Y(LOCATION) AS LAT,
            ST_X(LOCATION) AS LON,
            FLIGHT,
            LOCATION as point_geom,
            H3_POINT_TO_CELL_STRING(LOCATION, {h3_resolution}) as h3_cell
        FROM {db_prefix}.ADSB_DATA_LOCAL SAMPLE BERNOULLI ({int(sample_percent)})
        CROSS JOIN bbox b
        WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
            AND LOCATION IS NOT NULL
            AND FLIGHT IS NOT NULL
            AND ST_Y(LOCATION) BETWEEN b.min_lat AND b.max_lat
            AND ST_X(LOCATION) BETWEEN b.min_lon AND b.max_lon
    ),
    h3_with_bounds AS (
        SELECT 
            h3_cell,
            {count_expr},
            ST_COLLECT(point_geom) as collected_points
        FROM points_with_h3
        WHERE h3_cell IS NOT NULL
        GROUP BY h3_cell
    )
    SELECT 
        h3_cell,
        metric_value,
        ST_XMIN(collected_points) as min_lon,
        ST_XMAX(collected_points) as max_lon,
        ST_YMIN(collected_points) as min_lat,
        ST_YMAX(collected_points) as max_lat
    FROM h3_with_bounds
    ORDER BY metric_value DESC
    LIMIT {int(max_cells)}
    """
    return _session.sql(query).to_pandas()

# Density stats removed

# Load data
with st.spinner("Loading geographic data..."):
    start_datetime = f"{selected_start_date} {time_start.strftime('%H:%M:%S')}"
    end_datetime = f"{selected_end_date} {time_end.strftime('%H:%M:%S')}"
    bbox = utils.get_airport_bbox(session)
    min_lat = float(bbox["min_lat"]); max_lat = float(bbox["max_lat"])
    min_lon = float(bbox["min_lon"]); max_lon = float(bbox["max_lon"])
    
    # Load different data based on visualization type
    if viz_type == "Hexagon":
        # Use H3 aggregated data for hexagon visualization - all datapoints
        h3_data = get_h3_hexagon_data(
            session,
            start_datetime,
            end_datetime,
            h3_resolution,
            metric_type,
            sample_percent=int(hex_sample_pct),
            max_cells=int(st.session_state.get('hex_max_cells', 4000))
        )
        geo_data = None  # Not needed for H3 viz
    else:
        # Load heatmap data with sampling controls to avoid oversized payload
        if metric_type == "Time Spent":
            # Dynamically choose a Bernoulli sample percent to cap rows ~200k
            # Start at 10%; if over threshold, reduce percent; if under, keep
            start_pct = int(st.session_state.get('heatmap_sample_pct', 10))
            for pct in (start_pct, 5, 3, 2, 1):
                geo_data = get_time_spent_sample_points(
                    session,
                    start_datetime,
                    end_datetime,
                    sample_percent=pct,
                    _min_lat=min_lat, _max_lat=max_lat, _min_lon=min_lon, _max_lon=max_lon
                )
                if geo_data is None or geo_data.empty or len(geo_data) <= 200_000:
                    break
            st.session_state['heatmap_sample_pct'] = pct
        else:
            # For Distinct Flights, fetch only the first point per flight to shrink payload
            df_all = get_geographic_data(
                session,
                start_datetime,
                end_datetime,
                min_lat, max_lat, min_lon, max_lon
            )
            if df_all is not None and not df_all.empty and 'FLIGHT' in df_all.columns:
                geo_data = df_all.drop_duplicates(subset=['FLIGHT'], keep='first')
                # Cap to 100k rows to avoid payload limits
                if len(geo_data) > 100_000:
                    geo_data = geo_data.sample(n=100_000, random_state=42)
            else:
                geo_data = df_all
        h3_data = None


# Geographic Coverage Statistics removed

# Main map visualization
has_data = (geo_data is not None and not geo_data.empty) or (h3_data is not None and not h3_data.empty)

if has_data:
    # Determine metric label
    metric_label = "Distinct Flights" if metric_type == "Distinct Flights" else "Data Points (Time Spent)"
    
    if viz_type == "Hexagon" and h3_data is not None:
        # No title for Hexagon view
        pass
    elif viz_type == "Heatmap" and geo_data is not None:
        # No title or caption for Heatmap view
        pass
    
    # Create layers based on visualization type
    layers = []
    
    # Infrastructure layers (componentized rendering)
    infra_df = utils.get_infrastructure_layers(session, db_prefix, selected_infra_layers, include_tags=show_infra_tags) if selected_infra_layers else pd.DataFrame()
    layers.extend(utils.create_infrastructure_pydeck_layers(infra_df, show_tags=show_infra_tags))
    
    if viz_type == "Heatmap":
        # For heatmap, handle data based on metric type
        if metric_type == "Distinct Flights":
            heatmap_data = geo_data.drop_duplicates(subset=['FLIGHT'], keep='first') if (geo_data is not None and not geo_data.empty and 'FLIGHT' in geo_data.columns) else geo_data
        else:
            # Use sample points
            heatmap_data = geo_data
        
        # Altitude-like gradient (low -> high): #97E7EF to #D966FF
        color_range = [
            [151, 231, 239, 0],
            [151, 231, 239, 64],
            [167, 205, 244, 128],
            [186, 174, 247, 160],
            [205, 140, 251, 200],
            [217, 102, 255, 255]
        ]
        
        layer = pdk.Layer(
            'HeatmapLayer',
            data=heatmap_data,
            get_position='[LON, LAT]',
            get_weight=1,
            intensity=heatmap_intensity,
            radiusPixels=30,
            opacity=0.9,
            threshold=0.05,
            colorRange=color_range
        )
        layers.append(layer)
        
        # Default map bounds to airport polygon
        map_bounds = utils.get_airport_default_view(session)
        pitch = 0
        
    elif viz_type == "Hexagon":
        # Use PyDeck's H3HexagonLayer with H3 string cell IDs
        # Altitude-like gradient color based on METRIC_VALUE
        max_count = h3_data['METRIC_VALUE'].max()
        max_count = max_count if pd.notna(max_count) and max_count > 0 else 1
        
        # elevation for 3D if enabled
        h3_data['elevation'] = (h3_data['METRIC_VALUE'] / max_count * 500) if hex_elevation else 0
        
        low_rgb = (151, 231, 239)
        high_rgb = (217, 102, 255)
        
        def to_color(val):
            t = float(val) / float(max_count)
            t = 0.0 if pd.isna(t) else max(0.0, min(1.0, t))
            r = int(low_rgb[0] + t * (high_rgb[0] - low_rgb[0]))
            g = int(low_rgb[1] + t * (high_rgb[1] - low_rgb[1]))
            b = int(low_rgb[2] + t * (high_rgb[2] - low_rgb[2]))
            return [r, g, b, 220]
        
        h3_data['color'] = h3_data['METRIC_VALUE'].apply(to_color)
        
        # Add tooltip column for consistent tooltip handling across all layers
        h3_data['tooltip'] = h3_data['METRIC_VALUE'].apply(lambda v: f"<b>{metric_label}:</b> {int(v) if pd.notna(v) else 0}")
        
        # Use H3HexagonLayer which works directly with H3 string cell IDs
        layer = pdk.Layer(
            'H3HexagonLayer',
            data=h3_data,
            get_hexagon='H3_CELL',
            get_fill_color='color',
            get_elevation='elevation',
            elevation_scale=1,
            extruded=hex_elevation,
            pickable=True,
            auto_highlight=True,
            get_line_color=[255, 255, 255, 100],
            line_width_min_pixels=1
        )
        layers.append(layer)
        
        # Default map bounds to airport polygon
        map_bounds = utils.get_airport_default_view(session)
        pitch = 50
    
    # Optional: All flights layer as altitude-colored points
    if show_all_flights:
        pts_df = get_all_flight_points(
            session, start_datetime, end_datetime, 
            max_flights=int(sample_size) if sample_size else 200
        )
        if not pts_df.empty:
            # Prepare altitude-colored points
            def to_float_or_none(v):
                try:
                    s = str(v)
                    if s is None or s.strip() == '':
                        return None
                    return float(v)
                except Exception:
                    return None
            pts_df['ALT'] = pts_df['ALTITUDE_BARO'].apply(to_float_or_none)
            alt_series = pts_df['ALT'].dropna()
            min_alt = float(alt_series.min()) if not alt_series.empty else 0.0
            max_alt = float(alt_series.max()) if not alt_series.empty else 1.0
            low_rgb = (151, 231, 239)
            high_rgb = (217, 102, 255)
            def interp_color(val):
                if val is None:
                    t = 0.0
                else:
                    t = (val - min_alt) / (max_alt - min_alt) if max_alt > min_alt else 0.0
                    t = max(0.0, min(1.0, t))
                r = int(low_rgb[0] + t * (high_rgb[0] - low_rgb[0]))
                g = int(low_rgb[1] + t * (high_rgb[1] - low_rgb[1]))
                b = int(low_rgb[2] + t * (high_rgb[2] - low_rgb[2]))
                return [r, g, b, 220]
            pts_df['color'] = pts_df['ALT'].apply(interp_color)
            # Build a single scatter layer with small points
            layers.append(pdk.Layer(
                'ScatterplotLayer',
                data=pts_df,
                get_position='[LON, LAT]',
                get_color='color',
                get_radius=8,
                radius_min_pixels=1,
                radius_max_pixels=3,
                pickable=False
            ))
    
    # View state: always use airport polygon bounds
    airport_bounds = utils.get_airport_default_view(session)
    view_state = pdk.ViewState(
        latitude=airport_bounds['latitude'],
        longitude=airport_bounds['longitude'],
        zoom=airport_bounds['zoom'],
        pitch=pitch,
        bearing=0
    )
    
    # Create deck with unified tooltip using {tooltip} column (all layers populate this)
    deck_tooltip = {"html": "{tooltip}", "style": {"backgroundColor": "steelblue", "color": "white"}}
    r = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_style='light',
        tooltip=deck_tooltip
    )
    
    try:
        st.pydeck_chart(r, use_container_width=True, height=700)
    except Exception as e:
        if 'MessageSizeError' in str(e):
            # Reduce sampling by 20% and rerun
            changed = False
            if viz_type == "Hexagon":
                curr = int(st.session_state.get('hex_sample_pct', hex_sample_pct))
                new_val = max(1, int(curr * 0.8))
                if new_val != curr:
                    st.session_state['hex_sample_pct'] = new_val
                    changed = True
                # Also reduce max cells
                curr_cells = int(st.session_state.get('hex_max_cells', 4000))
                new_cells = max(500, int(curr_cells * 0.8))
                if new_cells != curr_cells:
                    st.session_state['hex_max_cells'] = new_cells
                    changed = True
            if show_all_flights:
                curr_n = int(st.session_state.get('all_flights_sample_size', sample_size or 200))
                new_n = max(1, int(curr_n * 0.8))
                if new_n != curr_n:
                    st.session_state['all_flights_sample_size'] = new_n
                    changed = True
            if viz_type == "Heatmap" and metric_type == "Time Spent":
                curr_hp = int(st.session_state.get('heatmap_sample_pct', 10))
                new_hp = max(1, int(curr_hp * 0.8))
                if new_hp != curr_hp:
                    st.session_state['heatmap_sample_pct'] = new_hp
                    changed = True
            if changed:
                st.experimental_rerun()
            else:
                st.error("MessageSizeError: Reduce filters or zoom to load less data.")
        else:
            raise

    # H3 analysis section removed

else:
    st.warning("⚠️ No data available for the selected filters. Try adjusting the date or time range.")

st.divider()

