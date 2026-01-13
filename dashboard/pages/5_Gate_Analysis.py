"""
Gate Analysis Page - Gate utilization and dwell analytics
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
    page_title="Gate Analysis",
    page_icon="🛬",
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
st.title("🛬 Gate Analysis")

# Sidebar filters
with st.sidebar:
    selected_db = utils.render_airport_selector(sidebar=True)

    if not selected_db:
        st.warning("No V5 airport databases found yet. Run the installer first.")
        st.stop()

    st.subheader("Filters")
    min_date, max_date = utils.get_date_range(session)
    start_date, end_date = (
        (max_date - timedelta(days=7), max_date) if max_date else (datetime.now().date() - timedelta(days=7), datetime.now().date())
    )
    date_range = st.date_input(
        "Date Range",
        value=(start_date, end_date)
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date = end_date = date_range
@st.cache_data(ttl=300)
def get_gate_fill_rate(_session, start_dt, end_dt, _db_prefix):
    """Calculate gate fill rate from GATE_ANALYSIS_AIRCRAFT_GROUND_SESSIONS vs GATE_ANALYSIS_FLIGHT_GATE_TIME."""
    q = f"""
    WITH total AS (
        SELECT COUNT(DISTINCT ground_session_id) AS cnt
        FROM {_db_prefix}.GATE_ANALYSIS_AIRCRAFT_GROUND_SESSIONS
        WHERE service_date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    ),
    with_gate AS (
        SELECT COUNT(DISTINCT ground_session_id) AS cnt
        FROM {_db_prefix}.GATE_ANALYSIS_FLIGHT_GATE_TIME
        WHERE service_date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    )
    SELECT 
        t.cnt AS total_rows,
        COALESCE(w.cnt, 0) AS with_gate,
        ROUND(COALESCE(w.cnt, 0) / NULLIF(t.cnt, 0) * 100, 1) AS fill_rate_pct
    FROM total t
    CROSS JOIN with_gate w
    """
    try:
        df = _session.sql(q).to_pandas()
        if df is not None and not df.empty:
            return int(df.iloc[0]['TOTAL_ROWS']), int(df.iloc[0]['WITH_GATE']), float(df.iloc[0]['FILL_RATE_PCT'])
    except Exception:
        pass
    return 0, 0, 0.0

# Airline utility
def to_airline_name(code: str, name_map: dict) -> str:
    """Get airline name from code using the provided mapping"""
    try:
        return name_map.get(str(code), str(code)) if code else 'Unknown'
    except Exception:
        return str(code) if code else 'Unknown'

@st.cache_data(ttl=300)
def get_airline_utilization(_session, start_dt, end_dt):
    """Dwell minutes and flights by airline (uses derived table; callsign not required)."""
    q = f"""
    SELECT 
      airline_code,
      SUM(dwell_minutes) AS dwell_minutes,
      SUM(flights) AS flights
    FROM {db_prefix}.GATE_ANALYSIS_GATE_AIRLINE_DWELL_DAILY
    WHERE date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    GROUP BY airline_code
    ORDER BY dwell_minutes DESC
    """
    return _session.sql(q).to_pandas()

@st.cache_data(ttl=300)
def get_gate_rankings(_session, start_dt, end_dt):
    """Top gates by total dwell minutes and distinct flights"""
    q = f"""
    SELECT gate_name,
           SUM(dwell_minutes) AS total_dwell_minutes,
           SUM(flights) AS flights
    FROM {db_prefix}.GATE_ANALYSIS_GATE_UTIL_DAILY
    WHERE date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    GROUP BY gate_name
    ORDER BY total_dwell_minutes DESC
    """
    return _session.sql(q).to_pandas()

@st.cache_data(ttl=300)
def get_gate_by_airline_breakdown(_session, start_dt, end_dt):
    """Breakdown per gate by airline for dwell minutes and flights"""
    q = f"""
    SELECT gate_name,
           airline_code,
           SUM(dwell_minutes) AS dwell_minutes,
           SUM(flights) AS flights
    FROM {db_prefix}.GATE_ANALYSIS_GATE_AIRLINE_DWELL_DAILY
    WHERE date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    GROUP BY gate_name, airline_code
    """
    return _session.sql(q).to_pandas()

@st.cache_data(ttl=300)
def get_gate_dow_heatmap(_session, start_dt, end_dt):
    """Aggregate dwell minutes by gate and day-of-week for the selected interval."""
    q = f"""
    SELECT 
      closest_gate_name AS gate_name,
      DAYOFWEEK(ts) AS day_of_week,
      SUM(lag_seconds)/60.0 AS dwell_minutes
    FROM {db_prefix}.GATE_ANALYSIS_ADSB_GROUND_POINTS
    WHERE ts::TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
      AND closest_gate_name IS NOT NULL
    GROUP BY gate_name, day_of_week
    """
    return _session.sql(q).to_pandas()

@st.cache_data(ttl=300)
def get_top_dwell_flights(_session, start_dt, end_dt, top_n: int = 20):
    """Top N ground sessions by dwell minutes within the given period."""
    q = f"""
    WITH dim_icao AS (
      SELECT
        TRIM(UPPER(AIRLINE_ICAO)) AS airline_icao,
        MAX(NULLIF(TRIM(AIRLINE_IATA), '')) AS airline_iata,
        MAX(NULLIF(TRIM(AIRLINE_NAME), '')) AS airline_name
      FROM {db_prefix}.HELPER_AIRLINE_DIM
      WHERE AIRLINE_ICAO IS NOT NULL AND TRIM(AIRLINE_ICAO) <> ''
      GROUP BY 1
    ),
    dim_iata AS (
      SELECT
        TRIM(UPPER(AIRLINE_IATA)) AS airline_iata,
        MAX(NULLIF(TRIM(AIRLINE_ICAO), '')) AS airline_icao,
        MAX(NULLIF(TRIM(AIRLINE_NAME), '')) AS airline_name
      FROM {db_prefix}.HELPER_AIRLINE_DIM
      WHERE AIRLINE_IATA IS NOT NULL AND TRIM(AIRLINE_IATA) <> ''
      GROUP BY 1
    ),
    per_session AS (
      SELECT ground_session_id, icao_hex, service_date AS day, SUM(lag_seconds)/60.0 AS dwell_minutes
      FROM {db_prefix}.GATE_ANALYSIS_ADSB_GROUND_POINTS
      WHERE ts::DATE BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
        AND closest_gate_name IS NOT NULL
      GROUP BY 1,2,3
    ),
    gate AS (
      SELECT ground_session_id, gate_name, dwell_seconds, flight_number
      FROM {db_prefix}.GATE_ANALYSIS_FLIGHT_GATE_TIME
      WHERE service_date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    ),
    airline AS (
      SELECT
        ICAO_HEX,
        TIMESTAMP::DATE AS day,
        MAX(NULLIF(TRIM(AIRLINE_ICAO), '')) AS airline_icao,
        MAX(NULLIF(TRIM(AIRLINE_IATA), '')) AS airline_iata,
        MAX(NULLIF(TRIM(AIRLINE_NAME), '')) AS airline_name
      FROM {db_prefix}.ADSB_DATA_LOCAL
      WHERE TIMESTAMP::DATE BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
      GROUP BY 1, 2
    )
    SELECT 
      COALESCE(NULLIF(TRIM(g.flight_number), ''), p.icao_hex) AS flight_number,
      COALESCE(
        a.airline_icao,
        a.airline_iata,
        di.airline_icao,
        dj.airline_iata,
        REGEXP_SUBSTR(UPPER(TRIM(g.flight_number)), '^[A-Z]{{3}}'),
        REGEXP_SUBSTR(UPPER(TRIM(g.flight_number)), '^[A-Z]{{2}}'),
        'UNK'
      ) AS airline_code,
      COALESCE(
        a.airline_name,
        di.airline_name,
        dj.airline_name
      ) AS airline_name,
      p.day,
      g.gate_name,
      ROUND(p.dwell_minutes) AS dwell_minutes
    FROM per_session p
    LEFT JOIN gate g ON g.ground_session_id = p.ground_session_id
    LEFT JOIN airline a ON a.icao_hex = p.icao_hex AND a.day = p.day
    -- Fallback: infer airline from callsign prefix when ADSB enrichment is missing
    LEFT JOIN dim_icao di ON di.airline_icao = REGEXP_SUBSTR(UPPER(TRIM(g.flight_number)), '^[A-Z]{{3}}')
    LEFT JOIN dim_iata dj ON dj.airline_iata = REGEXP_SUBSTR(UPPER(TRIM(g.flight_number)), '^[A-Z]{{2}}')
    ORDER BY p.dwell_minutes DESC
    LIMIT {int(top_n)}
    """
    return _session.sql(q).to_pandas()

# KPI row now that dates are known
tr, wg, fr = get_gate_fill_rate(session, start_date, end_date, db_prefix)
colk1, colk2, colk3 = st.columns(3)
colk1.metric("Air Ops rows", f"{tr:,}")
colk2.metric("With GATE_ACTUAL", f"{wg:,}")
colk3.metric("Gate Actual Fill‑Rate", f"{fr:.1f}%")

# Airline dropdown (full names)
air_df_all = get_airline_utilization(session, start_date, end_date)
code_to_name = utils.get_airline_name_map(session, start_date, end_date)
if air_df_all is not None and not air_df_all.empty:
    air_df_all['AIRLINE_NAME'] = air_df_all['AIRLINE_CODE'].apply(lambda c: code_to_name.get(str(c), str(c)))
    airline_options = ["All Airlines"] + air_df_all.sort_values('AIRLINE_NAME')['AIRLINE_NAME'].tolist()
else:
    airline_options = ["All Airlines"]

with st.sidebar:
    airline_selected = st.selectbox("Airline", options=airline_options, index=0)
    hide_unknown_airlines = st.checkbox("Hide Unknown (UNK)", value=False)

# Sections
st.subheader("🏢 Gate Utilization by Airline (Stacked Minutes by Gate)")
# Use full gate-by-airline breakdown to build stacked absolute minutes per airline
breakdown_all = get_gate_by_airline_breakdown(session, start_date, end_date)
if breakdown_all is not None and not breakdown_all.empty:
    breakdown_all = breakdown_all.copy()
    if hide_unknown_airlines:
        breakdown_all = breakdown_all[breakdown_all['AIRLINE_CODE'].astype(str) != 'UNK']
    breakdown_all['AIRLINE_NAME'] = breakdown_all['AIRLINE_CODE'].apply(lambda c: code_to_name.get(str(c), str(c)))
    if airline_selected != "All Airlines":
        breakdown_all = breakdown_all[breakdown_all['AIRLINE_NAME'] == airline_selected]
    air_gate_pivot = breakdown_all.pivot_table(index='AIRLINE_NAME', columns='GATE_NAME', values='DWELL_MINUTES', aggfunc='sum', fill_value=0)
    air_gate_pivot = air_gate_pivot.round(0).sort_index()
    gate_names = list(air_gate_pivot.columns)
    base_colors = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b',
        '#e377c2', '#7f7f7f', '#bcbd22', '#17becf', '#4daf4a', '#984ea3',
        '#ff7f00', '#377eb8', '#f781bf', '#a65628', '#999999'
    ]
    gate_palette = {g: base_colors[i % len(base_colors)] for i, g in enumerate(gate_names)}
    fig_air = go.Figure()
    for g in gate_names:
        fig_air.add_trace(go.Bar(
            x=air_gate_pivot[g],
            y=air_gate_pivot.index,
            name=str(g),
            orientation='h',
            marker_color=gate_palette[g],
            customdata=[str(g)] * len(air_gate_pivot.index),
            hovertemplate="Gate %{customdata}: %{x:.0f} min<extra></extra>"
        ))
    n_rows = len(air_gate_pivot)
    fig_air.update_layout(
        barmode='stack',
        height=min(max(500, 40 * n_rows), 1200),
        xaxis_title='Dwell Minutes',
        yaxis_title='Airline',
        template='plotly_white',
        bargap=0.1,
        bargroupgap=0.02,
        margin=dict(l=160, r=30, t=40, b=40),
        showlegend=False
    )
    st.plotly_chart(fig_air, use_container_width=True)
else:
    st.info("No airline-gate breakdown available in selected range.")

st.divider()

# Gate-level stacked bars by airline proportions
breakdown_df = get_gate_by_airline_breakdown(session, start_date, end_date)
if airline_selected != "All Airlines" and breakdown_df is not None and not breakdown_df.empty:
    # Filter to selected airline name -> map back to codes
    codes_map = {code_to_name.get(str(c), str(c)): c for c in air_df_all['AIRLINE_CODE'].tolist()} if air_df_all is not None and not air_df_all.empty else {}
    selected_code = codes_map.get(airline_selected)
    if selected_code:
        breakdown_df = breakdown_df[breakdown_df['AIRLINE_CODE'] == selected_code]

# Ensure we have all gates, sort alphabetically
if breakdown_df is None:
    breakdown_df = pd.DataFrame(columns=['GATE_NAME','AIRLINE_CODE','DWELL_MINUTES','FLIGHTS'])

if hide_unknown_airlines and breakdown_df is not None and not breakdown_df.empty:
    breakdown_df = breakdown_df[breakdown_df['AIRLINE_CODE'].astype(str) != 'UNK']

# Build stacked horizontal bar for dwell minutes by gate
st.subheader("🧭 Gate Utilization by Gate (Dwell Minutes)")
if not breakdown_df.empty:
    dwell_pivot = breakdown_df.pivot_table(index='GATE_NAME', columns='AIRLINE_CODE', values='DWELL_MINUTES', aggfunc='sum', fill_value=0)
    dwell_pivot = dwell_pivot.sort_index()
    fig_dwell = go.Figure()
    # Use consistent color set per airline code
    airlines_codes = list(dwell_pivot.columns)
    palette = {}
    base_colors = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b',
        '#e377c2', '#7f7f7f', '#bcbd22', '#17becf', '#4daf4a', '#984ea3',
        '#ff7f00', '#377eb8', '#f781bf', '#a65628', '#999999'
    ]
    for i, code in enumerate(airlines_codes):
        palette[code] = base_colors[i % len(base_colors)]
    for code in airlines_codes:
        fig_dwell.add_trace(go.Bar(
            x=dwell_pivot[code],
            y=dwell_pivot.index,
            name=code_to_name.get(str(code), str(code)),
            orientation='h',
            marker_color=palette.get(code, '#1f77b4')
        ))
    n_gates = len(dwell_pivot)
    fig_dwell.update_layout(
        barmode='stack',
        height=min(max(800, 28 * n_gates), 2400),
        xaxis_title='Dwell Minutes',
        yaxis_title='Gate',
        template='plotly_white',
        bargap=0.1,
        bargroupgap=0.02,
        margin=dict(l=160, r=20, t=40, b=40),
        yaxis=dict(tickfont=dict(size=11))
    )
    st.plotly_chart(fig_dwell, use_container_width=True)
else:
    st.info("No gate dwell data available.")

st.divider()

# Build stacked horizontal bar for number of flights by gate
st.subheader("🧮 Gate Utilization by Gate (Number of Flights)")
if not breakdown_df.empty:
    flights_pivot = breakdown_df.pivot_table(index='GATE_NAME', columns='AIRLINE_CODE', values='FLIGHTS', aggfunc='sum', fill_value=0)
    flights_pivot = flights_pivot.sort_index()
    fig_flights = go.Figure()
    airline_codes = list(flights_pivot.columns)
    # reuse palette
    for code in airline_codes:
        fig_flights.add_trace(go.Bar(
            x=flights_pivot[code],
            y=flights_pivot.index,
            name=code_to_name.get(str(code), str(code)),
            orientation='h',
            marker_color=palette.get(code, '#1f77b4')
        ))
    n_gates_f = len(flights_pivot)
    fig_flights.update_layout(
        barmode='stack',
        height=min(max(800, 28 * n_gates_f), 2400),
        xaxis_title='Flights',
        yaxis_title='Gate',
        template='plotly_white',
        bargap=0.1,
        bargroupgap=0.02,
        margin=dict(l=160, r=20, t=40, b=40),
        yaxis=dict(tickfont=dict(size=11))
    )
    st.plotly_chart(fig_flights, use_container_width=True)
else:
    st.info("No gate flights data available.")

st.divider()

st.subheader("🏅 Top 20 Flights by Dwell Time (Minutes)")
top_df = get_top_dwell_flights(session, start_date, end_date, top_n=20)
if top_df is not None and not top_df.empty:
    display_df = top_df.copy()
    if hide_unknown_airlines:
        display_df = display_df[display_df['AIRLINE_CODE'].astype(str).str.strip().str.upper() != 'UNK']
    # Prefer DB-provided airline_name (from HELPER_AIRLINE_DIM fallback), then fallback to mapping
    if 'AIRLINE_NAME' not in display_df.columns:
        display_df['AIRLINE_NAME'] = None
    display_df['AIRLINE_NAME'] = display_df['AIRLINE_NAME'].fillna(
        display_df['AIRLINE_CODE'].apply(lambda c: code_to_name.get(str(c), str(c)))
    )
    display_df['LABEL'] = display_df.apply(lambda r: f"{str(r.get('FLIGHT_NUMBER',''))} — {r.get('AIRLINE_NAME','')} — {r.get('DAY','')} ({r.get('GATE_NAME','N/A')})", axis=1)
    fig_top = go.Figure(go.Bar(
        x=display_df['DWELL_MINUTES'].round(0),
        y=display_df['LABEL'],
        orientation='h',
        marker_color='#4FC3F7'
    ))
    n_rows = len(display_df)
    fig_top.update_layout(
        height=min(max(600, 28 * n_rows), 1200),
        xaxis_title='Dwell Minutes',
        yaxis_title='Flight (Gate)',
        template='plotly_white',
        margin=dict(l=180, r=20, t=40, b=40)
    )
    st.plotly_chart(fig_top, use_container_width=True)
else:
    st.info("No flights with dwell time found in selected range.")

st.divider()

st.subheader("📊 Gate Usage Heatmap by Day of Week")
hm_df = get_gate_dow_heatmap(session, start_date, end_date)
if hm_df is not None and not hm_df.empty:
    # Map day numbers to names: 0/1.. mapping depends on DB; Snowflake DAYOFWEEK returns 0=Sunday
    day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    hm_df = hm_df.copy()
    hm_df['DAY_NAME'] = hm_df['DAY_OF_WEEK'].apply(lambda x: day_names[int(x) % 7])
    # Pivot to matrix: rows = day, cols = gate
    pivot = hm_df.pivot_table(index='DAY_NAME', columns='GATE_NAME', values='DWELL_MINUTES', aggfunc='sum', fill_value=0)
    # Reorder rows by week sequence
    pivot = pivot.reindex(day_names)
    fig_hm = go.Figure(data=go.Heatmap(
        z=pivot.values,
        x=pivot.columns.tolist(),
        y=pivot.index.tolist(),
        colorscale='Blues',
        hovertemplate='Day %{y}<br>Gate %{x}<br>Dwell %{z:.0f} min<extra></extra>'
    ))
    fig_hm.update_layout(
        xaxis_title='Gate',
        yaxis_title='Day of Week',
        height=500,
        template='plotly_white'
    )
    st.plotly_chart(fig_hm, use_container_width=True)
else:
    st.info("No gate/day-of-week data available for the selected range.")
