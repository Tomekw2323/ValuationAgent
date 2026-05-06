"""Streamlit UI for Valuation Agent AI.

Run from the project directory:
    streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from config import MODEL, HAS_LLM_CREDENTIALS

st.set_page_config(page_title="Valuation Agent", page_icon="📊", layout="wide")

if not HAS_LLM_CREDENTIALS:
    st.error("Missing API credentials in .env (OPENAI_API_KEY or AZURE_OPENAI_*).")
    st.stop()

for _k, _v in [("running", False), ("wyniki", None), ("last_params", None)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

with st.sidebar:
    st.title("⚙️ Parameters")
    ticker_raw = st.text_input("Ticker", placeholder="e.g. AAPL, PKN.WA")
    wacc_pct   = st.slider("WACC (%)", 5.0, 20.0, 10.0, 0.5)
    growth_pct = st.slider("FCF growth (%)", 0.0, 25.0, 5.0, 1.0)
    clicked    = st.button("▶ Run valuation", type="primary",
                           use_container_width=True, disabled=st.session_state.running)
    st.caption(f"Data: yfinance | Model: {MODEL}")

if clicked:
    ticker = ticker_raw.strip().upper()
    if not ticker:
        st.sidebar.warning("Enter a ticker symbol.")
    else:
        params = {"ticker": ticker, "wacc": wacc_pct, "growth": growth_pct}
        if st.session_state.wyniki is None or params != st.session_state.last_params:
            st.session_state.running = True
            st.session_state.last_params = params
            st.session_state.wyniki = None
            st.rerun()
        else:
            st.sidebar.info("Results already loaded.")

if st.session_state.running:
    ticker = st.session_state.last_params["ticker"]
    selected_wacc = float(st.session_state.last_params.get("wacc", 10.0)) / 100.0
    selected_growth = float(st.session_state.last_params.get("growth", 5.0)) / 100.0
    with st.spinner(f"Running analysis for {ticker}..."):
        from agent.orchestrator import ValuationAgent
        from tools.data_fetcher import get_financial_data
        try:
            fd = get_financial_data(ticker)
            if fd is None:
                st.error(f'No data for "{ticker}". '
                         "Check the symbol (GPW: use .WA, e.g. PKN.WA).")
            else:
                agent = ValuationAgent()
                wstepne = agent.run_preliminary(ticker)
                if wstepne is None:
                    st.error("Could not prepare valuation inputs.")
                    report = None
                else:
                    assumptions = {
                        "wacc": selected_wacc,
                        "growth": selected_growth,
                        "wide_moat": False,
                        "buyback": True,
                        "cykliczna": (wstepne.get("cv_fcf") or 0.0) > 0.4,
                        "peers": wstepne.get("peers") or [],
                    }
                    report = agent.run_final(
                        ticker,
                        assumptions=assumptions,
                        wstepne=wstepne,
                    )
                if report is None:
                    st.error("The agent failed to generate a report. Try again.")
                else:
                    info = (agent.financial_data_cache.get(ticker) or fd).get("info", {})
                    curr = (info.get("currency") or "USD").strip()
                    mp = (
                        agent.last_market_price
                        or info.get("currentPrice")
                        or info.get("regularMarketPrice")
                    )
                    dcf = agent.last_dcf_result or {}
                    mult = agent.last_multiples_result or {}
                    sens = agent.last_sensitivity_result or {}
                    st.session_state.wyniki = {
                        "ticker": ticker, "report": report, "curr": curr,
                        "mp": mp,
                        "dcf_price": dcf.get("cena_z_korekta_buyback") or dcf.get("cena_na_akcje"),
                        "mult_med": mult.get("mediana"), "mult": mult, "sens": sens,
                    }
        except Exception as e:
            st.error(f"Agent error: {e}")
            st.exception(e)
        finally:
            st.session_state.running = False
            st.rerun()

w = st.session_state.wyniki
if w is None:
    st.title("Valuation Agent AI")
    st.info("Enter a ticker in the sidebar and click **Run valuation**.")
    st.stop()

curr, mp, dcf_p, med = w["curr"], w["mp"], w["dcf_price"], w["mult_med"]
st.title(f"Valuation: {w['ticker']}")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Market price", f"{mp:,.2f} {curr}"    if mp    else "—")
c2.metric("DCF",            f"{dcf_p:,.2f} {curr}" if dcf_p else "—")
c3.metric("Multiples",       f"{med:,.2f} {curr}"   if med   else "—")
if dcf_p and mp and mp > 0:
    c4.metric("DCF vs market", f"{(dcf_p - mp) / mp * 100:+.1f}%")
else:
    c4.metric("DCF vs market", "—")

st.divider()

col_l, col_r = st.columns(2)

with col_l:
    st.subheader("DCF scenarios")
    scen     = w["sens"].get("scenariusze") or {}
    labels = ["Bear", "Base", "Bull"]
    keys   = ["pesymistyczny", "bazowy", "optymistyczny"]
    colors = ["#ef4444", "#6366f1", "#22c55e"]
    values = [float(scen.get(k) or 0) for k in keys]
    if any(values):
        fig = go.Figure(go.Bar(
            x=labels, y=values, marker_color=colors, width=0.5,
            text=[f"{v:,.2f}" for v in values], textposition="outside",
        ))
        if mp:
            fig.add_hline(y=mp, line_dash="dash", line_color="#f59e0b",
                          annotation_text=f"Market: {mp:,.2f} {curr}",
                          annotation_position="top right")
        fig.update_layout(
            height=360, showlegend=False, plot_bgcolor="white", paper_bgcolor="white",
            yaxis=dict(title=curr, range=[0, max(values + ([mp] if mp else [])) * 1.3],
                       tickformat=",.0f"),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No scenario data for chart.")

with col_r:
    st.subheader("Multiples breakdown")
    rows = []
    for label, klucz in [("P/E", "wycena_pe"), ("EV/EBITDA", "wycena_ev_ebitda"), ("P/BV", "wycena_pbv")]:
        blok = w["mult"].get(klucz)
        cena = blok.get("cena_na_akcje")    if isinstance(blok, dict) else None
        mnoz = blok.get("wartosc_mnoznika") if isinstance(blok, dict) else None
        rows.append({
            "Method":  label,
            "Multiple": f"{mnoz:.1f}x"       if mnoz else "—",
            "Value":    f"{cena:,.2f} {curr}" if cena else "—",
        })
    rows.append({"Method": "Median", "Multiple": "—",
                 "Value": f"{med:,.2f} {curr}" if med else "—"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

st.divider()

st.subheader("Agent narrative")
st.markdown(w["report"])

from datetime import datetime
st.download_button(
    label="Download report (.txt)", data=w["report"], mime="text/plain",
    file_name=f"valuation_{w['ticker']}_{datetime.now():%Y-%m-%d}.txt",
)

st.markdown("---")
st.caption("Educational demo only — not investment advice.")
