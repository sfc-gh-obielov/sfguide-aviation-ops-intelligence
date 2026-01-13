"""
Dashboard Home

Uses Streamlit built-in multipage sidebar navigation (dashboard/pages/*).
We redirect to Flight Tracker for a "single entry" experience.
"""

import streamlit as st

import utils


st.set_page_config(page_title="Airport Analytics", page_icon="✈️", layout="wide")
utils.apply_custom_css()

# Enforce V5-only: if no installed airports exist, show guidance instead of redirecting.
airports = utils.get_available_airports()
if not airports:
    st.title("Airport Analytics (V5)")
    st.warning("No V5 airport databases found (no `AIRPORT_XXX.V5.PROPERTIES_AIRPORT`).")
    st.write("Run the installer Streamlit app first, then come back here.")
    st.stop()

st.switch_page("pages/1_Flight_Tracker.py")


