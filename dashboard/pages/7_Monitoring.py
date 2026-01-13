"""
Monitoring & Health Dashboard
Comprehensive health metrics for data ingestion, flight matching, and task status
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
from snowflake.snowpark.context import get_active_session
import utils

st.set_page_config(page_title="Monitoring", page_icon="🛡️", layout="wide")
utils.apply_custom_css()
session = get_active_session()

db = utils.get_selected_database()
schema = utils.get_schema()
db_prefix = f"{db}.{schema}"

with st.sidebar:
    selected_db = utils.render_airport_selector(sidebar=True)

if not selected_db:
    st.warning("No V5 airport databases found yet. Run the installer first.")
    st.stop()

# Update db_prefix after selector (in case it changed)
db = utils.get_selected_database()
db_prefix = f"{db}.{schema}"

st.title("🛡️ Monitoring & Health")

# =============================================================================
# HEALTH SUMMARY QUERIES
# =============================================================================

@st.cache_data(ttl=120)
def get_health_summary(_session, _db_prefix):
    """Get key health metrics for the summary cards."""
    result = {
        'match_rate': None,
        'match_rate_delta': None,
        'aircraft_today': None,
        'aircraft_delta': None,
        'data_age_min': None,
        'tasks_ok': None,
        'tasks_total': None,
    }
    
    # Match rate (% of local ADS-B points with schedule match)
    try:
        q = f"""
        WITH today_stats AS (
            SELECT 
                COUNT(*) AS total_points,
                COUNT_IF(SCHEDULE_FLIGHT_KEY IS NOT NULL) AS matched_points
            FROM {_db_prefix}.ADSB_DATA_LOCAL
            WHERE TIMESTAMP >= DATEADD('day', -1, CURRENT_TIMESTAMP())
        ),
        yesterday_stats AS (
            SELECT 
                COUNT(*) AS total_points,
                COUNT_IF(SCHEDULE_FLIGHT_KEY IS NOT NULL) AS matched_points
            FROM {_db_prefix}.ADSB_DATA_LOCAL
            WHERE TIMESTAMP >= DATEADD('day', -2, CURRENT_TIMESTAMP())
              AND TIMESTAMP < DATEADD('day', -1, CURRENT_TIMESTAMP())
        )
        SELECT 
            t.total_points,
            t.matched_points,
            ROUND(100.0 * t.matched_points / NULLIF(t.total_points, 0), 1) AS match_rate,
            ROUND(100.0 * y.matched_points / NULLIF(y.total_points, 0), 1) AS match_rate_yesterday
        FROM today_stats t, yesterday_stats y
        """
        r = _session.sql(q).collect()
        if r:
            result['match_rate'] = float(r[0]['MATCH_RATE']) if r[0]['MATCH_RATE'] else 0
            yesterday = float(r[0]['MATCH_RATE_YESTERDAY']) if r[0]['MATCH_RATE_YESTERDAY'] else 0
            result['match_rate_delta'] = round(result['match_rate'] - yesterday, 1) if yesterday else None
    except Exception:
        pass
    
    # Unique aircraft today vs yesterday
    try:
        q = f"""
        WITH today AS (
            SELECT COUNT(DISTINCT ICAO_HEX) AS cnt
            FROM {_db_prefix}.ADSB_DATA_LOCAL
            WHERE TIMESTAMP::DATE = CURRENT_DATE()
        ),
        yesterday AS (
            SELECT COUNT(DISTINCT ICAO_HEX) AS cnt
            FROM {_db_prefix}.ADSB_DATA_LOCAL
            WHERE TIMESTAMP::DATE = DATEADD('day', -1, CURRENT_DATE())
        )
        SELECT t.cnt AS today_cnt, y.cnt AS yesterday_cnt
        FROM today t, yesterday y
        """
        r = _session.sql(q).collect()
        if r:
            result['aircraft_today'] = int(r[0]['TODAY_CNT']) if r[0]['TODAY_CNT'] else 0
            yesterday_cnt = int(r[0]['YESTERDAY_CNT']) if r[0]['YESTERDAY_CNT'] else 0
            result['aircraft_delta'] = result['aircraft_today'] - yesterday_cnt if yesterday_cnt else None
    except Exception:
        pass
    
    # Data freshness (minutes since last ADS-B point)
    # Use SYSDATE() for UTC comparison since ADSB timestamps are stored as TIMESTAMP_NTZ in UTC
    try:
        q = f"""
        SELECT DATEDIFF('minute', MAX(TIMESTAMP), SYSDATE()) AS age_min
        FROM {_db_prefix}.ADSB_DATA_LOCAL
        """
        r = _session.sql(q).collect()
        if r and r[0]['AGE_MIN'] is not None:
            result['data_age_min'] = int(r[0]['AGE_MIN'])
    except Exception:
        pass
    
    # Task health
    try:
        q = f"SHOW TASKS IN SCHEMA {_db_prefix}"
        _session.sql(q).collect()
        q2 = """
        SELECT 
            COUNT_IF(LOWER("state") = 'started') AS started_cnt,
            COUNT(*) AS total_cnt
        FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
        WHERE "name" IN ('TASK_INGEST_ADSB', 'TASK_ENRICH_ADSB_HOURLY', 
                         'TASK_FLIGHT_SCHEDULE_HOURLY', 'TASK_REFRESH_DERIVED_15MIN')
        """
        r = _session.sql(q2).collect()
        if r:
            result['tasks_ok'] = int(r[0]['STARTED_CNT']) if r[0]['STARTED_CNT'] else 0
            result['tasks_total'] = int(r[0]['TOTAL_CNT']) if r[0]['TOTAL_CNT'] else 0
    except Exception:
        pass
    
    return result


# =============================================================================
# HEALTH SUMMARY CARDS
# =============================================================================

health = get_health_summary(session, db_prefix)

st.subheader("📊 Health Summary")
c1, c2, c3, c4 = st.columns(4)

with c1:
    match_rate = health.get('match_rate')
    match_delta = health.get('match_rate_delta')
    if match_rate is not None:
        st.metric(
            "Flight Match Rate",
            f"{match_rate:.1f}%",
            delta=f"{match_delta:+.1f}%" if match_delta is not None else None,
            delta_color="normal"
        )
    else:
        st.metric("Flight Match Rate", "N/A")

with c2:
    aircraft = health.get('aircraft_today')
    aircraft_delta = health.get('aircraft_delta')
    if aircraft is not None:
        st.metric(
            "Aircraft Today",
            f"{aircraft:,}",
            delta=f"{aircraft_delta:+,}" if aircraft_delta is not None else None,
            delta_color="normal"
        )
    else:
        st.metric("Aircraft Today", "N/A")

with c3:
    age = health.get('data_age_min')
    if age is not None:
        if age <= 5:
            age_label = f"{age} min"
            st.metric("Data Freshness", age_label, delta="Fresh", delta_color="normal")
        elif age <= 30:
            st.metric("Data Freshness", f"{age} min", delta="OK", delta_color="off")
        else:
            st.metric("Data Freshness", f"{age} min", delta="Stale", delta_color="inverse")
    else:
        st.metric("Data Freshness", "N/A")

with c4:
    tasks_ok = health.get('tasks_ok')
    tasks_total = health.get('tasks_total')
    if tasks_ok is not None and tasks_total is not None:
        if tasks_ok == tasks_total:
            st.metric("Tasks Running", f"{tasks_ok}/{tasks_total}", delta="All OK", delta_color="normal")
        else:
            st.metric("Tasks Running", f"{tasks_ok}/{tasks_total}", delta="Issues", delta_color="inverse")
    else:
        st.metric("Tasks Running", "N/A")

st.divider()

# =============================================================================
# FLIGHT MATCHING HEALTH
# =============================================================================

st.subheader("✈️ Flight Matching Health")

@st.cache_data(ttl=300)
def get_match_rate_trend(_session, _db_prefix, days=14):
    """Get daily match rate trend."""
    try:
        q = f"""
        SELECT 
            TIMESTAMP::DATE AS service_date,
            COUNT(*) AS total_points,
            COUNT_IF(SCHEDULE_FLIGHT_KEY IS NOT NULL) AS matched_points,
            ROUND(100.0 * COUNT_IF(SCHEDULE_FLIGHT_KEY IS NOT NULL) / NULLIF(COUNT(*), 0), 1) AS match_rate_pct
        FROM {_db_prefix}.ADSB_DATA_LOCAL
        WHERE TIMESTAMP >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1
        ORDER BY 1
        """
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def get_match_method_distribution(_session, _db_prefix):
    """Get distribution of match methods."""
    try:
        q = f"""
        SELECT 
            COALESCE(MATCH_METHOD, 'unmatched') AS match_method,
            COUNT(*) AS point_count
        FROM {_db_prefix}.ADSB_DATA_LOCAL
        WHERE TIMESTAMP >= DATEADD('day', -7, CURRENT_TIMESTAMP())
        GROUP BY 1
        ORDER BY 2 DESC
        """
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def get_unmatched_legs(_session, _db_prefix, days=7):
    """Get count of unmatched flight legs."""
    try:
        q = f"""
        SELECT 
            l.SERVICE_DATE,
            COUNT(*) AS total_legs,
            COUNT_IF(r.SCHEDULE_FLIGHT_KEY IS NULL) AS unmatched_legs,
            ROUND(100.0 * COUNT_IF(r.SCHEDULE_FLIGHT_KEY IS NOT NULL) / NULLIF(COUNT(*), 0), 1) AS leg_match_rate
        FROM {_db_prefix}.HELPER_FLIGHT_LEG l
        LEFT JOIN {_db_prefix}.HELPER_FLIGHT_MATCH_RESULT r
          ON l.SERVICE_DATE = r.SERVICE_DATE
         AND l.ICAO_HEX = r.ICAO_HEX
         AND l.SEG_ID = r.SEG_ID
        WHERE l.SERVICE_DATE >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1
        ORDER BY 1
        """
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


match_col1, match_col2 = st.columns(2)

with match_col1:
    match_trend = get_match_rate_trend(session, db_prefix)
    if match_trend is not None and not match_trend.empty:
        fig = px.line(
            match_trend, 
            x='SERVICE_DATE', 
            y='MATCH_RATE_PCT',
            title='Daily Match Rate (%)',
            labels={'SERVICE_DATE': 'Date', 'MATCH_RATE_PCT': 'Match Rate %'},
            markers=True
        )
        fig.update_layout(yaxis_range=[0, 100])
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No match rate data available yet.")

with match_col2:
    method_dist = get_match_method_distribution(session, db_prefix)
    if method_dist is not None and not method_dist.empty:
        # Clean up method names for display
        method_dist['MATCH_METHOD'] = method_dist['MATCH_METHOD'].replace({
            'callsign': 'Callsign',
            'registration': 'Registration',
            'prior': 'Prior History',
            'propagated': 'Propagated',
            'unmatched': 'Unmatched'
        })
        fig = px.pie(
            method_dist,
            values='POINT_COUNT',
            names='MATCH_METHOD',
            title='Match Method Distribution (Last 7 Days)',
            hole=0.4
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No match method data available yet.")

# Leg match rate table
with st.expander("📋 Flight Leg Match Details (by day)"):
    leg_data = get_unmatched_legs(session, db_prefix)
    if leg_data is not None and not leg_data.empty:
        st.dataframe(leg_data, use_container_width=True, hide_index=True)
    else:
        st.info("No flight leg data available yet.")

st.divider()

# =============================================================================
# DATA VOLUME METRICS
# =============================================================================

st.subheader("📈 Data Volume")

@st.cache_data(ttl=300)
def get_daily_volume(_session, _db_prefix, days=14):
    """Get daily ADS-B point counts and aircraft counts."""
    try:
        q = f"""
        SELECT 
            TIMESTAMP::DATE AS service_date,
            COUNT(*) AS point_count,
            COUNT(DISTINCT ICAO_HEX) AS aircraft_count
        FROM {_db_prefix}.ADSB_DATA_LOCAL
        WHERE TIMESTAMP >= DATEADD('day', -{days}, CURRENT_DATE())
        GROUP BY 1
        ORDER BY 1
        """
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def get_hourly_ingestion(_session, _db_prefix, hours=48):
    """Get hourly ADS-B ingestion counts to monitor data loading."""
    try:
        q = f"""
        SELECT 
            DATE_TRUNC('hour', TIMESTAMP) AS hour_ts,
            COUNT(*) AS point_count,
            COUNT(DISTINCT ICAO_HEX) AS aircraft_count
        FROM {_db_prefix}.ADSB_DATA_LOCAL
        WHERE TIMESTAMP >= DATEADD('hour', -{hours}, CURRENT_TIMESTAMP())
        GROUP BY 1
        ORDER BY 1
        """
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def get_schedule_volume(_session, _db_prefix, days=14):
    """Get daily scheduled flight counts."""
    try:
        q = f"""
        SELECT 
            FLIGHT_DATE AS service_date,
            COUNT(*) AS scheduled_flights,
            COUNT_IF(IS_CODESHARE = FALSE) AS operating_flights
        FROM {_db_prefix}.FLIGHT_SCHEDULE
        WHERE FLIGHT_DATE >= DATEADD('day', -{days}, CURRENT_DATE())
          AND FLIGHT_DATE <= CURRENT_DATE()
        GROUP BY 1
        ORDER BY 1
        """
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


# Hourly ingestion timeseries (full width for visibility)
st.caption("📊 Hourly Ingestion (Last 48 Hours)")
hourly_data = get_hourly_ingestion(session, db_prefix)
if hourly_data is not None and not hourly_data.empty:
    fig_hourly = go.Figure()
    fig_hourly.add_trace(go.Scatter(
        x=hourly_data['HOUR_TS'],
        y=hourly_data['POINT_COUNT'],
        mode='lines+markers',
        name='ADS-B Points',
        line=dict(color='#636EFA', width=2),
        marker=dict(size=4),
        fill='tozeroy',
        fillcolor='rgba(99, 110, 250, 0.2)'
    ))
    fig_hourly.update_layout(
        title='Hourly ADS-B Points Ingested',
        xaxis_title='Time',
        yaxis_title='Points per Hour',
        showlegend=False,
        hovermode='x unified'
    )
    # Add reference line for average
    avg_points = hourly_data['POINT_COUNT'].mean()
    fig_hourly.add_hline(
        y=avg_points, 
        line_dash="dash", 
        line_color="orange",
        annotation_text=f"Avg: {avg_points:,.0f}",
        annotation_position="right"
    )
    st.plotly_chart(fig_hourly, use_container_width=True)
    
    # Show recent hours summary
    recent_hours = hourly_data.tail(6)
    if not recent_hours.empty:
        latest_hour = recent_hours.iloc[-1]
        prev_hour = recent_hours.iloc[-2] if len(recent_hours) > 1 else None
        
        h_col1, h_col2, h_col3, h_col4 = st.columns(4)
        with h_col1:
            st.metric(
                "Last Hour Points",
                f"{int(latest_hour['POINT_COUNT']):,}",
                delta=f"{int(latest_hour['POINT_COUNT'] - prev_hour['POINT_COUNT']):+,}" if prev_hour is not None else None
            )
        with h_col2:
            st.metric("Last Hour Aircraft", f"{int(latest_hour['AIRCRAFT_COUNT']):,}")
        with h_col3:
            st.metric("6h Avg Points/Hour", f"{int(recent_hours['POINT_COUNT'].mean()):,}")
        with h_col4:
            # Check for gaps (hours with zero or very low points)
            low_hours = len(recent_hours[recent_hours['POINT_COUNT'] < avg_points * 0.1])
            if low_hours == 0:
                st.metric("Data Continuity", "✅ Good", delta="No gaps")
            else:
                st.metric("Data Continuity", "⚠️ Gaps", delta=f"{low_hours} low hours", delta_color="inverse")
else:
    st.info("No hourly ingestion data available yet.")

st.divider()

vol_col1, vol_col2 = st.columns(2)

with vol_col1:
    daily_vol = get_daily_volume(session, db_prefix)
    if daily_vol is not None and not daily_vol.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=daily_vol['SERVICE_DATE'],
            y=daily_vol['POINT_COUNT'],
            name='ADS-B Points',
            marker_color='#636EFA'
        ))
        fig.update_layout(
            title='Daily ADS-B Points (Last 14 Days)',
            xaxis_title='Date',
            yaxis_title='Points',
            showlegend=False
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No volume data available yet.")

with vol_col2:
    if daily_vol is not None and not daily_vol.empty:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=daily_vol['SERVICE_DATE'],
            y=daily_vol['AIRCRAFT_COUNT'],
            mode='lines+markers',
            name='Unique Aircraft',
            line=dict(color='#00CC96', width=2),
            marker=dict(size=8)
        ))
        fig.update_layout(
            title='Daily Unique Aircraft',
            xaxis_title='Date',
            yaxis_title='Aircraft Count'
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No aircraft count data available yet.")

# Schedule vs observed comparison
with st.expander("📊 Schedule vs Observed Flights"):
    sched_vol = get_schedule_volume(session, db_prefix)
    leg_data = get_unmatched_legs(session, db_prefix)
    
    if sched_vol is not None and not sched_vol.empty:
        col_a, col_b = st.columns(2)
        with col_a:
            st.caption("Scheduled Flights per Day")
            st.dataframe(sched_vol, use_container_width=True, hide_index=True)
        with col_b:
            if leg_data is not None and not leg_data.empty:
                st.caption("Observed Flight Legs per Day")
                st.dataframe(leg_data[['SERVICE_DATE', 'TOTAL_LEGS']], use_container_width=True, hide_index=True)
    else:
        st.info("No schedule volume data available yet.")

st.divider()

# =============================================================================
# TASK HEALTH
# =============================================================================

st.subheader("⚙️ Task Health")

@st.cache_data(ttl=60)
def get_task_status(_session, _db_prefix):
    """Get status of all tasks in the schema."""
    try:
        q = f"SHOW TASKS IN SCHEMA {_db_prefix}"
        _session.sql(q).collect()
        q2 = """
        SELECT 
            "name" AS task_name,
            "state" AS status,
            "schedule" AS schedule,
            "last_committed_on" AS last_modified
        FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
        ORDER BY "name"
        """
        return _session.sql(q2).to_pandas()
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=120)
def get_task_history(_session, db, schema, limit=20):
    """Get recent task execution history."""
    try:
        q = f"""
        SELECT 
            NAME AS task_name,
            STATE AS run_state,
            SCHEDULED_TIME,
            COMPLETED_TIME,
            DATEDIFF('second', SCHEDULED_TIME, COMPLETED_TIME) AS duration_sec,
            ERROR_MESSAGE
        FROM TABLE({db}.INFORMATION_SCHEMA.TASK_HISTORY())
        WHERE SCHEMA_NAME = '{schema}'
        ORDER BY SCHEDULED_TIME DESC
        LIMIT {limit}
        """
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()


task_col1, task_col2 = st.columns(2)

with task_col1:
    st.caption("Task Status")
    task_status = get_task_status(session, db_prefix)
    if task_status is not None and not task_status.empty:
        # Add status indicator
        def status_indicator(status):
            if str(status).lower() == 'started':
                return '🟢 Running'
            elif str(status).lower() == 'suspended':
                return '🟡 Suspended'
            else:
                return f'⚪ {status}'
        
        task_status['STATUS'] = task_status['STATUS'].apply(status_indicator)
        st.dataframe(task_status, use_container_width=True, hide_index=True)
    else:
        st.info("Could not retrieve task status.")

with task_col2:
    st.caption("Recent Task Runs")
    show_history = st.checkbox("Load task history", value=False, 
                               help="May require additional permissions")
    if show_history:
        task_hist = get_task_history(session, db, schema)
        if task_hist is not None and not task_hist.empty:
            # Add run state indicator
            def run_indicator(state):
                if str(state).upper() == 'SUCCEEDED':
                    return '✅ Success'
                elif str(state).upper() == 'FAILED':
                    return '❌ Failed'
                elif str(state).upper() == 'CANCELLED':
                    return '⚠️ Cancelled'
                else:
                    return f'⏳ {state}'
            
            task_hist['RUN_STATE'] = task_hist['RUN_STATE'].apply(run_indicator)
            st.dataframe(
                task_hist[['TASK_NAME', 'RUN_STATE', 'SCHEDULED_TIME', 'DURATION_SEC']],
                use_container_width=True, 
                hide_index=True
            )
            
            # Show failures if any
            failures = task_hist[task_hist['ERROR_MESSAGE'].notna() & (task_hist['ERROR_MESSAGE'] != '')]
            if not failures.empty:
                st.warning(f"⚠️ {len(failures)} task failures in recent history")
                with st.expander("View failure details"):
                    st.dataframe(failures[['TASK_NAME', 'SCHEDULED_TIME', 'ERROR_MESSAGE']], 
                                 use_container_width=True, hide_index=True)
        else:
            st.info("No task history available (may require elevated permissions).")

st.divider()

# =============================================================================
# DATA FRESHNESS DETAILS
# =============================================================================

st.subheader("🕐 Data Freshness")

@st.cache_data(ttl=60)
def get_freshness_details(_session, _db_prefix):
    """Get detailed freshness metrics."""
    result = {}
    
    # ADS-B last point
    # Use SYSDATE() for UTC comparison since timestamps are stored as TIMESTAMP_NTZ in UTC
    try:
        q = f"""
        SELECT 
            MAX(TIMESTAMP) AS last_adsb_ts,
            DATEDIFF('minute', MAX(TIMESTAMP), SYSDATE()) AS adsb_age_min
        FROM {_db_prefix}.ADSB_DATA_LOCAL
        """
        r = _session.sql(q).collect()
        if r:
            result['adsb_last_ts'] = r[0]['LAST_ADSB_TS']
            result['adsb_age_min'] = r[0]['ADSB_AGE_MIN']
    except Exception:
        pass
    
    # Schedule last update
    try:
        q = f"""
        SELECT 
            MAX(UPDATED_AT) AS last_schedule_ts,
            DATEDIFF('hour', MAX(UPDATED_AT), SYSDATE()) AS schedule_age_hours
        FROM {_db_prefix}.FLIGHT_SCHEDULE
        """
        r = _session.sql(q).collect()
        if r:
            result['schedule_last_ts'] = r[0]['LAST_SCHEDULE_TS']
            result['schedule_age_hours'] = r[0]['SCHEDULE_AGE_HOURS']
    except Exception:
        pass
    
    # Enrichment last run
    try:
        q = f"""
        SELECT 
            MAX(MATCHED_AT) AS last_enrichment_ts,
            DATEDIFF('minute', MAX(MATCHED_AT), SYSDATE()) AS enrichment_age_min
        FROM {_db_prefix}.ADSB_DATA
        WHERE MATCHED_AT IS NOT NULL
        """
        r = _session.sql(q).collect()
        if r:
            result['enrichment_last_ts'] = r[0]['LAST_ENRICHMENT_TS']
            result['enrichment_age_min'] = r[0]['ENRICHMENT_AGE_MIN']
    except Exception:
        pass
    
    return result


freshness = get_freshness_details(session, db_prefix)

fresh_col1, fresh_col2, fresh_col3 = st.columns(3)

with fresh_col1:
    adsb_ts = freshness.get('adsb_last_ts')
    adsb_age = freshness.get('adsb_age_min')
    st.metric(
        "Last ADS-B Point",
        str(adsb_ts)[:19] if adsb_ts else "N/A",
        delta=f"{adsb_age} min ago" if adsb_age is not None else None,
        delta_color="off"
    )

with fresh_col2:
    sched_ts = freshness.get('schedule_last_ts')
    sched_age = freshness.get('schedule_age_hours')
    st.metric(
        "Last Schedule Update",
        str(sched_ts)[:19] if sched_ts else "N/A",
        delta=f"{sched_age} hours ago" if sched_age is not None else None,
        delta_color="off"
    )

with fresh_col3:
    enrich_ts = freshness.get('enrichment_last_ts')
    enrich_age = freshness.get('enrichment_age_min')
    st.metric(
        "Last Enrichment Run",
        str(enrich_ts)[:19] if enrich_ts else "N/A",
        delta=f"{enrich_age} min ago" if enrich_age is not None else None,
        delta_color="off"
    )

st.divider()

# =============================================================================
# EXISTING SECTIONS (Last Refresh, QA Counts, Audit)
# =============================================================================

st.subheader("📋 Detailed Monitoring Tables")

# Extract airport code from database name (e.g., AIRPORT_SAN -> SAN)
airport_code = db.replace('AIRPORT_', '') if db else 'SAN'

with st.sidebar:
    st.divider()
    st.subheader("Audit Filters")
    lookback_days = st.slider("Lookback (days)", min_value=1, max_value=90, value=7)

col1, col2 = st.columns(2)

with col1:
    st.caption("Last Refresh Timestamps")
    try:
        q = f"""
        SELECT table_name, last_refreshed_at, status, row_count_24h, details
        FROM {db_prefix}.HELPER_MONITOR_LAST_REFRESH
        ORDER BY last_refreshed_at DESC
        """
        df = session.sql(q).to_pandas()
        if df is None or df.empty:
            st.info("No refresh metadata yet.")
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)
    except Exception:
        st.info("Refresh table not available yet.")

with col2:
    st.caption("Daily QA Metrics (Last 14 Days)")
    try:
        q2 = f"""
        SELECT metric_date, metric_name, metric_value
        FROM {db_prefix}.HELPER_QA_COUNTS_DAILY
        WHERE metric_date >= DATEADD('day', -14, CURRENT_DATE)
        ORDER BY metric_date DESC, metric_name
        """
        qa = session.sql(q2).to_pandas()
        if qa is None or qa.empty:
            st.info("No QA metrics logged yet.")
        else:
            # Pivot for better display
            try:
                qa_pivot = qa.pivot(index='METRIC_DATE', columns='METRIC_NAME', values='METRIC_VALUE').reset_index()
                st.dataframe(qa_pivot, use_container_width=True, hide_index=True)
            except Exception:
                st.dataframe(qa, use_container_width=True, hide_index=True)
    except Exception:
        st.info("QA metrics table not available yet.")

st.divider()

st.subheader("📥 Ingestion Audit")

@st.cache_data(ttl=60)
def get_audit(_session, _db_prefix, code, days):
    q = f"""
    SELECT 
      run_id,
      airport_code,
      window_start,
      window_end,
      rows_raw,
      rows_inserted,
      rows_deduped,
      status,
      error_message,
      created_at
    FROM {_db_prefix}.HELPER_INGEST_AUDIT
    WHERE airport_code = '{code}'
      AND created_at >= DATEADD('day', -{int(days)}, CURRENT_TIMESTAMP())
    ORDER BY created_at DESC
    """
    try:
        return _session.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame()

with st.spinner("Loading audit events..."):
    audit = get_audit(session, db_prefix, airport_code, lookback_days)

if audit is None or audit.empty:
    st.info("No recent audit records.")
else:
    st.dataframe(audit, use_container_width=True, hide_index=True)

    st.caption("Latest Ingestion Summary")
    latest = audit.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows Raw", f"{int(latest.get('ROWS_RAW',0)):,}")
    c2.metric("Rows Inserted", f"{int(latest.get('ROWS_INSERTED',0)):,}")
    c3.metric("Rows Deduped", f"{int(latest.get('ROWS_DEDUPED',0)):,}")
    c4.metric("Status", latest.get('STATUS','N/A'))

st.divider()
st.caption(f"Data sources: `{db_prefix}.HELPER_MONITOR_LAST_REFRESH`, `{db_prefix}.HELPER_QA_COUNTS_DAILY`, `{db_prefix}.HELPER_INGEST_AUDIT`, `{db_prefix}.ADSB_DATA_LOCAL`, `{db_prefix}.HELPER_FLIGHT_LEG`, `{db_prefix}.HELPER_FLIGHT_MATCH_RESULT`")
