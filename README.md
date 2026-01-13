# Airport Analytics Solution - Deployment Guide

A Snowflake-native solution for real-time aviation analytics using ADS-B flight tracking, flight schedules, and airport infrastructure data. Deploy complete per-airport analytics databases with automated pipelines and interactive dashboards.

## ✈️ Key Features

- **Real-time Flight Tracking**: Live ADS-B ingestion every minute
- **Historical Backfill**: Automated download of historical ADS-B data from GitHub releases
- **Flight Schedule Integration**: Hourly ingestion from Aviationstack API with automated matching
- **Gate Analytics**: Aircraft-to-gate proximity analysis with dwell time calculations
- **Runway Crossing Detection**: Identifies taxiing aircraft crossing runways
- **Infrastructure Visualization**: Dynamic rendering of runways, taxiways, gates, and terminals
- **Multi-Airport Support**: Deploy separate databases for multiple airports

![Live View](https://github.com/sfc-gh-obielov/sfguide-aviation-ops-intelligence/blob/main/assets/live_view.png)

---

## 📋 Prerequisites

### Snowflake Account Requirements

1. **Role**: ACCOUNTADMIN or equivalent with permissions to:
   - CREATE DATABASE, SCHEMA
   - CREATE EXTERNAL ACCESS INTEGRATION
   - CREATE SECRET
   - CREATE PROCEDURE (with Python handler)
   - CREATE TASK, DYNAMIC TABLE
   - CREATE STREAMLIT

2. **Warehouse**: 
   - M-L warehouse for Installer app and production data pipelines
   - M warehouse for Streamlit dashboard

3. **Snowflake Marketplace Listings** (free):
   - [Overture Maps - Base](https://app.snowflake.com/marketplace/listing/GZT0Z4CM1E9KV/carto-overture-maps-base)

### API Keys

1. **Aviationstack API Key (Optional)** (required for flight schedules)
   - Sign up at [aviationstack.com](https://aviationstack.com)
   - Basic tier (recommended): 10000 requests/month (sufficient for several airports)

---

## 🚀 Deployment 

Execute these queries in a Snowflake workworkspaces (replace placeholders):

```sql
-- Step 1: Set context (replace with your preferred database/schema)
CREATE DATABASE IF NOT EXISTS AVIA_INSTALLER;
-- Step 1: Set context (replace with your preferred database/schema)
USE ROLE ACCOUNTADMIN;
USE DATABASE AVIA_INSTALLER;
USE SCHEMA PUBLIC;

-- Step 2: Create API Integration for GitHub (if not exists)
-- Note: For PUBLIC repositories, no GitHub PAT is required
CREATE API INTEGRATION IF NOT EXISTS AVIA_INSTALLER.PUBLIC.github_api_integration
  API_PROVIDER = git_https_api
  API_ALLOWED_PREFIXES = ('https://github.com/sfc-gh-obielov/')
  ENABLED = TRUE;

-- Step 3: Create Git Repository Object (NO credentials needed for public repos)
CREATE OR REPLACE GIT REPOSITORY AVIA_INSTALLER.PUBLIC.avia_ops_repo
  API_INTEGRATION = github_api_integration
  ORIGIN = 'https://github.com/sfc-gh-obielov/sfguide-aviation-ops-intelligence';

-- Step 4: Fetch latest files from repository
ALTER GIT REPOSITORY avia_ops_repo FETCH;
```

**Note**: Replace `<your_warehouse_name>` with your warehouse (e.g., `COMPUTE_WH`). Replace `AVIA_INSTALLER.PUBLIC` with your chosen database and schema. Replace `<your_role>` with the role that needs access.

---

## 📖 Usage Workflow

### 1. Create the Installer App

Follow this video guide:
![Installer demo](https://github.com/sfc-gh-obielov/sfguide-aviation-ops-intelligence/blob/main/assets/installer.gif)

### 2. Install the solution
1. Open the **Installer** Streamlit app
2. Select an airport from the dropdown (searches Overture Maps international airports)
3. Configure settings:
   - **Database Name**: Auto-generated as `AIRPORT_XXX` (e.g., `AIRPORT_SAN` for San Diego)
   - **API Keys**: Provide Aviationstack API key if you have it. If not provided, Callsights from ADSB won't be matched with actual flights.
   - **Backfill Days**: Choose 0-30 days of historical data to load.
4. Click **Generate SQL** to review the deployment scripts
5. Click **Execute in Snowflake** to deploy the infrastructure

**What the Installer Creates:**
- Database: `AIRPORT_XXX` with schema `V5`
- Tables: Airport properties, ADS-B data, flight schedules, analytics tables
- Procedures: Data ingestion, enrichment, backfill
- Tasks: Automated pipelines (1-min ADS-B ingestion, hourly schedule/enrichment)
- Dynamic Tables: Incremental analytics (gate analysis, runway crossings, traffic facts)
- External Access Integrations: API access for ADSB.lol, GitHub, Aviationstack

### 2. Monitor Installation

After execution completes (~5-10 minutes):
- Check task status in the Installer app
- Wait for initial data ingestion (1-2 minutes for first ADS-B points)

### 3. Open the Dashboard

1. Create the **Dashboard** Streamlit app same way as installer, but use /dashboard/streamlit_app.py file
2. Select your airport from the dropdown (e.g., "San Diego International Airport (SAN)")
3. Explore the 8 dashboard pages:
   - **Flight Tracker**: Individual flight paths with altitude profiles
   - **Airport Activity**: Geographic heatmaps and traffic density
   - **Runway Crossings**: Taxiing aircraft crossing runway detection
   - **Traffic Analysis**: Temporal patterns and airline rankings
   - **Gate Analysis**: Gate utilization and dwell time metrics
   - **Operations**: Curated operational views
   - **Monitoring**: Data pipeline health and quality metrics
   - **Performance**: Query performance and warehouse utilization

### 4. Deploy Additional Airports

Repeat the installer workflow for each airport. Each airport gets its own database (`AIRPORT_XXX`) to avoid data collisions.

---

## 📁 Repository Structure

```
sd_poc/
├── installer/                    # Installer Streamlit app
│   ├── streamlit_app.py         # Main installer (4966 lines)
│   ├── airlines.csv             # Airline reference data
│   └── aviationstack_api_key.txt # API key file (not in Git)
│
├── dashboard/                    # Dashboard Streamlit app
│   ├── streamlit_app.py         # Main entry point
│   ├── utils.py                 # Shared utilities (1243 lines)
│   ├── pages/                   # 8 dashboard pages
│   │   ├── 1_Flight_Tracker.py
│   │   ├── 2_Airport_Activity.py
│   │   ├── 3_Runway_Crossings.py
│   │   ├── 4_Traffic_Analysis.py
│   │   ├── 5_Gate_Analysis.py
│   │   ├── 6_Operations.py
│   │   ├── 7_Monitoring.py
│   │   └── 8_Performance.py
│   └── images/                  # Dashboard assets
│
└──README.md                    # This deployment guide
```

**Key Files:**
- **installer/streamlit_app.py**: Generates and deploys SQL for airport infrastructure
- **dashboard/streamlit_app.py**: Main dashboard entry point 
- **dashboard/utils.py**: Shared utilities for airport selection, infrastructure rendering, time filters

---

## 🔧 Troubleshooting

### Common Issues

#### 1. "No airport databases found" in Dashboard

**Cause**: No `AIRPORT_XXX` databases with `V5.PROPERTIES_AIRPORT` table exist.

**Fix**: Run the Installer app first to deploy at least one airport.

#### 2. Installer execution fails with permission errors

**Cause**: Missing required privileges.

**Fix**: Ensure you have ACCOUNTADMIN role or equivalent with:
```sql
USE ROLE ACCOUNTADMIN;
-- Then re-run installer
```

#### 5. No data appearing in Dashboard

**Possible causes**:
- Real-time ingestion task not running
- Dynamic tables not refreshing
- Warehouse suspended

**Debug**:
```sql
-- Check if ADSB_DATA has recent data
SELECT COUNT(*), MAX(TIMESTAMP) 
FROM AIRPORT_<XXX>.V5.ADSB_DATA;

-- Check task state
SHOW TASKS IN SCHEMA AIRPORT_<XXX>.V5;

-- Check dynamic table state
SHOW DYNAMIC TABLES IN SCHEMA AIRPORT_<XXX>.V5;

-- Manually trigger ingestion
CALL AIRPORT_<XXX>.V5.PROC_INGEST_ADSB();
```



## 📚 Additional Resources
  
- **[Snowflake Streamlit Documentation](https://docs.snowflake.com/en/developer-guide/streamlit/about-streamlit)**: Official Streamlit in Snowflake docs

- **[Aviationstack API Docs](https://aviationstack.com/documentation)**: Flight schedule API reference

- **[ADSB.lol API](https://api.adsb.lol/)**: Real-time ADS-B data source

- **[Overture Maps](https://overturemaps.org/)**: Open-source geospatial data for airport infrastructure

---

## 🔒 Security Notes

- **Never commit** API keys or PAT tokens to Git
- Store secrets in Snowflake `SECRET` objects (as shown in Option 2)
- Rotate GitHub PATs regularly (recommend 90-day expiration)
- Use role-based access control (RBAC) for Streamlit apps in production

---

**Built with Snowflake Native Architecture**  
Version: V5 | Last Updated: January 2026
