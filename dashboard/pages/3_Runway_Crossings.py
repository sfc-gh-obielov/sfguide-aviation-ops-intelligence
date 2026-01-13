import streamlit as st
import pandas as pd
from snowflake.snowpark.context import get_active_session
import pydeck as pdk
from datetime import datetime, timedelta
import utils

st.set_page_config(page_title="Runway Crossings", page_icon="🛤️", layout="wide")
utils.apply_custom_css()
session = get_active_session()

# Get the selected database
db = utils.get_selected_database()
schema = utils.get_schema()
db_prefix = f"{db}.{schema}"

st.title("🛤️ Runway Crossings Analysis")
st.caption("Detects aircraft crossing the runway while taxiing on the ground (S→N or N→S). Filters out takeoffs and landings using: max speed ≤45 kts, time on runway ≤120 sec, and straight-line distance ≤220m.")

table_fqn = f"{db_prefix}.RUNWAY_CROSSINGS_DETAILED"
min_date, max_date = utils.get_table_date_bounds(session, table_fqn, "t_entry")

with st.sidebar:
    selected_db = utils.render_airport_selector(sidebar=True)

with st.sidebar:
    if not selected_db:
        st.warning("No V5 airport databases found yet. Run the installer first.")
        st.stop()

    st.header("Filters")
    
    st.subheader("Direction")
    dir_south_north = st.checkbox("South → North", value=True, key="dir_sn")
    dir_north_south = st.checkbox("North → South", value=True, key="dir_ns")
    
    st.divider()
    st.subheader("Metric")
    metric_type = st.radio(
        "Display metric:",
        options=["flight_count", "total_duration"],
        format_func=lambda x: "Flight Count" if x == "flight_count" else "Total Duration (min)",
        index=0
    )
    hide_unknown_airlines = st.checkbox("Hide Unknown (UNK)", value=False)
    st.divider()
    start_d, end_d, _period = utils.render_time_period_filter(
        min_date,
        max_date,
        key_prefix="runway_crossings",
        default_period="Last 7 Days",
    )
    
    st.divider()
    st.subheader("Map Layers")
    show_flights = st.checkbox("Show Flights", value=False, help="Display trajectories of flights that performed crossings")
    if show_flights:
        max_flights = st.slider(
            "Max flights to display",
            min_value=10,
            max_value=500,
            value=100,
            step=10,
            help="Limit number of flight paths to prevent overload"
        )
        sample_points = st.slider(
            "Points per flight (%)",
            min_value=10,
            max_value=100,
            value=30,
            step=10,
            help="Percentage of points to show per flight (reduces detail but improves performance)"
        )
    else:
        max_flights = 100
        sample_points = 30
    
    # Infrastructure Layers - use new dynamic selector
    infra_selection = utils.render_infrastructure_selector(
        session, db_prefix, 
        sidebar=True, 
        default_preset="all",
        key_prefix="runway_crossings"
    )
    selected_infra_layers = infra_selection['layers']
    show_infra_tags = infra_selection['show_tags']

# Build direction filter
directions = []
if dir_south_north:
    directions.append('S→N')
if dir_north_south:
    directions.append('N→S')

if not directions:
    st.warning("⚠️ Please select at least one direction filter.")
    st.stop()

@st.cache_data(ttl=600)
def get_crossing_summary(_session, start_d, end_d, dirs):
    """Get overall crossing counts and stats"""
    dir_filter = "','".join(dirs)
    q = f"""
    SELECT
      COUNT(DISTINCT flight_key) AS total_flights,
      COUNT(*) AS total_crossings,
      AVG(duration_s) AS avg_duration_s,
      SUM(duration_s)/60.0 AS total_duration_min
    FROM {db_prefix}.RUNWAY_CROSSINGS_DETAILED
    WHERE DATE(t_entry) BETWEEN '{start_d}'::DATE AND '{end_d}'::DATE
      AND direction IN ('{dir_filter}')
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_crossing_aggregates(_session, start_d, end_d, dirs, metric):
    """
    Aggregate crossings by H3 cell for heatmap visualization.
    metric: 'flight_count' or 'total_duration'
    """
    dir_filter = "','".join(dirs)
    
    if metric == 'flight_count':
        agg_expr = 'COUNT(DISTINCT flight_key) AS metric_value'
        metric_label = 'flights'
    else:
        agg_expr = 'SUM(duration_s)/60.0 AS metric_value'
        metric_label = 'minutes'
    
    q = f"""
    WITH base AS (
      SELECT
        flight_key,
        t_entry,
        duration_s,
        direction,
        midpoint_geom,
        H3_POINT_TO_CELL_STRING(midpoint_geom, 12) AS h3_cell
      FROM {db_prefix}.RUNWAY_CROSSINGS_DETAILED
      WHERE DATE(t_entry) BETWEEN '{start_d}'::DATE AND '{end_d}'::DATE
        AND direction IN ('{dir_filter}')
    )
    SELECT
      h3_cell,
      {agg_expr},
      ANY_VALUE(ST_Y(midpoint_geom)) AS lat,
      ANY_VALUE(ST_X(midpoint_geom)) AS lon
    FROM base
    GROUP BY h3_cell
    HAVING metric_value > 0
    ORDER BY metric_value DESC
    """
    try:
        df = _session.sql(q).to_pandas()
        if df is not None and not df.empty:
            df['METRIC_LABEL'] = metric_label
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_crossing_analytics(_session, start_d, end_d, dirs):
    """Get ALL crossing events with flight/airline data for analytics (no limit)"""
    dir_filter = "','".join(dirs)
    q = f"""
    WITH crossings AS (
      SELECT
        c.flight_key,
        c.t_entry,
        c.direction,
        c.duration_s
      FROM {db_prefix}.RUNWAY_CROSSINGS_DETAILED c
      WHERE DATE(c.t_entry) BETWEEN '{start_d}'::DATE AND '{end_d}'::DATE
        AND c.direction IN ('{dir_filter}')
    )
    SELECT 
      c.*,
      a.FLIGHT AS flight_number,
      SUBSTR(a.FLIGHT, 1, 3) AS airline_code
    FROM crossings c
    LEFT JOIN {db_prefix}.ADSB_DATA_LOCAL a
      ON a.FLIGHT_KEY = c.flight_key
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.flight_key ORDER BY a.TIMESTAMP) = 1
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_crossing_details(_session, start_d, end_d, dirs, limit=100):
    """Get recent crossing events for table display (limited)"""
    dir_filter = "','".join(dirs)
    q = f"""
    WITH crossings AS (
      SELECT
        c.flight_key,
        c.t_entry,
        c.t_exit,
        c.direction,
        c.duration_s,
        ROUND(c.duration_s/60.0, 2) AS duration_min,
        ROUND(c.max_speed_kts, 1) AS max_speed_kts,
        ROUND(c.chord_m, 1) AS chord_m,
        ST_Y(c.midpoint_geom) AS lat,
        ST_X(c.midpoint_geom) AS lon
      FROM {db_prefix}.RUNWAY_CROSSINGS_DETAILED c
      WHERE DATE(c.t_entry) BETWEEN '{start_d}'::DATE AND '{end_d}'::DATE
        AND c.direction IN ('{dir_filter}')
      ORDER BY c.t_entry DESC
      LIMIT {limit}
    )
    SELECT 
      c.*,
      a.FLIGHT AS flight_number,
      SUBSTR(a.FLIGHT, 1, 3) AS airline_code
    FROM crossings c
    LEFT JOIN {db_prefix}.ADSB_DATA_LOCAL a
      ON a.FLIGHT_KEY = c.flight_key
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.flight_key ORDER BY a.TIMESTAMP) = 1
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def get_runway_geometry(_session):
    """Fetch runway polygon for base layer"""
    q = f"""
    SELECT ST_ASGEOJSON(runway_geog) AS geom_json
    FROM {db_prefix}.PROPERTIES_RUNWAYS
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()

# Infrastructure layers now use utils.get_infrastructure_layers()

@st.cache_data(ttl=600)
def get_crossing_flight_paths(_session, start_d, end_d, dirs, sample_pct=10):
    """Get flight trajectories for aircraft that performed crossings with schedule info"""
    dir_filter = "','".join(dirs)
    q = f"""
    WITH crossing_flights AS (
      -- Pick a single representative crossing direction per flight/day
      SELECT flight_key, crossing_date, direction
      FROM (
        SELECT
          flight_key,
          DATE(t_entry) AS crossing_date,
          direction,
          COUNT(*) AS crossings,
          ROW_NUMBER() OVER (
            PARTITION BY flight_key, DATE(t_entry)
            ORDER BY COUNT(*) DESC, direction ASC
          ) AS rn
        FROM {db_prefix}.RUNWAY_CROSSINGS_DETAILED
        WHERE DATE(t_entry) BETWEEN '{start_d}'::DATE AND '{end_d}'::DATE
          AND direction IN ('{dir_filter}')
        GROUP BY 1,2,3
      )
      WHERE rn = 1
    ), flight_paths AS (
      SELECT
        a.FLIGHT,
        a.FLIGHT_KEY,
        ST_Y(a.LOCATION) AS LAT,
        ST_X(a.LOCATION) AS LON,
        a.TIMESTAMP,
        a.ALTITUDE_BARO,
        cf.crossing_date,
        cf.direction
      FROM {db_prefix}.ADSB_DATA_LOCAL a
      INNER JOIN crossing_flights cf ON cf.flight_key = a.FLIGHT_KEY
      WHERE a.LOCATION IS NOT NULL
    )
    SELECT
      fp.*,
      s.DEPARTURE_AIRPORT AS ORIGIN_AIRPORT,
      s.ARRIVAL_AIRPORT AS DESTINATION_AIRPORT
    FROM flight_paths fp
    LEFT JOIN {db_prefix}.FLIGHT_SCHEDULE s
      ON s.FLIGHT_DATE = fp.crossing_date
     AND TO_VARCHAR(s.FLIGHT_NUMBER) = REGEXP_SUBSTR(fp.FLIGHT, '[0-9]+')
    ORDER BY fp.FLIGHT_KEY, fp.TIMESTAMP
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()

# Fetch data
with st.spinner("Loading crossing data..."):
    summary_df = get_crossing_summary(session, start_d, end_d, directions)
    agg_df = get_crossing_aggregates(session, start_d, end_d, directions, metric_type)
    analytics_df = get_crossing_analytics(session, start_d, end_d, directions)  # For charts (all data)
    details_df = get_crossing_details(session, start_d, end_d, directions)  # For table (limited to 100)
    runway_df = get_runway_geometry(session)
    # Always fetch flight paths data for charts, but only render paths on map if checkbox is on
    flight_paths_df = get_crossing_flight_paths(session, start_d, end_d, directions)
    # Load infrastructure layers based on selection
    infra_df = utils.get_infrastructure_layers(session, db_prefix, selected_infra_layers, include_tags=show_infra_tags) if selected_infra_layers else pd.DataFrame()

# Summary KPIs
if summary_df.empty or summary_df.iloc[0]['TOTAL_CROSSINGS'] == 0:
    st.info("No crossing events detected for the selected filters.")
    st.stop()

summary = summary_df.iloc[0]
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total Crossings", f"{int(summary['TOTAL_CROSSINGS']):,}")
with col2:
    st.metric("Unique Flights", f"{int(summary['TOTAL_FLIGHTS']):,}")
with col3:
    st.metric("Avg Duration", f"{summary['AVG_DURATION_S']:.1f} sec")
with col4:
    st.metric("Total Duration", f"{summary['TOTAL_DURATION_MIN']:.1f} min")

st.divider()

# Map visualization
st.subheader("📍 Crossing Density Heatmap")

if agg_df.empty:
    st.info("No aggregated data available for map visualization.")
else:
    # Determine color scale based on metric
    max_val = agg_df['METRIC_VALUE'].max()
    min_val = agg_df['METRIC_VALUE'].min()
    
    # Prepare tooltip
    metric_label = agg_df['METRIC_LABEL'].iloc[0]
    agg_df['tooltip'] = agg_df.apply(
        lambda r: f"Cell: {r['H3_CELL']}<br>Value: {r['METRIC_VALUE']:.1f} {metric_label}", 
        axis=1
    )
    
    # Base map center
    center_lat = agg_df['LAT'].mean()
    center_lon = agg_df['LON'].mean()
    
    # Layers
    layers = []
    
    # Helper functions for geometry parsing
    import json
    def parse_polygon(geom_json):
        try:
            g = json.loads(geom_json)
            if g['type'] == 'Polygon':
                return g['coordinates'][0]
            elif g['type'] == 'MultiPolygon':
                return g['coordinates'][0][0]
        except:
            return None
    
    def parse_linestring(geom_json):
        try:
            g = json.loads(geom_json)
            if g['type'] == 'LineString':
                return g['coordinates']
            elif g['type'] == 'MultiLineString':
                return g['coordinates'][0]
        except:
            return None
    
    # Infrastructure layers (componentized rendering)
    layers.extend(utils.create_infrastructure_pydeck_layers(infra_df, show_tags=show_infra_tags))
    
    # Runway base layer from V5.PROPERTIES_RUNWAYS
    if not runway_df.empty and runway_df.iloc[0]['GEOM_JSON']:
        geom_json = json.loads(runway_df.iloc[0]['GEOM_JSON'])
        layers.append(
            pdk.Layer(
                'GeoJsonLayer',
                data={'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'geometry': geom_json}]},
                get_fill_color=[80, 80, 80, 100],
                get_line_color=[200, 200, 200, 200],
                line_width_min_pixels=2,
                pickable=False
            )
        )
    
    # H3 Hexagon heatmap layer
    # Normalize elevation to max height of 500 to prevent overly tall hexagons
    max_elevation = 500
    agg_df = agg_df.copy()
    agg_df['elevation'] = (agg_df['METRIC_VALUE'] / max_val * max_elevation).clip(upper=max_elevation)
    
    layers.append(
        pdk.Layer(
            'H3HexagonLayer',
            agg_df,
            get_hexagon='H3_CELL',
            get_fill_color=f'[255, (1 - METRIC_VALUE / {max_val}) * 255, 0, 180]',
            get_line_color=[255, 255, 255, 50],
            line_width_min_pixels=1,
            pickable=True,
            auto_highlight=True,
            extruded=True,
            elevation_scale=1,
            get_elevation='elevation'
        )
    )
    
    # Optional flight path layer with per-segment altitude coloring
    if show_flights and not flight_paths_df.empty:
        # Get airline name mapping
        code_to_name = utils.get_airline_name_map(session, start_d, end_d)
        
        # Limit number of flights to display
        unique_flights = flight_paths_df['FLIGHT_KEY'].unique()
        if len(unique_flights) > max_flights:
            # Randomly sample flights
            import numpy as np
            sampled_keys = np.random.choice(unique_flights, size=max_flights, replace=False)
            flight_paths_sampled = flight_paths_df[flight_paths_df['FLIGHT_KEY'].isin(sampled_keys)]
        else:
            flight_paths_sampled = flight_paths_df
        
        # Get gate assignments for these flights.
        # Callsigns are often missing historically; use flight_key from ADSB and map to most-likely gate via GATE_ANALYSIS_ADSB_GROUND_POINTS.
        sampled_keys_list = flight_paths_sampled['FLIGHT_KEY'].unique().tolist()[:1000]  # Limit for query
        gate_map = {}
        if sampled_keys_list:
            keys_clause = "','".join([str(k) for k in sampled_keys_list])
            gate_q = f"""
            WITH per_gate AS (
              SELECT flight_key, closest_gate_name AS gate_name, SUM(lag_seconds) AS dwell_s
              FROM {db_prefix}.GATE_ANALYSIS_ADSB_GROUND_POINTS
              WHERE closest_gate_name IS NOT NULL
                AND flight_key IN ('{keys_clause}')
              GROUP BY 1,2
            )
            SELECT flight_key, gate_name
            FROM per_gate
            QUALIFY ROW_NUMBER() OVER (PARTITION BY flight_key ORDER BY dwell_s DESC) = 1
            """
            try:
                gate_map_df = session.sql(gate_q).to_pandas()
                gate_map = {str(r['FLIGHT_KEY']): str(r['GATE_NAME']) for _, r in gate_map_df.iterrows()} if gate_map_df is not None and not gate_map_df.empty else {}
            except Exception:
                gate_map = {}
        
        # Group by flight and create altitude-colored segments
        flights_grouped = flight_paths_sampled.groupby('FLIGHT_KEY')
        
        # Altitude gradient colors (same as Flight Tracker)
        low_rgb = (151, 231, 239)  # Cyan - low altitude
        high_rgb = (217, 102, 255)  # Purple - high altitude
        
        def interp_color(alt, min_alt, max_alt):
            """Interpolate color based on altitude"""
            if pd.isna(alt) or min_alt == max_alt:
                t = 0.0
            else:
                t = (alt - min_alt) / (max_alt - min_alt)
                t = max(0.0, min(1.0, t))
            r = int(low_rgb[0] + t * (high_rgb[0] - low_rgb[0]))
            g = int(low_rgb[1] + t * (high_rgb[1] - low_rgb[1]))
            b = int(low_rgb[2] + t * (high_rgb[2] - low_rgb[2]))
            return [r, g, b, 200]
        
        segments = []
        for flight_key, group in flights_grouped:
            group = group.sort_values('TIMESTAMP')
            
            # Subsample points per flight to reduce data
            if len(group) > 2:
                # Keep first, last, and sample of middle points
                sample_size = max(2, int(len(group) * sample_points / 100))
                step = max(1, len(group) // sample_size)
                indices = list(range(0, len(group), step))
                if indices[-1] != len(group) - 1:
                    indices.append(len(group) - 1)  # Ensure last point included
                group = group.iloc[indices]
            
            # Get altitude range for this flight
            alts = group['ALTITUDE_BARO'].dropna()
            if not alts.empty:
                min_alt = alts.min()
                max_alt = alts.max()
            else:
                min_alt = max_alt = 0
            
            # Create tooltip once for this flight (reuse for all segments)
            flight_num = group.iloc[0]['FLIGHT'] if 'FLIGHT' in group.columns else flight_key[:8]
            airline_code = flight_num[:3] if len(flight_num) >= 3 else 'N/A'
            airline_name = code_to_name.get(airline_code, airline_code)
            first_ts = group.iloc[0]['TIMESTAMP'] if 'TIMESTAMP' in group.columns else 'N/A'
            origin = group.iloc[0]['ORIGIN_AIRPORT'] if 'ORIGIN_AIRPORT' in group.columns and pd.notna(group.iloc[0]['ORIGIN_AIRPORT']) else 'N/A'
            destination = group.iloc[0]['DESTINATION_AIRPORT'] if 'DESTINATION_AIRPORT' in group.columns and pd.notna(group.iloc[0]['DESTINATION_AIRPORT']) else 'N/A'
            gate_name = gate_map.get(str(flight_key), 'N/A')
            tooltip = f"<b>Airline:</b> {airline_name}<br><b>Flight:</b> {flight_num}<br><b>Route:</b> {origin} → {destination}<br><b>Gate:</b> {gate_name}<br><b>Time:</b> {first_ts}"
            
            # Create colored segments between consecutive points
            for i in range(len(group) - 1):
                lat1 = group.iloc[i]['LAT']
                lon1 = group.iloc[i]['LON']
                lat2 = group.iloc[i + 1]['LAT']
                lon2 = group.iloc[i + 1]['LON']
                alt = group.iloc[i]['ALTITUDE_BARO']
                
                if pd.notna(lat1) and pd.notna(lon1) and pd.notna(lat2) and pd.notna(lon2):
                    color = interp_color(alt, min_alt, max_alt)
                    
                    segments.append({
                        'path': [[lon1, lat1], [lon2, lat2]],
                        'color': color,
                        'tooltip': tooltip  # Same tooltip for all segments of this flight
                    })
        
        if segments:
            segments_df = pd.DataFrame(segments)
            layers.append(pdk.Layer(
                'PathLayer',
                data=segments_df,
                get_path='path',
                get_color='color',
                width_scale=3,
                width_min_pixels=2,
                pickable=True,
                auto_highlight=True
            ))
    
    # Render map
    view_state = pdk.ViewState(
        latitude=center_lat,
        longitude=center_lon,
        zoom=14,
        pitch=45,
        bearing=0
    )
    
    # Tooltip configuration - use lowercase 'tooltip' for pydeck compatibility
    r = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip={"html": "{tooltip}", "style": {"backgroundColor": "steelblue", "color": "white"}},
        map_style='light'
    )
    
    st.pydeck_chart(r, use_container_width=True)
    
    st.caption(f"💡 Hexagon color and height represent {metric_label}. Zoom and tilt for 3D view.")

st.divider()

# Analytics visualizations
if not analytics_df.empty:
    # Heatmap: Day of Week x Hour of Day
    st.subheader("📅 Crossing Heatmap (Day of Week × Hour)")
    
    heat_df = analytics_df.copy()
    heat_df['dow'] = pd.to_datetime(heat_df['T_ENTRY']).dt.dayofweek
    heat_df['hour'] = pd.to_datetime(heat_df['T_ENTRY']).dt.hour
    
    # Pivot to create heatmap
    pivot = heat_df.pivot_table(index='dow', columns='hour', aggfunc='size', fill_value=0)
    
    # Reorder days: Mon-Sun
    dow_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    pivot = pivot.reindex([0, 1, 2, 3, 4, 5, 6])
    pivot.index = dow_names
    
    import plotly.graph_objects as go
    fig_heat = go.Figure(data=go.Heatmap(
        z=pivot.values,
        x=[f"{h:02d}:00" for h in pivot.columns],
        y=pivot.index.tolist(),
        colorscale='Blues',
        hovertemplate='%{y}<br>Hour: %{x}<br>Crossings: %{z}<extra></extra>'
    ))
    fig_heat.update_layout(
        xaxis_title='Hour of Day',
        yaxis_title='Day of Week',
        height=400,
        template='plotly_white'
    )
    st.plotly_chart(fig_heat, use_container_width=True)
    
    st.divider()
    
    # Direction breakdown: S→N vs N→S
    st.subheader("🧭 Crossings by Direction")
    if 'DIRECTION' in analytics_df.columns and 'DURATION_S' in analytics_df.columns:
        dir_agg = analytics_df.groupby('DIRECTION').agg({
            'FLIGHT_KEY': 'count',
            'DURATION_S': ['sum', 'mean']
        }).reset_index()
        dir_agg.columns = ['DIRECTION', 'crossing_count', 'total_duration_s', 'avg_duration_s']
        dir_agg['total_duration_min'] = dir_agg['total_duration_s'] / 60.0
        dir_agg['avg_duration_s'] = dir_agg['avg_duration_s'].round(1)
        
        col_d1, col_d2, col_d3 = st.columns(3)
        with col_d1:
            st.markdown("**By Flight Count**")
            fig_d1 = go.Figure(go.Bar(
                x=dir_agg['DIRECTION'],
                y=dir_agg['crossing_count'],
                marker_color=['#4FC3F7', '#66BB6A'],
                text=dir_agg['crossing_count'],
                textposition='outside'
            ))
            fig_d1.update_layout(
                xaxis_title='Direction',
                yaxis_title='Number of Crossings',
                height=300,
                template='plotly_white',
                showlegend=False
            )
            st.plotly_chart(fig_d1, use_container_width=True)
        
        with col_d2:
            st.markdown("**By Total Time (min)**")
            fig_d2 = go.Figure(go.Bar(
                x=dir_agg['DIRECTION'],
                y=dir_agg['total_duration_min'],
                marker_color=['#FF9800', '#9C27B0'],
                text=dir_agg['total_duration_min'].round(2),
                textposition='outside'
            ))
            fig_d2.update_layout(
                xaxis_title='Direction',
                yaxis_title='Total Duration (min)',
                height=300,
                template='plotly_white',
                showlegend=False
            )
            st.plotly_chart(fig_d2, use_container_width=True)
        
        with col_d3:
            st.markdown("**By Avg Time Per Crossing (sec)**")
            fig_d3 = go.Figure(go.Bar(
                x=dir_agg['DIRECTION'],
                y=dir_agg['avg_duration_s'],
                marker_color=['#E91E63', '#00BCD4'],
                text=dir_agg['avg_duration_s'],
                textposition='outside'
            ))
            fig_d3.update_layout(
                xaxis_title='Direction',
                yaxis_title='Avg Duration (sec)',
                height=300,
                template='plotly_white',
                showlegend=False
            )
            st.plotly_chart(fig_d3, use_container_width=True)
    
    st.divider()
    
    # Arrival vs Departure breakdown
    st.subheader("🛬🛫 Crossings by Flight Type (Arrival vs Departure)")
    if not flight_paths_df.empty and 'DIRECTION' in flight_paths_df.columns and 'FLIGHT_KEY' in flight_paths_df.columns and 'DURATION_S' in analytics_df.columns:
        # Get unique flight info from paths (which has DIRECTION from schedule)
        flight_types = flight_paths_df[['FLIGHT_KEY', 'DIRECTION']].drop_duplicates()
        # Merge with analytics to get duration info - keep all crossings
        type_df = analytics_df.merge(flight_types, on='FLIGHT_KEY', how='left', suffixes=('_crossing', '_flight'))
        # Fill unknown with 'Unknown'
        type_df['DIRECTION_flight'] = type_df['DIRECTION_flight'].fillna('Unknown')
        
        if not type_df.empty:
            type_agg = type_df.groupby('DIRECTION_flight').agg({
                'FLIGHT_KEY': 'count',
                'DURATION_S': 'sum'
            }).reset_index()
            type_agg.columns = ['flight_type', 'crossing_count', 'total_duration_s']
            type_agg['total_duration_min'] = type_agg['total_duration_s'] / 60.0
            
            # Sort to show Arrival, Departure, Unknown
            order = {'Arrival': 0, 'Departure': 1, 'Unknown': 2}
            type_agg['sort_order'] = type_agg['flight_type'].map(order).fillna(3)
            type_agg = type_agg.sort_values('sort_order').drop('sort_order', axis=1)
            
            col_t1, col_t2 = st.columns(2)
            with col_t1:
                st.markdown("**By Crossing Count**")
                # Dynamic colors based on categories present
                colors = []
                for ft in type_agg['flight_type']:
                    if ft == 'Arrival':
                        colors.append('#00BCD4')
                    elif ft == 'Departure':
                        colors.append('#FF5722')
                    else:
                        colors.append('#9E9E9E')  # Gray for Unknown
                
                fig_t1 = go.Figure(go.Bar(
                    x=type_agg['flight_type'],
                    y=type_agg['crossing_count'],
                    marker_color=colors,
                    text=type_agg['crossing_count'],
                    textposition='outside'
                ))
                fig_t1.update_layout(
                    xaxis_title='Flight Type',
                    yaxis_title='Number of Crossings',
                    height=300,
                    template='plotly_white',
                    showlegend=False
                )
                st.plotly_chart(fig_t1, use_container_width=True)
            
            with col_t2:
                st.markdown("**By Total Time (min)**")
                # Dynamic colors for duration chart
                colors_dur = []
                for ft in type_agg['flight_type']:
                    if ft == 'Arrival':
                        colors_dur.append('#3F51B5')
                    elif ft == 'Departure':
                        colors_dur.append('#E91E63')
                    else:
                        colors_dur.append('#757575')  # Dark gray for Unknown
                
                fig_t2 = go.Figure(go.Bar(
                    x=type_agg['flight_type'],
                    y=type_agg['total_duration_min'],
                    marker_color=colors_dur,
                    text=type_agg['total_duration_min'].round(2),
                    textposition='outside'
                ))
                fig_t2.update_layout(
                    xaxis_title='Flight Type',
                    yaxis_title='Total Duration (min)',
                    height=300,
                    template='plotly_white',
                    showlegend=False
                )
                st.plotly_chart(fig_t2, use_container_width=True)
        else:
            st.info("Flight type data not available")
    else:
        st.info("No flight type data available for the selected period")
    
    st.divider()
    
    # Gates used by crossing flights
    st.subheader("🚪 Gates by Crossing Flights")
    if not flight_paths_df.empty and 'DIRECTION' in flight_paths_df.columns:
        # Merge gate assignments with flight type from paths
        gate_types = flight_paths_df[['FLIGHT_KEY', 'DIRECTION']].drop_duplicates()
        
        # Get gate assignments for crossing flights from analytics
        crossing_keys = analytics_df['FLIGHT_KEY'].unique().tolist()
        if len(crossing_keys) > 0:
            # Query gate assignments
            keys_clause = "','".join([str(k) for k in crossing_keys[:1000]])
            gate_q = f"""
            SELECT flight_key, gate_name
            FROM {db_prefix}.GATE_ANALYSIS_FLIGHT_GATE_TIME
            WHERE flight_key IN ('{keys_clause}')
            """
            
            try:
                gate_df = session.sql(gate_q).to_pandas()
                if not gate_df.empty:
                    # Merge with flight types
                    gate_with_type = gate_df.merge(gate_types, on='FLIGHT_KEY', how='left')
                    gate_with_type = gate_with_type[gate_with_type['DIRECTION'].notna()]
                    
                    if not gate_with_type.empty and 'GATE_NAME' in gate_with_type.columns:
                        # Aggregate by gate and flight type
                        gate_agg = gate_with_type.groupby(['GATE_NAME', 'DIRECTION']).size().reset_index(name='count')
                        gate_pivot = gate_agg.pivot(index='GATE_NAME', columns='DIRECTION', values='count').fillna(0)
                        
                        # Sort by total
                        gate_pivot['total'] = gate_pivot.sum(axis=1)
                        gate_pivot = gate_pivot.sort_values('total', ascending=True).drop('total', axis=1).tail(15)
                        
                        fig_gate = go.Figure()
                        if 'Arrival' in gate_pivot.columns:
                            fig_gate.add_trace(go.Bar(
                                x=gate_pivot['Arrival'],
                                y=gate_pivot.index,
                                name='Arrival',
                                orientation='h',
                                marker_color='#00BCD4'
                            ))
                        if 'Departure' in gate_pivot.columns:
                            fig_gate.add_trace(go.Bar(
                                x=gate_pivot['Departure'],
                                y=gate_pivot.index,
                                name='Departure',
                                orientation='h',
                                marker_color='#FF5722'
                            ))
                        
                        fig_gate.update_layout(
                            barmode='stack',
                            xaxis_title='Number of Crossings',
                            yaxis_title='Gate',
                            height=450,
                            template='plotly_white',
                            margin=dict(l=100, r=20, t=20, b=40),
                            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
                        )
                        st.plotly_chart(fig_gate, use_container_width=True)
                        st.caption("Top 15 gates by total crossings. Blue=Arrivals, Orange=Departures (stacked)")
                    else:
                        st.info("No gate assignment data available")
                else:
                    st.info("No gate data found")
            except Exception as e:
                st.info(f"Gate data not available: {str(e)}")
        else:
            st.info("No crossings or gate data available")
    else:
        st.info("No flight/gate data available for the selected period")
    
    st.divider()
    
    # Top airlines by crossings (count & duration)
    st.subheader("✈️ Top Airlines")
    if 'AIRLINE_CODE' in analytics_df.columns and 'DURATION_S' in analytics_df.columns:
        airline_df = analytics_df[analytics_df['AIRLINE_CODE'].notna()].copy()
        if hide_unknown_airlines:
            airline_df = airline_df[~airline_df['AIRLINE_CODE'].astype(str).isin(['UNK', 'N/A', 'NA', ''])]
        airline_agg = airline_df.groupby('AIRLINE_CODE').agg({
            'FLIGHT_KEY': 'count',
            'DURATION_S': 'sum'
        }).reset_index()
        airline_agg.columns = ['AIRLINE_CODE', 'crossing_count', 'total_duration_s']
        airline_agg['total_duration_min'] = airline_agg['total_duration_s'] / 60.0
        airline_agg = airline_agg.sort_values('crossing_count', ascending=False).head(10)
        
        # Map codes to names
        code_to_name = utils.get_airline_name_map(session, start_d, end_d)
        airline_agg['airline_name'] = airline_agg['AIRLINE_CODE'].apply(
            lambda c: code_to_name.get(str(c), str(c))
        )
        
        col_a1, col_a2 = st.columns(2)
        with col_a1:
            st.markdown("**By Crossing Count**")
            # Sort by crossing count descending
            airline_count_sorted = airline_agg.sort_values('crossing_count', ascending=True)  # ascending for horizontal bar (bottom to top)
            fig_a1 = go.Figure(go.Bar(
                x=airline_count_sorted['crossing_count'],
                y=airline_count_sorted['airline_name'],
                orientation='h',
                marker_color='#4FC3F7'
            ))
            fig_a1.update_layout(
                xaxis_title='Crossings',
                yaxis_title='',
                height=350,
                template='plotly_white',
                margin=dict(l=100, r=10, t=10, b=40)
            )
            st.plotly_chart(fig_a1, use_container_width=True)
        
        with col_a2:
            st.markdown("**By Total Time (min)**")
            # Sort by duration descending
            airline_dur_sorted = airline_agg.sort_values('total_duration_min', ascending=True)
            fig_a2 = go.Figure(go.Bar(
                x=airline_dur_sorted['total_duration_min'],
                y=airline_dur_sorted['airline_name'],
                orientation='h',
                marker_color='#FF9800'
            ))
            fig_a2.update_layout(
                xaxis_title='Total Duration (min)',
                yaxis_title='',
                height=350,
                template='plotly_white',
                margin=dict(l=100, r=10, t=10, b=40)
            )
            st.plotly_chart(fig_a2, use_container_width=True)
    else:
        st.info("No airline data available")
    
    st.divider()
    
    # Top flights by crossings (count & duration)
    st.subheader("🔝 Top Flights")
    if 'FLIGHT_NUMBER' in analytics_df.columns and 'DURATION_S' in analytics_df.columns:
        flight_df = analytics_df[analytics_df['FLIGHT_NUMBER'].notna()].copy()
        flight_agg = flight_df.groupby('FLIGHT_NUMBER').agg({
            'FLIGHT_KEY': 'count',
            'DURATION_S': 'sum'
        }).reset_index()
        flight_agg.columns = ['FLIGHT_NUMBER', 'crossing_count', 'total_duration_s']
        flight_agg['total_duration_min'] = flight_agg['total_duration_s'] / 60.0
        flight_agg = flight_agg.sort_values('crossing_count', ascending=False).head(10)

        # Enrich per-flight labels (airline + O/D) from schedule (helps when ADSB enrichment is missing)
        try:
            hdr = utils.get_flight_headers_from_schedule(
                session,
                start_d,
                flight_agg['FLIGHT_NUMBER'].astype(str).tolist(),
                db_prefix=db_prefix
            )
        except Exception:
            hdr = pd.DataFrame()
        if hdr is not None and not hdr.empty:
            # normalize to expected casing
            if 'flight_id' in hdr.columns and 'FLIGHT_ID' not in hdr.columns:
                hdr = hdr.rename(columns={
                    'flight_id': 'FLIGHT_NUMBER',
                    'airline_name': 'AIRLINE_NAME',
                    'origin_airport': 'ORIGIN_AIRPORT',
                    'destination_airport': 'DESTINATION_AIRPORT',
                })
            flight_agg = flight_agg.merge(
                hdr[['FLIGHT_NUMBER', 'AIRLINE_NAME', 'ORIGIN_AIRPORT', 'DESTINATION_AIRPORT']],
                on='FLIGHT_NUMBER',
                how='left'
            )
            flight_agg['LABEL'] = flight_agg.apply(
                lambda r: f"{r['FLIGHT_NUMBER']} — {r.get('AIRLINE_NAME') or 'N/A'} — {(r.get('ORIGIN_AIRPORT') or 'N/A')}→{(r.get('DESTINATION_AIRPORT') or 'N/A')}",
                axis=1
            )
        else:
            flight_agg['LABEL'] = flight_agg['FLIGHT_NUMBER']
        
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            st.markdown("**By Crossing Count**")
            # Sort by crossing count descending (ascending for horizontal bar top-to-bottom)
            flight_count_sorted = flight_agg.sort_values('crossing_count', ascending=True)
            fig_f1 = go.Figure(go.Bar(
                x=flight_count_sorted['crossing_count'],
                y=flight_count_sorted['LABEL'],
                orientation='h',
                marker_color='#66BB6A'
            ))
            fig_f1.update_layout(
                xaxis_title='Crossings',
                yaxis_title='',
                height=350,
                template='plotly_white',
                margin=dict(l=100, r=10, t=10, b=40)
            )
            st.plotly_chart(fig_f1, use_container_width=True)
        
        with col_f2:
            st.markdown("**By Total Time (min)**")
            # Sort by duration descending
            flight_dur_sorted = flight_agg.sort_values('total_duration_min', ascending=True)
            fig_f2 = go.Figure(go.Bar(
                x=flight_dur_sorted['total_duration_min'],
                y=flight_dur_sorted['LABEL'],
                orientation='h',
                marker_color='#9C27B0'
            ))
            fig_f2.update_layout(
                xaxis_title='Total Duration (min)',
                yaxis_title='',
                height=350,
                template='plotly_white',
                margin=dict(l=100, r=10, t=10, b=40)
            )
            st.plotly_chart(fig_f2, use_container_width=True)
    else:
        st.info("No flight data available")

st.divider()

# Detailed table
st.subheader("📋 Recent Crossing Events")

if details_df.empty:
    st.info("No detailed events available.")
else:
    # Format the dataframe for display
    cols = ['FLIGHT_NUMBER', 'AIRLINE_CODE', 'T_ENTRY', 'T_EXIT', 'DIRECTION', 
            'DURATION_MIN', 'MAX_SPEED_KTS', 'CHORD_M']
    # Use only columns that exist
    display_cols = [c for c in cols if c in details_df.columns]
    display_df = details_df[display_cols].copy()
    
    # Rename columns for display
    col_rename = {
        'FLIGHT_NUMBER': 'Flight',
        'AIRLINE_CODE': 'Airline',
        'T_ENTRY': 'Entry Time',
        'T_EXIT': 'Exit Time',
        'DIRECTION': 'Direction',
        'DURATION_MIN': 'Duration (min)',
        'MAX_SPEED_KTS': 'Max Speed (kts)',
        'CHORD_M': 'Chord (m)'
    }
    display_df = display_df.rename(columns={k: v for k, v in col_rename.items() if k in display_df.columns})
    
    st.dataframe(display_df, use_container_width=True, height=400)
    st.caption(f"Showing most recent {len(details_df)} crossings (max 100)")
