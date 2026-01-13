"""
Operations Overview - Map-focused head-to-head (H2H) visualization
"""

import streamlit as st
import pandas as pd
import pydeck as pdk
from snowflake.snowpark.context import get_active_session
from datetime import datetime, timedelta
import json
import sys
import plotly.graph_objects as go
sys.path.append('..')
import utils

# Page configuration
st.set_page_config(
    page_title="Operations",
    page_icon="🧭",
    layout="wide"
)

utils.apply_custom_css()
session = get_active_session()

schema = utils.get_schema()

st.title("🧭 Operations Overview")

st.markdown(
    """
**Directional Pattern Detection Map (Below)**

This map shows **ADS-B position samples** from aircraft during time bins where opposite-direction flight patterns coexist:

**🔵 Blue Points**: Arrivals approaching from the west (heading eastbound, altitude ≤1,000 ft)  
**🔴 Red Points**: Departures climbing to the west (heading westbound, altitude ≤1,500 ft, speed ≥80 kts)  
**🟢 Green Points**: Arrivals from the east (heading westbound)  
**🟡 Amber Points**: Departures to the east (heading eastbound)

**West Pattern (W)**: Arrivals from west + Departures to west = both using west-facing approach/departure  
**East Pattern (E)**: Arrivals from east + Departures to east = both using east-facing approach/departure

The map identifies **time bins (default 5 min)** where both patterns exist simultaneously, indicating potential operational conflicts.

**Why it matters**: Head‑to‑head operations reduce throughput and increase delay risk before curfew.
    """
)

# Sidebar controls
with st.sidebar:
    selected_db = utils.render_airport_selector(sidebar=True)

if not selected_db:
    st.warning("No V5 airport databases found yet. Run the installer first.")
    st.stop()

db_prefix = f"{selected_db}.{schema}"

with st.sidebar:
    st.subheader("Filters")

    min_date, max_date = utils.get_date_range(session)
    # Defaults: last 3 days (clamped to available range if needed)
    default_end = max_date if max_date else datetime.now().date()
    default_start = default_end - timedelta(days=3)
    min_bound = min_date if min_date else (datetime.now().date() - timedelta(days=365))
    max_bound = max_date if max_date else datetime.now().date()
    if default_start < min_bound:
        default_start = min_bound
    if default_end > max_bound:
        default_end = max_bound
    if default_end < default_start:
        default_end = default_start

    date_range = st.date_input(
        "Date Range",
        value=(default_start, default_end),
        min_value=min_bound,
        max_value=max_bound,
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date = end_date = date_range

    bin_minutes = st.select_slider("Bin Size (minutes)", options=[1, 5, 10, 15], value=5)
    sample_pct = st.slider("Sample % (points)", min_value=1, max_value=50, value=10)
    show_hex = st.checkbox("Show Hexagon Layer", value=False)

    st.divider()
    st.subheader("H2H Conflict Filters")
    max_gap_tolerance = st.slider(
        "Max Time Gap (seconds)",
        min_value=0,
        max_value=120,
        value=60,
        step=5,
        help="Show conflicts with absolute time gap ≤ this value. Lower = more severe conflicts only.",
    )

# Helpers
@st.cache_data(ttl=300)
def get_airport_bounds(_session):
    bbox = utils.get_airport_bbox(_session)
    return float(bbox["center_lat"]), float(bbox["center_lon"]), 13

@st.cache_data(ttl=300)
def get_head_to_head_points(_session, start_dt: str, end_dt: str, bin_min: int, sample: int):
    """Return head-to-head points for both patterns in bins where both coexist.
    Patterns:
      - 'W' (West): arrivals eastbound (from west) + departures westbound (to west)
      - 'E' (East): arrivals westbound (from east) + departures eastbound (to east)
    """
    q = f"""
    WITH airport AS (
      SELECT geometry FROM {db_prefix}.PROPERTIES_AIRPORT LIMIT 1
    ), pts AS (
      SELECT 
        a.TIMESTAMP AS ts,
        a.FLIGHT,
        a.ALTITUDE_BARO,
        a.VELOCITY,
        a.TRACK,
        a.LOCATION,
        TIME_SLICE(TO_TIMESTAMP_NTZ(a.TIMESTAMP), {int(bin_min)}, 'MINUTE') AS bin_ts
      FROM {db_prefix}.ADSB_DATA_LOCAL a SAMPLE BERNOULLI ({int(sample)})
      CROSS JOIN airport ap
      WHERE a.TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
        AND a.LOCATION IS NOT NULL
        AND ST_DWITHIN(a.LOCATION, ap.geometry, 4000)
    ), arr_east AS (
      SELECT bin_ts, LOCATION
      FROM pts
      WHERE CAST(TRACK AS FLOAT) BETWEEN 45 AND 135
        AND CAST(ALTITUDE_BARO AS FLOAT) <= 1000
    ), dep_west AS (
      SELECT bin_ts, LOCATION
      FROM pts
      WHERE CAST(TRACK AS FLOAT) BETWEEN 225 AND 315
        AND CAST(ALTITUDE_BARO AS FLOAT) <= 1500
        AND CAST(VELOCITY AS FLOAT) >= 80
    ), arr_west AS (
      SELECT bin_ts, LOCATION
      FROM pts
      WHERE CAST(TRACK AS FLOAT) BETWEEN 225 AND 315
        AND CAST(ALTITUDE_BARO AS FLOAT) <= 1000
    ), dep_east AS (
      SELECT bin_ts, LOCATION
      FROM pts
      WHERE CAST(TRACK AS FLOAT) BETWEEN 45 AND 135
        AND CAST(ALTITUDE_BARO AS FLOAT) <= 1500
        AND CAST(VELOCITY AS FLOAT) >= 80
    ), h2h_w AS (  -- West pattern bins
      SELECT a.bin_ts
      FROM arr_east a JOIN dep_west d USING (bin_ts)
      GROUP BY a.bin_ts
    ), h2h_e AS (  -- East pattern bins
      SELECT a.bin_ts
      FROM arr_west a JOIN dep_east d USING (bin_ts)
      GROUP BY a.bin_ts
    )
    SELECT 'W' AS pattern, 'ARR' AS kind, ST_Y(a.LOCATION) AS lat, ST_X(a.LOCATION) AS lon, a.bin_ts
    FROM arr_east a JOIN h2h_w b ON a.bin_ts = b.bin_ts
    UNION ALL
    SELECT 'W' AS pattern, 'DEP' AS kind, ST_Y(d.LOCATION) AS lat, ST_X(d.LOCATION) AS lon, d.bin_ts
    FROM dep_west d JOIN h2h_w b ON d.bin_ts = b.bin_ts
    UNION ALL
    SELECT 'E' AS pattern, 'ARR' AS kind, ST_Y(a.LOCATION) AS lat, ST_X(a.LOCATION) AS lon, a.bin_ts
    FROM arr_west a JOIN h2h_e b ON a.bin_ts = b.bin_ts
    UNION ALL
    SELECT 'E' AS pattern, 'DEP' AS kind, ST_Y(d.LOCATION) AS lat, ST_X(d.LOCATION) AS lon, d.bin_ts
    FROM dep_east d JOIN h2h_e b ON d.bin_ts = b.bin_ts
    """
    return _session.sql(q).to_pandas()

@st.cache_data(ttl=300)
def get_h2h_bins(_session, start_dt: str, end_dt: str, bin_min: int):
    """Return head-to-head bins for both patterns with counts per bin."""
    q = f"""
    WITH airport AS (
      SELECT geometry FROM {db_prefix}.PROPERTIES_AIRPORT LIMIT 1
    ), pts AS (
      SELECT 
        TIME_SLICE(TO_TIMESTAMP_NTZ(a.TIMESTAMP), {int(bin_min)}, 'MINUTE') AS bin_ts,
        CAST(TRACK AS FLOAT) AS trk,
        CAST(ALTITUDE_BARO AS FLOAT) AS alt,
        CAST(VELOCITY AS FLOAT) AS vel,
        a.LOCATION
      FROM {db_prefix}.ADSB_DATA_LOCAL a
      CROSS JOIN airport ap
      WHERE a.TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
        AND a.LOCATION IS NOT NULL
        AND ST_DWITHIN(a.LOCATION, ap.geometry, 4000)
    ), arr_east AS (
      SELECT bin_ts FROM pts WHERE trk BETWEEN 45 AND 135 AND alt <= 1000
    ), dep_west AS (
      SELECT bin_ts FROM pts WHERE trk BETWEEN 225 AND 315 AND alt <= 1500 AND vel >= 80
    ), arr_west AS (
      SELECT bin_ts FROM pts WHERE trk BETWEEN 225 AND 315 AND alt <= 1000
    ), dep_east AS (
      SELECT bin_ts FROM pts WHERE trk BETWEEN 45 AND 135 AND alt <= 1500 AND vel >= 80
    ), dep_w_counts AS (
      SELECT bin_ts, COUNT(*) AS dep_count FROM dep_west GROUP BY bin_ts
    ), dep_e_counts AS (
      SELECT bin_ts, COUNT(*) AS dep_count FROM dep_east GROUP BY bin_ts
    ), w_bins AS (
      SELECT a.bin_ts, COUNT(*) AS arr_count, COALESCE(dw.dep_count,0) AS dep_count
      FROM arr_east a LEFT JOIN dep_w_counts dw ON dw.bin_ts = a.bin_ts
      GROUP BY a.bin_ts, dw.dep_count
      HAVING COUNT(*) > 0 AND COALESCE(dw.dep_count,0) > 0
    ), e_bins AS (
      SELECT a.bin_ts, COUNT(*) AS arr_count, COALESCE(de.dep_count,0) AS dep_count
      FROM arr_west a LEFT JOIN dep_e_counts de ON de.bin_ts = a.bin_ts
      GROUP BY a.bin_ts, de.dep_count
      HAVING COUNT(*) > 0 AND COALESCE(de.dep_count,0) > 0
    )
    SELECT 'W' AS pattern, bin_ts, arr_count AS arrivals, dep_count AS departures FROM w_bins
    UNION ALL
    SELECT 'E' AS pattern, bin_ts, arr_count AS arrivals, dep_count AS departures FROM e_bins
    ORDER BY pattern, bin_ts
    """
    return _session.sql(q).to_pandas()

@st.cache_data(ttl=300)
def get_hex_agg(_session, start_dt: str, end_dt: str, res: int, sample: int, max_cells: int = 3000):
    query = f"""
    WITH points AS (
      SELECT LOCATION
      FROM {db_prefix}.ADSB_DATA_LOCAL SAMPLE BERNOULLI ({int(sample)})
      WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
        AND LOCATION IS NOT NULL
    ), h3 AS (
      SELECT H3_POINT_TO_CELL_STRING(LOCATION, {int(res)}) AS h3_cell, LOCATION
      FROM points
    )
    SELECT h3_cell, COUNT(*) AS metric_value,
           ST_XMIN(ST_COLLECT(LOCATION)) AS min_lon,
           ST_XMAX(ST_COLLECT(LOCATION)) AS max_lon,
           ST_YMIN(ST_COLLECT(LOCATION)) AS min_lat,
           ST_YMAX(ST_COLLECT(LOCATION)) AS max_lat
    FROM h3
    WHERE h3_cell IS NOT NULL
    GROUP BY h3_cell
    ORDER BY metric_value DESC
    LIMIT {int(max_cells)}
    """
    return _session.sql(query).to_pandas()

# Build map layers
start_dt = f"{start_date} 00:00:00"
end_dt = f"{end_date} 23:59:59"

with st.spinner("Computing head-to-head layers..."):
    df = get_head_to_head_points(session, start_dt, end_dt, bin_minutes, sample_pct)
    bins_df = get_h2h_bins(session, start_dt, end_dt, bin_minutes)

layers = []

# Head-to-head arrivals/departures layers
if df is not None and not df.empty:
    # West pattern layers
    arr_w = df[(df['PATTERN'] == 'W') & (df['KIND'] == 'ARR')][['LAT','LON']].rename(columns={'LAT':'lat','LON':'lon'})
    dep_w = df[(df['PATTERN'] == 'W') & (df['KIND'] == 'DEP')][['LAT','LON']].rename(columns={'LAT':'lat','LON':'lon'})
    if not arr_w.empty:
        layers.append(pdk.Layer(
            'ScatterplotLayer',
            data=arr_w,
            get_position='[lon, lat]',
            get_fill_color=[66, 133, 244, 180],  # blue
            radius_min_pixels=2,
            radius_max_pixels=4,
            pickable=False
        ))
    if not dep_w.empty:
        layers.append(pdk.Layer(
            'ScatterplotLayer',
            data=dep_w,
            get_position='[lon, lat]',
            get_fill_color=[234, 67, 53, 180],   # red
            radius_min_pixels=2,
            radius_max_pixels=4,
            pickable=False
        ))
    # East pattern layers
    arr_e = df[(df['PATTERN'] == 'E') & (df['KIND'] == 'ARR')][['LAT','LON']].rename(columns={'LAT':'lat','LON':'lon'})
    dep_e = df[(df['PATTERN'] == 'E') & (df['KIND'] == 'DEP')][['LAT','LON']].rename(columns={'LAT':'lat','LON':'lon'})
    if not arr_e.empty:
        layers.append(pdk.Layer(
            'ScatterplotLayer',
            data=arr_e,
            get_position='[lon, lat]',
            get_fill_color=[52, 168, 83, 180],  # green
            radius_min_pixels=2,
            radius_max_pixels=4,
            pickable=False
        ))
    if not dep_e.empty:
        layers.append(pdk.Layer(
            'ScatterplotLayer',
            data=dep_e,
            get_position='[lon, lat]',
            get_fill_color=[251, 188, 5, 180],  # amber
            radius_min_pixels=2,
            radius_max_pixels=4,
            pickable=False
        ))

# Optional hexagon aggregate of all points in range
if show_hex:
    with st.spinner("Loading hexagon aggregate..."):
        h3 = get_hex_agg(session, start_dt, end_dt, res=13, sample=sample_pct, max_cells=2000)
    if h3 is not None and not h3.empty:
        # Color by intensity
        max_val = float(h3['METRIC_VALUE'].max()) if 'METRIC_VALUE' in h3 else 1.0
        def to_color(v):
            t = max(0.0, min(1.0, float(v)/max_val))
            return [int(151 + (217-151)*t), int(231 + (102-231)*t), int(239 + (255-239)*t), 220]
        h3['color'] = h3['METRIC_VALUE'].apply(to_color)
        layers.append(pdk.Layer(
            'H3HexagonLayer',
            data=h3,
            get_hexagon='H3_CELL',
            get_fill_color='color',
            pickable=True,
            auto_highlight=True,
            line_width_min_pixels=1
        ))

# Map view
lat, lon, zoom = get_airport_bounds(session)
view_state = pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom, pitch=0, bearing=0)

r = pdk.Deck(layers=layers, initial_view_state=view_state, map_style='light')
st.pydeck_chart(r, use_container_width=True, height=650)

# KPIs and supporting charts
st.divider()
col1, col2, col3 = st.columns(3)
if bins_df is not None and not bins_df.empty:
    # Total H2H minutes = number of bins * bin_minutes
    total_bins = len(bins_df)
    total_minutes = total_bins * bin_minutes
    # Split by pattern
    by_pat = bins_df.groupby('PATTERN', as_index=False)['BIN_TS'].count().rename(columns={'BIN_TS':'bins'})
    mins_w = int(by_pat[by_pat['PATTERN']=='W']['bins'].sum() * bin_minutes)
    mins_e = int(by_pat[by_pat['PATTERN']=='E']['bins'].sum() * bin_minutes)
    # Longest continuous span (approximate): difference between min/max consecutive bins when deltas == bin length
    bd = pd.to_datetime(bins_df['BIN_TS']) if 'BIN_TS' in bins_df.columns else pd.to_datetime(bins_df.iloc[:,0])
    bd = bd.sort_values().reset_index(drop=True)
    max_span = 0
    if len(bd) > 1:
        run = bin_minutes
        for i in range(1, len(bd)):
            delta = (bd[i] - bd[i-1]).total_seconds() / 60.0
            if abs(delta - bin_minutes) < 1e-6:
                run += bin_minutes
                max_span = max(max_span, run)
            else:
                run = bin_minutes
        max_span = max(max_span, run)
    col1.metric("H2H Minutes (total)", f"{int(total_minutes):,}")
    col2.metric("H2H Minutes W/E", f"W {mins_w:,} | E {mins_e:,}")
    col3.metric("Longest Span (min)", f"{int(max_span):,}")

    # Hour-of-day prevalence
    st.subheader("⏱️ Head‑to‑Head by Hour of Day")
    tmp = bins_df.copy()
    tmp['hour'] = pd.to_datetime(tmp['BIN_TS']).dt.hour
    tmp['minutes'] = bin_minutes
    hod_w = tmp[tmp['PATTERN']=='W'].groupby('hour', as_index=False)['minutes'].sum().rename(columns={'minutes':'min_w'})
    hod_e = tmp[tmp['PATTERN']=='E'].groupby('hour', as_index=False)['minutes'].sum().rename(columns={'minutes':'min_e'})
    hod_all = pd.merge(hod_w, hod_e, on='hour', how='outer').fillna(0)
    fig_hod = go.Figure()
    fig_hod.add_trace(go.Bar(x=hod_all['hour'], y=hod_all['min_w'], name='West pattern', marker_color='#4FC3F7'))
    fig_hod.add_trace(go.Bar(x=hod_all['hour'], y=hod_all['min_e'], name='East pattern', marker_color='#66BB6A'))
    fig_hod.update_layout(barmode='group')
    fig_hod.update_layout(xaxis_title='Hour', yaxis_title='Minutes', height=350, template='plotly_white')
    st.plotly_chart(fig_hod, use_container_width=True)

    # DOW × Hour heatmap
    st.subheader("📆 Head‑to‑Head Heatmap (Day of Week × Hour)")
    dow = bins_df.copy()
    dow['dow'] = pd.to_datetime(dow['BIN_TS']).dt.dayofweek
    dow['hour'] = pd.to_datetime(dow['BIN_TS']).dt.hour
    dow['minutes'] = bin_minutes
    # Map to names with Sunday last
    names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
    dow['dow_name'] = dow['dow'].apply(lambda x: names[int(x)%7])
    # Total heatmap
    pivot = dow.pivot_table(index='dow_name', columns='hour', values='minutes', aggfunc='sum', fill_value=0)
    # reorder rows
    pivot = pivot.reindex(names)
    fig_hm = go.Figure(data=go.Heatmap(z=pivot.values, x=pivot.columns.tolist(), y=pivot.index.tolist(), colorscale='Blues'))
    fig_hm.update_layout(xaxis_title='Hour', yaxis_title='Day of Week', height=400, template='plotly_white')
    st.plotly_chart(fig_hm, use_container_width=True)
    # Pattern split heatmaps side-by-side
    colh1, colh2 = st.columns(2)
    with colh1:
        dw = dow[dow['PATTERN']=='W']
        pw = dw.pivot_table(index='dow_name', columns='hour', values='minutes', aggfunc='sum', fill_value=0).reindex(names)
        fig_w = go.Figure(data=go.Heatmap(z=pw.values, x=pw.columns.tolist(), y=pw.index.tolist(), colorscale='Reds'))
        fig_w.update_layout(title='West pattern', height=350, template='plotly_white')
        st.plotly_chart(fig_w, use_container_width=True)
    with colh2:
        de = dow[dow['PATTERN']=='E']
        pe = de.pivot_table(index='dow_name', columns='hour', values='minutes', aggfunc='sum', fill_value=0).reindex(names)
        fig_e = go.Figure(data=go.Heatmap(z=pe.values, x=pe.columns.tolist(), y=pe.index.tolist(), colorscale='Greens'))
        fig_e.update_layout(title='East pattern', height=350, template='plotly_white')
        st.plotly_chart(fig_e, use_container_width=True)
else:
    st.info("No head‑to‑head intervals found for the selected range.")

# ========================================
# H2H Conflict Analysis
# ========================================

st.divider()
st.header("✈️ Runway Conflict Analysis")
st.caption("Detected simultaneous opposite-direction takeoffs and landings using flight schedule data")

@st.cache_data(ttl=600)
def get_h2h_conflicts(_session, start_dt, end_dt):
    """Get H2H conflict pairs from FLIGHT_SCHEDULE"""
    q = f"""
    SELECT *
    FROM {db_prefix}.H2H_CONFLICT_PAIRS
    WHERE DATE(a_start) BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    ORDER BY a_start
    """
    try:
        return _session.sql(q).to_pandas()
    except:
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_h2h_summary(_session, start_dt, end_dt):
    """Get H2H summary stats"""
    q = f"""
    SELECT
      COUNT(*) AS total_conflicts,
      COUNT(DISTINCT flight_a) AS unique_flights_a,
      COUNT(DISTINCT flight_b) AS unique_flights_b,
      AVG(ABS(min_gap_seconds)) AS avg_gap_abs_sec,
      MIN(min_gap_seconds) AS min_gap_sec,
      MAX(min_gap_seconds) AS max_gap_sec
    FROM {db_prefix}.H2H_CONFLICT_PAIRS
    WHERE DATE(a_start) BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    """
    try:
        return _session.sql(q).to_pandas()
    except:
        return pd.DataFrame()

@st.cache_data(ttl=600)
def get_h2h_locations(_session, start_dt, end_dt, limit=100):
    """Get spatial locations for conflict pairs"""
    q = f"""
    WITH conflicts AS (
      SELECT * FROM {db_prefix}.H2H_CONFLICT_PAIRS
      WHERE DATE(a_start) BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
      ORDER BY a_start DESC
      LIMIT {limit}
    ), ops_a AS (
      SELECT
        c.event_a_id,
        a.TIMESTAMP,
        ST_Y(a.LOCATION) AS lat,
        ST_X(a.LOCATION) AS lon
      FROM conflicts c
      JOIN {db_prefix}.ADSB_DATA_LOCAL a
        ON a.REGISTRATION = c.aircraft_a
       AND a.TIMESTAMP BETWEEN c.a_start AND c.a_end
      QUALIFY ROW_NUMBER() OVER (PARTITION BY c.event_a_id ORDER BY a.TIMESTAMP) = 1
    ), ops_b AS (
      SELECT
        c.event_b_id,
        a.TIMESTAMP,
        ST_Y(a.LOCATION) AS lat,
        ST_X(a.LOCATION) AS lon
      FROM conflicts c
      JOIN {db_prefix}.ADSB_DATA_LOCAL a
        ON a.REGISTRATION = c.aircraft_b
       AND a.TIMESTAMP BETWEEN c.b_start AND c.b_end
      QUALIFY ROW_NUMBER() OVER (PARTITION BY c.event_b_id ORDER BY a.TIMESTAMP) = 1
    )
    SELECT
      c.*,
      oa.lat AS a_lat,
      oa.lon AS a_lon,
      ob.lat AS b_lat,
      ob.lon AS b_lon
    FROM conflicts c
    LEFT JOIN ops_a oa ON oa.event_a_id = c.event_a_id
    LEFT JOIN ops_b ob ON ob.event_b_id = c.event_b_id
    WHERE oa.lat IS NOT NULL AND ob.lat IS NOT NULL
    """
    try:
        return _session.sql(q).to_pandas()
    except:
        return pd.DataFrame()

with st.spinner("Loading H2H conflict data..."):
    conflicts_df = get_h2h_conflicts(session, start_date, end_date)
    h2h_summary = get_h2h_summary(session, start_date, end_date)

# Apply tolerance filter
if not conflicts_df.empty:
    conflicts_df['gap_abs'] = conflicts_df['MIN_GAP_SECONDS'].abs()
    conflicts_df = conflicts_df[conflicts_df['gap_abs'] <= max_gap_tolerance].copy()

if conflicts_df.empty:
    st.info(f"No runway conflicts detected for the selected range with gap ≤ {max_gap_tolerance} seconds.")
else:
    # Summary KPIs
    summary = h2h_summary.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Conflicts", f"{len(conflicts_df):,}")  # Use filtered count
    c2.metric("Unique Flights", f"{len(set(conflicts_df['FLIGHT_A']) | set(conflicts_df['FLIGHT_B'])):,}")
    c3.metric("Avg Gap", f"{conflicts_df['gap_abs'].mean():.1f} sec")
    c4.metric("Min Gap", f"{conflicts_df['gap_abs'].min():.0f} sec")
    
    st.divider()
    
    # Conflict location map
    st.subheader("🗺️ Conflict Locations")
    loc_df = get_h2h_locations(session, start_date, end_date, limit=50)
    
    if not loc_df.empty:
        # Apply same tolerance filter
        loc_df['gap_abs'] = loc_df['MIN_GAP_SECONDS'].abs()
        loc_df = loc_df[loc_df['gap_abs'] <= max_gap_tolerance]
        
        if not loc_df.empty:
            # Create points for each operation
            points_a = []
            points_b = []
            lines = []
            
            for _, row in loc_df.iterrows():
                # Color by gap severity
                gap = row['gap_abs']
                if gap <= 15:
                    color_a = [255, 0, 0, 200]  # Red - critical
                    color_b = [255, 0, 0, 200]
                elif gap <= 30:
                    color_a = [255, 165, 0, 200]  # Orange - moderate
                    color_b = [255, 165, 0, 200]
                else:
                    color_a = [0, 255, 0, 200]  # Green - safe
                    color_b = [0, 255, 0, 200]
                
                points_a.append({
                    'lat': row['A_LAT'],
                    'lon': row['A_LON'],
                    'color': color_a,
                    'tooltip': f"<b>{row['FLIGHT_A']} ({row['OP_A']})</b><br>Start: {row['A_START']}<br>Gap: {row['gap_abs']:.0f}s"
                })
                points_b.append({
                    'lat': row['B_LAT'],
                    'lon': row['B_LON'],
                    'color': color_b,
                    'tooltip': f"<b>{row['FLIGHT_B']} ({row['OP_B']})</b><br>Start: {row['B_START']}<br>Gap: {row['gap_abs']:.0f}s"
                })
                lines.append({
                    'path': [[row['A_LON'], row['A_LAT']], [row['B_LON'], row['B_LAT']]],
                    'color': color_a
                })
            
            conflict_layers = []
            
            # Add lines connecting conflict pairs
            if lines:
                lines_df = pd.DataFrame(lines)
                conflict_layers.append(pdk.Layer(
                    'PathLayer',
                    data=lines_df,
                    get_path='path',
                    get_color='color',
                    width_scale=2,
                    width_min_pixels=1,
                    pickable=False
                ))
            
            # Add points for operations
            if points_a and points_b:
                pts_df = pd.DataFrame(points_a + points_b)
                conflict_layers.append(pdk.Layer(
                    'ScatterplotLayer',
                    data=pts_df,
                    get_position='[lon, lat]',
                    get_fill_color='color',
                    get_radius=50,
                    radius_min_pixels=5,
                    radius_max_pixels=10,
                    pickable=True
                ))
            
            # Map view
            center_lat = pts_df['lat'].mean() if not pts_df.empty else 32.7338
            center_lon = pts_df['lon'].mean() if not pts_df.empty else -117.1933
            
            conflict_view = pdk.ViewState(
                latitude=center_lat,
                longitude=center_lon,
                zoom=13,
                pitch=0,
                bearing=0
            )
            
            conflict_map = pdk.Deck(
                layers=conflict_layers,
                initial_view_state=conflict_view,
                map_style='light',
                tooltip={"html": "{tooltip}", "style": {"backgroundColor": "steelblue", "color": "white"}}
            )
            
            st.pydeck_chart(conflict_map, use_container_width=True, height=500)
            st.caption(f"Showing {len(loc_df)} most recent conflict locations. Points = operation positions, lines = conflict pairs. Color = gap severity.")
        else:
            st.info(f"No conflicts with gap ≤ {max_gap_tolerance}s have location data")
    else:
        st.info("No location data available for conflicts")
    
    st.divider()
    
    # DoW × Hour heatmap for conflicts
    st.subheader("📅 Conflict Heatmap (Day of Week × Hour)")
    heat_df = conflicts_df.copy()
    heat_df['dow'] = pd.to_datetime(heat_df['A_START']).dt.dayofweek
    heat_df['hour'] = pd.to_datetime(heat_df['A_START']).dt.hour
    pivot_conflicts = heat_df.pivot_table(index='dow', columns='hour', aggfunc='size', fill_value=0)
    dow_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    pivot_conflicts = pivot_conflicts.reindex([0,1,2,3,4,5,6])
    pivot_conflicts.index = dow_names
    
    fig_conflict_heat = go.Figure(data=go.Heatmap(
        z=pivot_conflicts.values,
        x=[f"{h:02d}:00" for h in pivot_conflicts.columns],
        y=pivot_conflicts.index.tolist(),
        colorscale='Reds',
        hovertemplate='%{y}<br>Hour: %{x}<br>Conflicts: %{z}<extra></extra>'
    ))
    fig_conflict_heat.update_layout(
        xaxis_title='Hour of Day',
        yaxis_title='Day of Week',
        height=400,
        template='plotly_white'
    )
    st.plotly_chart(fig_conflict_heat, use_container_width=True)
    
    st.divider()
    
    # Timeline view
    st.subheader("⏱️ Conflict Timeline")
    timeline_df = conflicts_df.copy()
    timeline_df['conflict_time'] = pd.to_datetime(timeline_df['A_START'])
    timeline_df['gap_abs'] = timeline_df['MIN_GAP_SECONDS'].abs()
    timeline_df = timeline_df.sort_values('conflict_time')
    
    # Build rich hover text
    timeline_df['hover_text'] = timeline_df.apply(
        lambda r: (
            f"<b>Conflict: {r['FLIGHT_A']} ({r['OP_A']}) vs {r['FLIGHT_B']} ({r['OP_B']})</b><br>"
            f"Time: {r['A_START']}<br>"
            f"Runway: {r['RUNWAY_MODE']}<br>"
            f"Gap: {r['MIN_GAP_SECONDS']:.0f} sec ({r['gap_abs']:.0f} sec absolute)<br>"
            f"Aircraft: {r['AIRCRAFT_A']} vs {r['AIRCRAFT_B']}"
        ),
        axis=1
    )
    
    fig_timeline = go.Figure()
    fig_timeline.add_trace(go.Scatter(
        x=timeline_df['conflict_time'],
        y=timeline_df['gap_abs'],
        mode='markers',
        marker=dict(
            size=10,
            color=timeline_df['gap_abs'],
            colorscale='RdYlGn',  # Red (low gap) to Green (high gap)
            showscale=True,
            colorbar=dict(
                title="Gap (sec)",
                tickvals=[0, 15, 30, 60],
                ticktext=['0 (Critical)', '15', '30', '60 (Safe)']
            )
        ),
        text=timeline_df['hover_text'],
        hovertemplate='%{text}<extra></extra>'
    ))
    fig_timeline.update_layout(
        xaxis_title='Time',
        yaxis_title='Time Gap (absolute seconds)',
        height=400,
        template='plotly_white',
        hovermode='closest'
    )
    st.plotly_chart(fig_timeline, use_container_width=True)
    
    # Legend
    col_leg1, col_leg2, col_leg3 = st.columns(3)
    with col_leg1:
        st.markdown("🔴 **Red (0-15s)**: Critical - Very close timing")
    with col_leg2:
        st.markdown("🟡 **Yellow (15-30s)**: Moderate - Close separation")
    with col_leg3:
        st.markdown("🟢 **Green (30s+)**: Safe - Adequate separation")
    
    st.divider()
    
    # Conflict pairs table
    st.subheader("📋 Conflict Pairs")
    display_conflicts = conflicts_df[[
        'FLIGHT_A', 'OP_A', 'A_START', 'A_END',
        'FLIGHT_B', 'OP_B', 'B_START', 'B_END',
        'RUNWAY_MODE', 'MIN_GAP_SECONDS'
    ]].copy()
    display_conflicts.columns = [
        'Flight A', 'Type A', 'A Start', 'A End',
        'Flight B', 'Type B', 'B Start', 'B End',
        'Runway', 'Gap (sec)'
    ]
    st.dataframe(display_conflicts, use_container_width=True, height=400)
    st.caption(f"Showing {len(display_conflicts)} conflict pairs. Negative gap = actual time overlap.")

st.divider()
