import os
import time
import requests
import pandas as pd
import streamlit as st


API_BASE = "https://googlemapscraper-production.up.railway.app"

st.set_page_config(
    page_title="Place Finder",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
        gap: 0.6rem;
    }

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        margin: 0 0 0.3rem 0;
        font-size: 0.9rem;
    }

    /* Filters heading */
    [data-testid="stSidebar"] h3 {
        font-size: 1.05rem !important;
        margin-bottom: 0.8rem !important;
    }

    /* Checkboxes */
    [data-testid="stSidebar"] [data-testid="stCheckbox"] {
        margin: 0.15rem 0;
        padding: 0.05rem 0;
    }

    [data-testid="stSidebar"] label {
        font-size: 0.92rem !important;
        margin-bottom: 0.05rem !important;
    }

    [data-testid="stSidebar"] [data-baseweb="checkbox"] {
        margin-top: 0.2rem;
    }

    [data-testid="stSidebar"] hr {
        margin: 0.5rem 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
    )

st.title("Place Finder")
st.caption("Discover businesses around any location and export the data as CSV.")

with st.sidebar:
    st.markdown("**Search**")
    place_type = st.text_input("Place type", value="gym", help="e.g. gym, hospital, car showroom, schools")
    location = st.text_input("Location", value="Latifabad, Hyderabad, Pakistan")
    search_btn = st.button("Search", type="primary", use_container_width=True)
    st.markdown("---")
    st.markdown("**Filters**")
    hide_no_website = st.checkbox("Hide without website", value=False)
    hide_no_phone = st.checkbox("Hide without phone", value=False)
    show_no_website = st.checkbox("Show only without website", value=False)
    show_no_phone = st.checkbox("Show only without phone", value=False)

if "results" not in st.session_state:
    st.session_state["results"] = None
if "stats" not in st.session_state:
    st.session_state["stats"] = None
if "request_id" not in st.session_state:
    st.session_state["request_id"] = None

if search_btn:
    if not place_type.strip() or not location.strip():
        st.error("Both place type and location are required.")
    else:
        with st.spinner("Searching... returning ALL results found"):
            try:
                t0 = time.perf_counter()
                resp = requests.post(
                    f"{API_BASE}/search",
                    json={
                        "query": place_type.strip(),
                        "location": location.strip(),
                    },
                    timeout=1800,
                )
                elapsed = time.perf_counter() - t0
                if resp.status_code != 200:
                    st.error(f"Backend error: {resp.status_code} - {resp.text}")
                else:
                    data = resp.json()
                    st.session_state["results"] = data.get("results", [])
                    st.session_state["stats"] = {
                        "total_results": data.get("total_results", 0),
                        "with_phone": data.get("with_phone", 0),
                        "with_website": data.get("with_website", 0),
                        "with_address": data.get("with_address", 0),
                        "elapsed_seconds": data.get("elapsed_seconds", 0),
                    }
                    st.session_state["request_id"] = data.get("request_id")
                    st.success(f"Done in {elapsed:.1f}s")
            except requests.exceptions.ConnectionError:
                st.error(f"Cannot reach backend at {API_BASE}.")
            except requests.exceptions.Timeout:
                st.error("Request timed out. Try a more specific location.")
            except Exception as e:
                st.error(f"Error: {e}")

stats = st.session_state["stats"]

if stats:
    st.subheader("Summary")
    cols = st.columns(4)
    cols[0].metric("Total", stats["total_results"])
    cols[1].metric("Phone", stats["with_phone"])
    cols[2].metric("Website", stats["with_website"])
    cols[3].metric("Time (s)", stats["elapsed_seconds"])

results = st.session_state["results"]
if results is not None:
    if len(results) == 0:
        st.warning(
            "No results found. Try a different place type or a broader location like 'Hyderabad, Pakistan'."
        )
    else:
        df = pd.DataFrame(results)

        def _has(val):
            return bool(str(val).strip()) if val is not None else False

        def _missing(val):
            return not _has(val)

        filtered = df
        if hide_no_website:
            filtered = filtered[filtered["website"].apply(_has)]
        if hide_no_phone:
            filtered = filtered[filtered["phone"].apply(_has)]
        if show_no_website:
            filtered = filtered[filtered["website"].apply(_missing)]
        if show_no_phone:
            filtered = filtered[filtered["phone"].apply(_missing)]

        st.subheader(f"Results ({len(filtered)} of {len(results)} places)")

        display_cols = [
            "name", "phone", "website", "address",
        ]
        existing_cols = [c for c in display_cols if c in filtered.columns]
        df_display = filtered[existing_cols].copy()
        st.dataframe(df_display, use_container_width=True, hide_index=True)

        st.markdown("---")
        col_dl, col_meta = st.columns([1, 2])
        with col_dl:
            rid = st.session_state["request_id"]
            st.download_button(
                label="Download CSV",
                data=df_display.to_csv(index=False).encode("utf-8"),
                file_name=f"places_{rid or 'results'}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with col_meta:
            st.caption(f"Request ID: {st.session_state['request_id']}")

st.markdown("---")
st.caption("Built with FastAPI + Streamlit. Search results may vary based on data source coverage.")
