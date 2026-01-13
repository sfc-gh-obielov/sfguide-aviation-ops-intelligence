"""
Traffic Analysis Page - Temporal patterns and traffic trends
Analyze flight patterns, peak hours, and traffic trends over time
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from snowflake.snowpark.context import get_active_session
from datetime import datetime, timedelta
import sys
sys.path.append('..')
import utils

# Page configuration
st.set_page_config(
    page_title="Traffic Analysis",
    page_icon="📊",
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
st.title("📊 Air Traffic Analysis")
st.markdown("Explore temporal patterns, peak hours, and traffic trends over time for the selected airport")

# Sidebar controls
with st.sidebar:
    
    # Get date range
    min_date, max_date = utils.get_date_range(session)
    start_date, end_date, analysis_period = utils.render_time_period_filter(
        min_date,
        max_date,
        key_prefix="traffic",
        default_period="Last 7 Days",
    )
    
    st.divider()
    
    # Granularity selector (dropdown) - daily or weekly
    granularity = st.selectbox(
        "Time Granularity",
        options=["daily", "weekly"],
        index=0
    )
    
    # Delay threshold for Flight Details classification
    delay_threshold = st.number_input(
        "Delay threshold (minutes)",
        min_value=0,
        value=15,
        step=1,
        help="Absolute deviation from scheduled time to consider earlier/later arrivals"
    )
    
    # Always show airline breakdown and heatmap (toggles removed)
    show_airlines = True
    show_heatmap = True
    hide_unknown_airlines = st.checkbox("Hide Unknown (UNK)", value=False)

# Query functions
@st.cache_data(ttl=300)
def get_hourly_traffic(_session, start_dt, end_dt):
    """Get hourly flight counts"""
    query = f"""
    SELECT hour,
           aircraft_count,
           data_points
    FROM {db_prefix}.FLIGHT_TRAFFIC_FACT_ADSB_HOURLY
    WHERE hour BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
    ORDER BY hour
    """
    return _session.sql(query).to_pandas()

@st.cache_data(ttl=300)
def get_daily_traffic(_session, start_dt, end_dt):
    """Get daily flight statistics"""
    query = f"""
    SELECT date,
           unique_aircraft,
           unique_flights,
           total_records,
           avg_altitude,
           avg_speed
    FROM {db_prefix}.FLIGHT_TRAFFIC_FACT_ADSB_DAILY
    WHERE date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    ORDER BY date
    """
    return _session.sql(query).to_pandas()

@st.cache_data(ttl=300)
def get_hourly_patterns(_session, start_dt, end_dt):
    """Get average traffic by hour of day"""
    query = f"""
    SELECT 
        HOUR(hour) as hour_of_day,
        SUM(aircraft_count) as avg_aircraft,
        SUM(data_points) as total_points
    FROM {db_prefix}.FLIGHT_TRAFFIC_FACT_ADSB_HOURLY
    WHERE hour BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
    GROUP BY hour_of_day
    ORDER BY hour_of_day
    """
    return _session.sql(query).to_pandas()

@st.cache_data(ttl=300)
def get_day_of_week_patterns(_session, start_dt, end_dt):
    """Get traffic patterns by day of week"""
    query = f"""
    SELECT 
        DAYOFWEEK(date) as day_of_week,
        SUM(unique_aircraft) as aircraft_count,
        SUM(unique_flights) as flight_count
    FROM {db_prefix}.FLIGHT_TRAFFIC_FACT_ADSB_DAILY
    WHERE date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    GROUP BY day_of_week
    ORDER BY day_of_week
    """
    return _session.sql(query).to_pandas()

@st.cache_data(ttl=300)
def get_airline_traffic(_session, start_dt, end_dt):
    """Get traffic by airline"""
    query = f"""
    SELECT airline_code,
           SUM(aircraft_count) as aircraft_count,
           SUM(flight_count) as flight_count,
           SUM(data_points) as data_points
    FROM {db_prefix}.FLIGHT_TRAFFIC_FACT_AIRLINE_TRAFFIC_DAILY
    WHERE date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    GROUP BY airline_code
    ORDER BY data_points DESC
    LIMIT 15
    """
    return _session.sql(query).to_pandas()

@st.cache_data(ttl=300)
def get_airline_delay_stats(_session, start_dt, end_dt):
    """Aggregate delays by airline using FLIGHT_TRAFFIC_FACT_AIRLINE_DELAY_DAILY over the selected period."""
    query = f"""
    SELECT airline,
           SUM(total_delay_minutes) AS total_delay_minutes,
           SUM(delayed_flights) AS delayed_flights,
           SUM(total_early_minutes) AS total_early_minutes,
           SUM(early_flights) AS early_flights
    FROM {db_prefix}.FLIGHT_TRAFFIC_FACT_AIRLINE_DELAY_DAILY
    WHERE date BETWEEN '{start_dt}'::DATE AND '{end_dt}'::DATE
    GROUP BY airline
    ORDER BY total_delay_minutes DESC
    """
    return _session.sql(query).to_pandas()

# Load data
with st.spinner("Analyzing traffic patterns..."):
    # Coerce to string dates robustly to avoid transient tuple casting issues
    def _to_date_str(d):
        try:
            return str(pd.to_datetime(d).date())
        except Exception:
            return str(datetime.now().date())
    start_datetime = f"{_to_date_str(start_date)} 00:00:00"
    end_datetime = f"{_to_date_str(end_date)} 23:59:59"
    
    if granularity.lower() == "daily":
        traffic_data = get_hourly_traffic(session, start_datetime, end_datetime)
        time_col = 'HOUR'
    else:
        traffic_data = get_daily_traffic(session, start_datetime, end_datetime)
        time_col = 'DATE'
    
    hourly_patterns = get_hourly_patterns(session, start_datetime, end_datetime)
    dow_patterns = get_day_of_week_patterns(session, start_datetime, end_datetime)
    
    if show_airlines:
        airline_data = get_airline_traffic(session, start_datetime, end_datetime)
        delay_stats = get_airline_delay_stats(session, start_date, end_date)
        code_to_name = utils.get_airline_name_map(session, start_date, end_date)

# (Traffic Summary removed)

# Traffic over time
st.subheader(f"📅 Traffic Trend - {granularity}")

if not traffic_data.empty:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    
    if granularity.lower() == "daily":
        fig.add_trace(
            go.Scatter(
                x=traffic_data['HOUR'],
                y=traffic_data['AIRCRAFT_COUNT'],
                name="Aircraft Count",
                line=dict(color='#4FC3F7', width=3),
                fill='tozeroy',
                fillcolor='rgba(79, 195, 247, 0.2)'
            ),
            secondary_y=False
        )
        
        fig.update_xaxes(title_text="Time")
        fig.update_yaxes(title_text="Number of Aircraft", secondary_y=False)
        
    else:  # weekly
        fig.add_trace(
            go.Scatter(
                x=traffic_data['DATE'],
                y=traffic_data['UNIQUE_AIRCRAFT'],
                name="Unique Aircraft",
                line=dict(color='#4FC3F7', width=3),
                fill='tozeroy',
                fillcolor='rgba(79, 195, 247, 0.2)'
            ),
            secondary_y=False
        )
        
        fig.add_trace(
            go.Scatter(
                x=traffic_data['DATE'],
                y=traffic_data['UNIQUE_FLIGHTS'],
                name="Unique Flights",
                line=dict(color='#81C784', width=3)
            ),
            secondary_y=True
        )
        
        fig.update_xaxes(title_text="Date")
        fig.update_yaxes(title_text="Unique Aircraft", secondary_y=False)
        fig.update_yaxes(title_text="Unique Flights", secondary_y=True)
    
    fig.update_layout(
        hovermode='x unified',
        height=450,
        template='plotly_white',
        showlegend=True
    )
    
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# Hourly patterns and day of week patterns
col1, col2 = st.columns(2)

with col1:
    st.subheader("🕐 Traffic by Hour of Day")
    
    if not hourly_patterns.empty:
        # Create bar chart
        fig = go.Figure()
        
        fig.add_trace(go.Bar(
            x=hourly_patterns['HOUR_OF_DAY'],
            y=hourly_patterns['AVG_AIRCRAFT'],
            marker_color='#4FC3F7',
            name='Aircraft Count'
        ))
        
        fig.update_layout(
            xaxis_title="Hour of Day (24h)",
            yaxis_title="Average Aircraft Count",
            height=400,
            template='plotly_white',
            showlegend=False
        )
        
        st.plotly_chart(fig, use_container_width=True)
        
        # Find peak hour
        peak_hour = hourly_patterns.loc[hourly_patterns['AVG_AIRCRAFT'].idxmax()]
        st.info(f"🔝 Peak Hour: **{int(peak_hour['HOUR_OF_DAY']):02d}:00** with **{int(peak_hour['AVG_AIRCRAFT']):,}** aircraft")

with col2:
    st.subheader("📆 Traffic by Day of Week")
    
    if not dow_patterns.empty:
        # Map day numbers to names
        day_names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
        dow_patterns['DAY_NAME'] = dow_patterns['DAY_OF_WEEK'].apply(lambda x: day_names[int(x)])
        
        fig = go.Figure()
        
        fig.add_trace(go.Bar(
            x=dow_patterns['DAY_NAME'],
            y=dow_patterns['AIRCRAFT_COUNT'],
            marker_color='#81C784',
            name='Aircraft Count'
        ))
        
        fig.update_layout(
            xaxis_title="Day of Week",
            yaxis_title="Aircraft Count",
            height=400,
            template='plotly_white',
            showlegend=False
        )
        
        st.plotly_chart(fig, use_container_width=True)
        
        # Find busiest day
        busiest_day = dow_patterns.loc[dow_patterns['AIRCRAFT_COUNT'].idxmax()]
        st.info(f"🔝 Busiest Day: **{busiest_day['DAY_NAME']}** with **{int(busiest_day['AIRCRAFT_COUNT']):,}** aircraft")

# Activity heatmap
if show_heatmap and not traffic_data.empty:
    st.divider()
    st.subheader("🔥 Activity Heatmap")
    
    # Get data for heatmap
    @st.cache_data(ttl=300)
    def get_heatmap_data(_session, start_dt, end_dt):
        query = f"""
        SELECT 
            HOUR(TIMESTAMP) as hour,
            DAYOFWEEK(TIMESTAMP) as day_of_week,
            COUNT(DISTINCT ICAO_HEX) as aircraft_count
    FROM {db_prefix}.ADSB_DATA_LOCAL
        WHERE TIMESTAMP BETWEEN '{start_dt}'::TIMESTAMP AND '{end_dt}'::TIMESTAMP
        GROUP BY hour, day_of_week
        ORDER BY day_of_week, hour
        """
        return _session.sql(query).to_pandas()
    
    heatmap_data = get_heatmap_data(session, start_datetime, end_datetime)
    
    if not heatmap_data.empty:
        # Pivot for heatmap
        day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        heatmap_data['DAY_NAME'] = heatmap_data['DAY_OF_WEEK'].apply(lambda x: day_names[int(x)])
        
        heatmap_pivot = heatmap_data.pivot_table(
            values='AIRCRAFT_COUNT',
            index='DAY_NAME',
            columns='HOUR',
            fill_value=0
        )
        
        # Reorder days
        heatmap_pivot = heatmap_pivot.reindex(day_names)
        
        fig = go.Figure(data=go.Heatmap(
            z=heatmap_pivot.values,
            x=[f"{h:02d}:00" for h in heatmap_pivot.columns],
            y=heatmap_pivot.index,
            colorscale='Blues',
            hovertemplate='%{y}<br>%{x}<br>Aircraft: %{z}<extra></extra>'
        ))
        
        fig.update_layout(
            xaxis_title="Hour of Day",
            yaxis_title="Day of Week",
            height=400,
            template='plotly_white'
        )
        
        st.plotly_chart(fig, use_container_width=True)

# Airline breakdown
if show_airlines and 'airline_data' in locals() and not airline_data.empty:
    st.divider()
    st.subheader("🏢 Top Airlines by Activity")
    
    # Add airline names
    airline_data['AIRLINE_NAME'] = airline_data['AIRLINE_CODE'].apply(lambda c: code_to_name.get(str(c), str(c)))
    if hide_unknown_airlines:
        airline_data = airline_data[airline_data['AIRLINE_CODE'].astype(str) != 'UNK']
    
    col1, col2 = st.columns(2)
    
    with col1:
        # Flights by airline
        fig = go.Figure(go.Bar(
            x=airline_data['FLIGHT_COUNT'],
            y=airline_data['AIRLINE_NAME'],
            orientation='h',
            marker_color='#FF6B6B'
        ))
        
        fig.update_layout(
            title="Flights by Airline",
            xaxis_title="Number of Flights",
            yaxis_title="",
            height=500,
            template='plotly_white',
            yaxis={'categoryorder':'total ascending'}
        )
        
        st.plotly_chart(fig, use_container_width=True)
    
    with col2:
        # Market share pie chart
        fig = go.Figure(data=[go.Pie(
            labels=airline_data['AIRLINE_NAME'],
            values=airline_data['FLIGHT_COUNT'],
            hole=.3
        )])
        
        fig.update_layout(
            title="Market Share by Flights",
            height=500,
            template='plotly_white'
        )
        
        st.plotly_chart(fig, use_container_width=True)

    # Delay analytics by airline
    if delay_stats is not None and not delay_stats.empty:
        st.divider()
        # Row 1: Delays (minutes) and Early Flights side-by-side
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("⏱️ Delays by Airline (Total Minutes)")
            # Ensure full carrier names: if already a long name, keep/title-case; otherwise map code→name
            def _to_full_airline(val: str) -> str:
                s = str(val).strip()
                # Heuristic: names often longer than 3 or contain spaces/slashes
                if len(s) > 3 or (' ' in s) or ('AIR' in s) or ('AIRLINES' in s) or ('LINES' in s):
                    return s.title()
                return code_to_name.get(s, s)
            delay_stats['AIRLINE_NAME'] = delay_stats['AIRLINE'].apply(_to_full_airline)
            d1 = delay_stats[['AIRLINE_NAME','TOTAL_DELAY_MINUTES']].copy()
            d1 = d1.sort_values('TOTAL_DELAY_MINUTES', ascending=False).head(15)
            fig_d1 = go.Figure(go.Bar(
                x=d1['TOTAL_DELAY_MINUTES'],
                y=d1['AIRLINE_NAME'],
                orientation='h',
                marker_color='#EF5350'
            ))
            fig_d1.update_layout(xaxis_title='Total Delay Minutes', yaxis_title='Airline', height=450, template='plotly_white')
            st.plotly_chart(fig_d1, use_container_width=True)
        with col2:
            st.subheader("🛬 Early Flights by Airline")
            e2 = delay_stats[['AIRLINE_NAME','EARLY_FLIGHTS']].copy().sort_values('EARLY_FLIGHTS', ascending=False).head(15)
            fig_e2 = go.Figure(go.Bar(x=e2['EARLY_FLIGHTS'], y=e2['AIRLINE_NAME'], orientation='h', marker_color='#43A047'))
            fig_e2.update_layout(xaxis_title='Early Flights', yaxis_title='Airline', height=450, template='plotly_white')
            st.plotly_chart(fig_e2, use_container_width=True)

        # Row 2: Delayed Flights and Early Minutes side-by-side (existing pairing)
        colA, colB = st.columns(2)
        with colA:
            st.subheader("✈️ Delayed Flights by Airline")
            d2 = delay_stats[['AIRLINE_NAME','DELAYED_FLIGHTS']].copy().sort_values('DELAYED_FLIGHTS', ascending=False).head(15)
            fig_d2 = go.Figure(go.Bar(x=d2['DELAYED_FLIGHTS'], y=d2['AIRLINE_NAME'], orientation='h', marker_color='#F57C00'))
            fig_d2.update_layout(xaxis_title='Delayed Flights', yaxis_title='Airline', height=450, template='plotly_white')
            st.plotly_chart(fig_d2, use_container_width=True)
        with colB:
            st.subheader("⏰ Early Arrivals by Airline (Minutes)")
            e1 = delay_stats[['AIRLINE_NAME','TOTAL_EARLY_MINUTES']].copy().sort_values('TOTAL_EARLY_MINUTES', ascending=False).head(15)
            fig_e1 = go.Figure(go.Bar(x=e1['TOTAL_EARLY_MINUTES'], y=e1['AIRLINE_NAME'], orientation='h', marker_color='#66BB6A'))
            fig_e1.update_layout(xaxis_title='Early Minutes', yaxis_title='Airline', height=450, template='plotly_white')
            st.plotly_chart(fig_e1, use_container_width=True)

st.divider()

# Flight Details section moved from Schedule Performance
@st.cache_data(ttl=300)
def get_schedule_vs_actual(_session, date):
    query = f"""
    WITH airport AS (
        SELECT UPPER(airport_code) AS airport_code
        FROM {db_prefix}.PROPERTIES_AIRPORT
        LIMIT 1
    ),
    schedule AS (
        SELECT 
            fs.FLIGHT_NUMBER,
            fs.FLIGHT_DATE AS TRAVEL_DATE,
            fs.AIRLINE_NAME AS MARKETING_CARRIER,
            IFF(UPPER(fs.DEPARTURE_AIRPORT) = a.airport_code, 'departure',
                IFF(UPPER(fs.ARRIVAL_AIRPORT) = a.airport_code, 'arrival', 'unknown')) AS DIRECTION,
            fs.DEPARTURE_AIRPORT AS ORIGIN_AIRPORT,
            fs.ARRIVAL_AIRPORT AS DESTINATION_AIRPORT,
            IFF(UPPER(fs.DEPARTURE_AIRPORT) = a.airport_code, fs.DEPARTURE_SCHEDULED, fs.ARRIVAL_SCHEDULED) AS SCHEDULED_TIME
        FROM {db_prefix}.FLIGHT_SCHEDULE fs
        CROSS JOIN airport a
        WHERE fs.FLIGHT_DATE = '{date}'::DATE
    ),
    actual AS (
        SELECT 
            REGEXP_SUBSTR(FLIGHT, '[0-9]+') AS FLIGHT_NUMBER,
            MIN(TIMESTAMP) AS FIRST_SEEN
    FROM {db_prefix}.ADSB_DATA_LOCAL
        WHERE TIMESTAMP::DATE = '{date}'::DATE
          AND FLIGHT IS NOT NULL
        GROUP BY 1
    )
    SELECT 
        s.FLIGHT_NUMBER,
        s.MARKETING_CARRIER,
        s.DIRECTION,
        s.ORIGIN_AIRPORT,
        s.DESTINATION_AIRPORT,
        s.SCHEDULED_TIME,
        a.FIRST_SEEN as ACTUAL_TIME,
        TIMESTAMPDIFF(MINUTE, s.SCHEDULED_TIME, a.FIRST_SEEN) as DELAY_MINUTES
    FROM schedule s
    LEFT JOIN actual a 
        ON TO_VARCHAR(s.FLIGHT_NUMBER) = TO_VARCHAR(a.FLIGHT_NUMBER)
    ORDER BY s.SCHEDULED_TIME
    """
    return _session.sql(query).to_pandas()

st.subheader("✈️ Flight Details")
flight_details = get_schedule_vs_actual(session, end_date)
if not flight_details.empty:
    # Classify by delay using threshold
    def classify_delay(delta_minutes, threshold):
        try:
            if pd.isna(delta_minutes):
                return "Unknown"
            if abs(float(delta_minutes)) <= float(threshold):
                return "On Time"
            return "Arrived Earlier" if float(delta_minutes) < -float(threshold) else "Arrived Later"
        except Exception:
            return "Unknown"

    flight_details = flight_details.copy()
    flight_details['STATUS'] = flight_details['DELAY_MINUTES'].apply(lambda d: classify_delay(d, delay_threshold))
    
    # Status filter
    status_options = ["On Time", "Arrived Earlier", "Arrived Later"]
    selected_status = st.multiselect(
        "Filter by status",
        options=status_options,
        default=status_options
    )
    if selected_status:
        flight_details = flight_details[flight_details['STATUS'].isin(selected_status)]
    else:
        flight_details = flight_details.iloc[0:0]
    
    display_cols = ['FLIGHT_NUMBER', 'MARKETING_CARRIER', 'DIRECTION', 'ORIGIN_AIRPORT', 
                   'DESTINATION_AIRPORT', 'SCHEDULED_TIME', 'ACTUAL_TIME', 'DELAY_MINUTES', 'STATUS']
    display_df = flight_details[display_cols].copy()
    display_df['SCHEDULED_TIME'] = display_df['SCHEDULED_TIME'].dt.strftime('%H:%M')
    display_df['ACTUAL_TIME'] = display_df['ACTUAL_TIME'].dt.strftime('%H:%M')
    display_df.columns = ['Flight', 'Carrier', 'Direction', 'Origin', 'Destination', 
                         'Scheduled', 'Actual', 'Delay (min)', 'Status']
    st.dataframe(display_df, use_container_width=True, hide_index=True)

