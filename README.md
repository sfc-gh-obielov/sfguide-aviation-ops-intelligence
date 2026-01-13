# Airport Analytics Platform - Deployment Guide

A Snowflake-native solution for real-time aviation analytics using ADS-B flight tracking, flight schedules, and airport infrastructure data. Deploy complete per-airport analytics databases with automated pipelines and interactive dashboards.

## ✈️ Key Features

- **Real-time Flight Tracking**: Live ADS-B ingestion every minute
- **Historical Backfill**: Automated download of historical ADS-B data from GitHub releases
- **Flight Schedule Integration**: Hourly ingestion from Aviationstack API with automated matching
- **Gate Analytics**: Aircraft-to-gate proximity analysis with dwell time calculations
- **Runway Crossing Detection**: Identifies taxiing aircraft crossing runways
- **Infrastructure Visualization**: Dynamic rendering of runways, taxiways, gates, and terminals
- **Multi-Airport Support**: Deploy separate databases for multiple airports

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
   - Free tier: 100 requests/month (sufficient for 1-2 airports)
   - Paid tier recommended for production

<video src="https://github.com/sfc-gh-obielov/sfguide-aviation-ops-intelligence/blob/main/assets/installer.gif" controls></video>

![Installer demo](https://github.com/sfc-gh-obielov/sfguide-aviation-ops-intelligence/blob/main/assets/installer.gif)


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

-- Step 5: Create Streamlit App from Git Repository
CREATE OR REPLACE STREAMLIT AVIA_INSTALLER.PUBLIC.airport_analytics_installer
  ROOT_LOCATION = '@avia_ops_repo/branches/main/installer'
  MAIN_FILE = 'streamlit_app.py'
  QUERY_WAREHOUSE = <your_warehouse_name>  -- Replace with your warehouse
  TITLE = 'Airport Analytics Installer'
  COMMENT = 'Installer for Airport Analytics Platform - generates and deploys airport infrastructure';

  -- Step 6: Create Streamlit App from Git Repository
CREATE OR REPLACE STREAMLIT AVIA_INSTALLER.PUBLIC.airport_analytics_dashboard
  ROOT_LOCATION = '@avia_ops_repo/branches/main/dashboard'
  MAIN_FILE = 'streamlit_app.py'
  QUERY_WAREHOUSE = <your_warehouse_name> -- Replace with your warehouse
  TITLE = 'Airport Analytics Dashboard'
  COMMENT = 'Dashboard for Airport Analytics Platform';

-- Step 6: Grant permissions (if needed for non-ACCOUNTADMIN users)
GRANT USAGE ON STREAMLIT AVIA_INSTALLER.PUBLIC.airport_analytics_installer TO ROLE PUBLIC;
GRANT USAGE ON STREAMLIT AVIA_INSTALLER.PUBLIC.airport_analytics_dashboard TO ROLE PUBLIC;

-- Step 7: Verify Streamlit App creation
SHOW STREAMLITS LIKE 'airport_analytics_%';
```

**Note**: Replace `<your_warehouse_name>` with your warehouse (e.g., `COMPUTE_WH`). Replace `AVIA_INSTALLER.PUBLIC` with your chosen database and schema. Replace `<your_role>` with the role that needs access.

---

## 📖 Usage Workflow

### 1. Run the Installer

https://github.com/sfc-gh-obielov/sfguide-aviation-ops-intelligence/raw/refs/heads/main/assets/installer.mp4

1. Open the **Installer** Streamlit app
2. Select an airport from the dropdown (searches Overture Maps international airports)
3. Configure settings:
   - **Database Name**: Auto-generated as `AIRPORT_XXX` (e.g., `AIRPORT_SAN` for San Diego)
   - **API Keys**: Provide Aviationstack API key and GitHub PAT
   - **Backfill Days**: Choose 0-30 days of historical data to load. If not provided, Callsights from ADSB won't be matched with actual flights.
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
- Historical backfill runs daily at 2 AM UTC (or on-demand via procedures)

### 3. Open the Dashboard

1. Open the **Dashboard** Streamlit app
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
├── COMPREHENSIVE_README.md      # Technical documentation (1900+ lines)
├── README.md                    # This deployment guide
├── snowflake.yml               # Snowflake CLI config (optional)
└── old/                        # Legacy docs (can be ignored)
```

**Key Files:**
- **installer/streamlit_app.py**: Generates and deploys SQL for airport infrastructure
- **dashboard/streamlit_app.py**: Main dashboard entry point (redirects to Flight Tracker)
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

#### 3. "External Access Integration already exists" error

**Cause**: EAI names must be unique per airport. Installer uses `AIRPORT_XXX_V5_*_EAI` pattern.

**Fix**: This is expected if re-running installer. The installer uses `CREATE OR REPLACE` to handle this.

#### 4. Low schedule match rate (<30%) in Monitoring page

**Possible causes**:
- Aviationstack API key exhausted (check quota at aviationstack.com)
- Flight schedule ingestion task not running
- Enrichment task not running

**Fix**:
```sql
-- Check task status
USE DATABASE AIRPORT_<XXX>;
USE SCHEMA V5;
SHOW TASKS;

-- Resume suspended tasks
ALTER TASK TASK_FLIGHT_SCHEDULE_HOURLY RESUME;
ALTER TASK TASK_ENRICH_ADSB_HOURLY RESUME;

-- Manually trigger enrichment
CALL PROC_ENRICH_ADSB_WITH_SCHEDULE(24);  -- Enrich last 24 hours
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

#### 6. GitHub integration fails with "Repository not found"

**Cause**: API integration prefix doesn't match repository URL, or PAT lacks permissions.

**Fix**:
```sql
-- Verify API integration allowed prefixes
SHOW API INTEGRATIONS LIKE 'github_api_integration';

-- Verify Git repository
SHOW GIT REPOSITORIES;

-- Test repository access (fully qualified)
LS @AVIA_INSTALLER.PUBLIC.avia_fleet_repo/branches/poc-stable-v5;
```

If listing fails, regenerate your GitHub PAT with correct permissions and recreate the secret.

#### 7. Warehouse sizing issues

**Symptoms**: Slow queries, task failures, high credit consumption.

**Recommendations**:
- **Installer app**: XS-S warehouse (short-lived operations)
- **Dashboard app**: M-L warehouse (interactive queries)
- **Data pipeline tasks**: M-L warehouse (continuous ingestion)
- **Backfill task**: L-XL warehouse (large TAR file processing)

**Fix**:
```sql
-- Update Streamlit app warehouse (fully qualified)
ALTER STREAMLIT AVIA_INSTALLER.PUBLIC.airport_analytics_dashboard 
  SET QUERY_WAREHOUSE = <larger_warehouse>;

-- Update task warehouse (fully qualified)
ALTER TASK AIRPORT_<XXX>.V5.TASK_INGEST_ADSB 
  SET WAREHOUSE = <your_warehouse>;
ALTER TASK AIRPORT_<XXX>.V5.TASK_INGEST_ADSB RESUME;
```

---

## 📚 Additional Resources

- **[COMPREHENSIVE_README.md](COMPREHENSIVE_README.md)**: Complete technical documentation covering:
  - Architecture and data model
  - Detailed table/procedure reference
  - Data flow diagrams
  - Task orchestration
  - Advanced troubleshooting
  
- **[Snowflake Streamlit Documentation](https://docs.snowflake.com/en/developer-guide/streamlit/about-streamlit)**: Official Streamlit in Snowflake docs

- **[Aviationstack API Docs](https://aviationstack.com/documentation)**: Flight schedule API reference

- **[ADSB.lol API](https://api.adsb.lol/)**: Real-time ADS-B data source

- **[Overture Maps](https://overturemaps.org/)**: Open-source geospatial data for airport infrastructure

---

## 🔒 Security Notes

- **Never commit** API keys or PAT tokens to Git
- Store secrets in Snowflake `SECRET` objects (as shown in Option 2)
- Use `.gitignore` to exclude `aviationstack_api_key.txt` and `pat_*.txt` files
- Rotate GitHub PATs regularly (recommend 90-day expiration)
- Use role-based access control (RBAC) for Streamlit apps in production

---

## 🆘 Support

For issues or questions:
1. Check this README troubleshooting section
2. Review [COMPREHENSIVE_README.md](COMPREHENSIVE_README.md) for technical details
3. Check Monitoring page in Dashboard for data pipeline health
4. Review Snowflake task history: `SELECT * FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY())`

---

**Built with Snowflake Native Architecture**  
Version: V5 | Last Updated: January 2026
