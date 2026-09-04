# dashboard.py - the swarm's window. Read-only: it opens files the swarm wrote and
# queries the account; it never drives an agent, and nothing it does can place an order.
#
#   streamlit run dashboard.py
import glob
import json
import os
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Worca", page_icon=":satellite:", layout="wide",
                   initial_sidebar_state="collapsed")

# The sidebar carries no controls any more; hide its expand arrow so the page reads
# as one surface rather than looking like something is missing.
st.markdown("<style>[data-testid='stSidebar'],[data-testid='stSidebarCollapsedControl']{display:none!important}</style>", unsafe_allow_html=True)

# A signal older than this is one the agent has stopped asserting - the bus expires it.
# Mirrors swarm.bus.DEFAULT_TTL_S so this panel agrees with what the executor can see.
SIGNAL_TTL_S = 600
REFRESH_MS = 30_000        # the page reloads itself; the swarm is always moving


# ---------------------------------------------------------------- loading
@st.cache_data(ttl=15)
def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # a torn last line while the swarm is mid-write
    return out


@st.cache_data(ttl=15)
def read_csv(path, names=None):
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path, names=names) if names else pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=15)
def read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _creds(env_file):
    """Alpaca credentials, from Streamlit secrets first, then a local .env file.

    Deployed on Streamlit Cloud there is no .env - the file is gitignored and must stay
    that way - so secrets are the only source. Locally there are no secrets, so the .env
    file is. Either may be absent: the dashboard's file-based panels read committed
    artifacts and do not need an account at all.
    """
    try:
        sec = st.secrets
        key = sec.get("ALPACA_API_KEY")
        if key:
            return key, sec.get("ALPACA_SECRET_KEY")
    except Exception:
        pass                                  # no secrets.toml - expected locally
    from dotenv import dotenv_values
    cfg = dotenv_values(env_file)
    return cfg.get("ALPACA_API_KEY"), cfg.get("ALPACA_SECRET_KEY")


@st.cache_data(ttl=30)
def account_state(env_file):
    """Live account and positions. Returns (info, positions_df, error)."""
    try:
        from alpaca.trading.client import TradingClient
        key, secret = _creds(env_file)
        if not key or not secret:
            return None, pd.DataFrame(), "no credentials configured"
        cli = TradingClient(key, secret, paper=True)
        a = cli.get_account()
        pos = [{"symbol": p.symbol, "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "market_value": float(p.market_value or 0),
                "unrealized_pl": float(p.unrealized_pl or 0)}
               for p in cli.get_all_positions()]
        return ({"account": a.account_number, "equity": float(a.equity),
                 "cash": float(a.cash), "options_level": a.options_trading_level,
                 "buying_power": float(a.options_buying_power or a.buying_power)},
                pd.DataFrame(pos), None)
    except Exception as e:
        return None, pd.DataFrame(), f"{type(e).__name__}: {e}"


def ts_col(df, col="ts"):
    df = df.copy()
    df[col] = pd.to_datetime(df[col], format="mixed", utc=True, errors="coerce")
    return df.dropna(subset=[col])


# ---------------------------------------------------------------- data source
# No run picker: this dashboard shows the submitted account and nothing else. A selector
# invites a viewer to switch to a development run and read numbers that were never part
# of the submission.
RUN = "results/submission"
ENV_FILE = ".env.submission"

runs = sorted(d.replace("\\", "/") for d in glob.glob("results/*") if os.path.isdir(d))
run = RUN if RUN in runs else (runs[-1] if runs else None)
env_file = ENV_FILE
if run is None:
    st.error("No runs under results/. Start the swarm: python main.py --tag submission")
    st.stop()

decisions = read_jsonl(f"{run}/oracle_log.jsonl")
trades = read_jsonl(f"{run}/trade_log.jsonl")
equity = read_csv(f"{run}/shadow_equity.csv")
book = read_json(f"{run}/shadow_book.json")

# Auto-refresh: judges open this and watch it move. The swarm writes artifacts
# continuously, so a static page would misrepresent a live system as a snapshot.
components.html(
    f"<script>setTimeout(function(){{window.parent.location.reload()}}, {REFRESH_MS})</script>",
    height=0)

st.title("Worca")
st.caption("Two specialist agents publish signals; an LLM Oracle allocates capital between "
           "them; every order passes the risk gates before it is sent. A shadow book "
           "records what each agent would have done with no Oracle at all.")

if not decisions:
    st.warning(f"No decisions logged yet in {run}/. The agents need one cycle to publish.")
    st.stop()

last = decisions[-1]

# ---------------------------------------------------------------- header
acct, positions, acct_err = account_state(env_file)
c = st.columns(5)
c[0].metric("Account", acct["account"] if acct else "unavailable",
            f"options L{acct['options_level']}" if acct else None)
c[1].metric("Equity", f"${acct['equity']:,.0f}" if acct else "-")
c[2].metric("Open positions", len(positions) if acct else "-")
c[3].metric("Oracle decisions", len(decisions),
            f"{sum(1 for d in decisions if d.get('fallback'))} fallback")
c[4].metric("Deployed", f"{last.get('deployed_fraction', 0):.0%}",
            f"{1 - last.get('deployed_fraction', 0):.0%} cash", delta_color="off")
if acct_err:
    st.info(f"Live account panel is off ({acct_err}). Everything below is rendered from the "
            "committed run artifacts in `results/`, which is the full audit trail.")

# Freshness banner. A judge should be able to tell at a glance whether they are looking
# at a running system or a committed snapshot, without taking our word for it.
age = None
try:
    age = (datetime.now(timezone.utc)
           - pd.to_datetime(last.get("ts"), utc=True)).total_seconds()
except Exception:
    pass
cols = st.columns([2, 3])
with cols[0]:
    if age is not None and age < 600:
        st.success(f"LIVE — last Oracle decision {int(age)}s ago")
    elif age is not None:
        st.warning(f"Snapshot — last Oracle decision {age / 3600:.1f}h ago "
                   f"(the swarm is not currently writing to this run)")
with cols[1]:
    st.caption(f"Run `{run}` · {len(decisions)} decisions · {len(trades)} order attempts · "
               f"page reloads every {REFRESH_MS // 1000}s")

tabs = st.tabs(["Signals", "Oracle", "Orders & risk", "Swarm vs solo", "History"])

# ---------------------------------------------------------------- 1. signals
with tabs[0]:
    st.subheader("What the agents are asserting right now")
    st.caption("strength is NOT comparable across agents - each reports conviction on its "
               "own scale, named in strength_basis. The Oracle is told this explicitly so "
               "it allocates on regime and corroboration, never on whose number is bigger.")

    inputs = last.get("inputs", {})
    if not inputs:
        st.info("No signals on the bus.")
    for sid, s in inputs.items():
        age = s.get("age_bucket_s", 0) or 0
        with st.container(border=True):
            head, body = st.columns([1, 3])
            with head:
                st.markdown(f"### {sid}")
                st.markdown(f"**{s.get('direction', '?').upper()}** - "
                            f"{s.get('signal_type', '')}")
                if age > SIGNAL_TTL_S:
                    st.error(f"STALE - {age / 3600:.1f}h old, expired off the bus")
                else:
                    st.success(f"live - {age}s old")
                st.progress(min(float(s.get("strength", 0)), 1.0),
                            text=f"strength {s.get('strength', 0):.2f} "
                                 f"({s.get('strength_basis', '?')})")
            with body:
                d = s.get("data", {})
                g = st.columns(4)
                if s.get("agent") == "gamma_scout":
                    g[0].metric("Wall", f"{d.get('wall_strike', 0):g}")
                    g[1].metric("Flip point",
                                f"{d['flip_point']:.2f}" if d.get("flip_point")
                                else "undefined")
                    g[2].metric("Distance to wall", f"{d.get('distance_pct', 0):.2%}")
                    g[3].metric("Net GEX", f"${d.get('net_gex', 0) / 1e9:+.2f}B",
                                d.get("regime_hint", ""), delta_color="off")
                    st.caption(f"Percentile ranked against {d.get('pctile_window_n', 0)} "
                               f"observations - dealer assumption "
                               f"{d.get('dealer_assumption', 'n/a')}")
                else:
                    g[0].metric("Front IV", f"{d.get('front_iv', 0):.2%}",
                                f"{d.get('front_dte', 0)} DTE", delta_color="off")
                    g[1].metric("Back IV", f"{d.get('back_iv', 0):.2%}",
                                f"{d.get('back_dte', 0)} DTE", delta_color="off")
                    g[2].metric("Spread", f"{d.get('spread', 0):+.4f}")
                    g[3].metric("z-score", f"{d.get('zscore', 0):+.2f}")
                    st.caption(f"Calendar: sell {d.get('front_exp', '?')} / buy "
                               f"{d.get('back_exp', '?')} at strike "
                               f"{d.get('atm_strike', '?')} - z over "
                               f"{d.get('zscore_window_n', 0)} observations")

# ---------------------------------------------------------------- 2. oracle
with tabs[1]:
    st.subheader("Oracle")
    if last.get("fallback"):
        st.error(f"FALLBACK - {last.get('fallback_reason', '')}. Holding the last good "
                 "allocation rather than crashing or going flat.")
    st.markdown(f"**Regime:** {last.get('regime', '-')}")
    st.info(last.get("reasoning", "-"))
    st.caption(f"model {last.get('model', '?')} - {last.get('latency_ms', 0):,}ms - "
               f"{last.get('ts', '')}")

    rows = []
    for d in decisions:
        acts, mdl = d.get("allocations") or {}, d.get("model_allocations") or {}
        for sid in set(acts) | set(mdl):
            rows.append({"ts": d.get("ts"), "signal": sid,
                         "acted on": acts.get(sid, 0.0),
                         "model said": mdl.get(sid, 0.0)})
    alloc_df = ts_col(pd.DataFrame(rows)) if rows else pd.DataFrame()

    st.markdown("#### Allocation: what the model said vs what was acted on")
    st.caption("A hosted mixture-of-experts at temperature=0 is not bit-deterministic. On "
               "2026-09-03 three consecutive cycles with materially identical inputs "
               "produced 0.35 / 0.00 / 0.35. A deadband plus confirmed-drop hysteresis "
               "damps that, and the raw model output is kept so the damping stays "
               "auditable rather than hidden. Gaps between the lines are the damping.")
    if not alloc_df.empty:
        long = alloc_df.melt(id_vars=["ts", "signal"], var_name="series",
                             value_name="alloc")
        st.altair_chart(
            alt.Chart(long).mark_line(interpolate="step-after").encode(
                x=alt.X("ts:T", title=None),
                y=alt.Y("alloc:Q", title="fraction of portfolio"),
                color=alt.Color("signal:N", title=None),
                strokeDash=alt.StrokeDash("series:N", title=None),
                tooltip=["ts:T", "signal:N", "series:N", "alloc:Q"],
            ).properties(height=280), width="stretch")

    st.markdown("#### Deployed vs cash held")
    st.caption("Clamping each allocation to [0.1, 0.7] and normalising only when the total "
               "exceeds 1.0 means the swarm can deliberately hold cash. This makes that "
               "choice visible instead of silent.")
    dep = ts_col(pd.DataFrame([{"ts": d.get("ts"),
                                "deployed": d.get("deployed_fraction", 0)}
                               for d in decisions]))
    if not dep.empty:
        dep["cash held"] = 1 - dep["deployed"]
        st.altair_chart(
            alt.Chart(dep.melt(id_vars="ts", var_name="k", value_name="v"))
            .mark_area().encode(
                x=alt.X("ts:T", title=None),
                y=alt.Y("v:Q", stack="normalize", title=None,
                        axis=alt.Axis(format="%")),
                color=alt.Color("k:N", title=None,
                                scale=alt.Scale(domain=["deployed", "cash held"],
                                                range=["#2a9d8f", "#e9ecef"])),
                tooltip=["ts:T", "k:N", "v:Q"],
            ).properties(height=160), width="stretch")

    st.markdown("#### Decision log")
    st.caption("Every decision is appended to oracle_log.jsonl, fallbacks included. "
               "This file is the explainability artefact.")
    st.dataframe(pd.DataFrame([{
        "ts": d.get("ts"), "regime": d.get("regime"),
        "deployed": d.get("deployed_fraction"), "fallback": d.get("fallback"),
        "latency_ms": d.get("latency_ms"), "reasoning": d.get("reasoning"),
    } for d in reversed(decisions)]), width="stretch", hide_index=True)

# ---------------------------------------------------------------- 3. orders
with tabs[2]:
    st.subheader("Orders and the gates they passed or failed")
    st.caption("RiskManager.check evaluates EVERY gate rather than stopping at the first "
               "failure, so a rejected order carries the complete reason it was rejected - "
               "which is what this table shows.")

    if acct and not positions.empty:
        st.markdown("#### Live positions")
        st.dataframe(positions, width="stretch", hide_index=True)
    elif acct:
        st.info("No open positions on this account.")

    if trades:
        counts = pd.Series([t.get("event") for t in trades]).value_counts()
        cols = st.columns(max(len(counts), 1))
        for col, (ev, n) in zip(cols, counts.items()):
            col.metric(ev, n)

        st.markdown("#### Order log")
        st.dataframe(pd.DataFrame([{
            "ts": t.get("ts"), "event": t.get("event"), "signal": t.get("signal_id"),
            "strategy": t.get("strategy"), "qty": t.get("qty"),
            "net_debit": t.get("net_debit"), "limit": t.get("limit_price"),
            "reasons": ", ".join(t.get("reasons") or []) or "-",
            "legs": " / ".join(f"{l['side']} {l['symbol']}"
                               for l in (t.get("legs") or [])),
        } for t in reversed(trades)]), width="stretch", hide_index=True)

        rej = [r for t in trades for r in (t.get("reasons") or [])]
        if rej:
            st.markdown("#### Why orders were refused")
            gate = pd.Series([r.split(":")[0].split(" ")[0] for r in rej]).value_counts()
            gdf = gate.reset_index()
            gdf.columns = ["gate", "n"]
            st.altair_chart(
                alt.Chart(gdf).mark_bar().encode(
                    x="n:Q", y=alt.Y("gate:N", sort="-x", title=None),
                    tooltip=["gate:N", "n:Q"]).properties(height=240),
                width="stretch")
    else:
        st.info("No orders logged yet in this run.")

# ---------------------------------------------------------------- 4. swarm vs solo
with tabs[3]:
    st.subheader("Swarm vs solo")
    st.caption("The solo book opens a position for EVERY signal each agent emits, "
               "regardless of allocation - that is the no-Oracle baseline. Both books use "
               "the same intent builder and the same exit rules (swarm/exits.py), so the "
               "only difference between them is the Oracle's allocation.")

    if not equity.empty and "iso" in equity:
        eq = ts_col(equity, "iso")
        st.altair_chart(
            alt.Chart(eq).mark_line(color="#e76f51").encode(
                x=alt.X("iso:T", title=None),
                y=alt.Y("equity:Q", title="solo equity ($)",
                        scale=alt.Scale(zero=False)),
                tooltip=["iso:T", "equity:Q", "realized:Q", "unrealized:Q"],
            ).properties(height=320), width="stretch")

        m = st.columns(4)
        m[0].metric("Solo equity", f"${eq['equity'].iloc[-1]:,.0f}",
                    f"{eq['equity'].iloc[-1] - 100_000:+,.0f}")
        m[1].metric("Solo realized", f"${eq['realized'].iloc[-1]:+,.0f}")
        m[2].metric("Solo unrealized", f"${eq['unrealized'].iloc[-1]:+,.0f}")
        m[3].metric("Swarm equity", f"${acct['equity']:,.0f}" if acct else "-",
                    f"{acct['equity'] - 100_000:+,.0f}" if acct else None)
    else:
        st.info("No shadow equity recorded yet in this run.")

    if book.get("positions"):
        st.markdown("#### Solo book")
        st.caption("Exits follow B2: gamma_scout +/-50% of premium, 2% from the wall, or "
                   "5 DTE; vol_surfer |z| back inside 0.5, -100% of the debit, or 7 DTE on "
                   "the front leg; both force-close inside the expiry-week blackout.")
        st.dataframe(pd.DataFrame([{
            "signal": p["signal_id"], "strategy": p["strategy"], "qty": p["qty"],
            "open": p["open"], "entry_debit": round(p["entry_debit"], 2),
            "mark": round(p["mark"], 2), "pnl": round(p["pnl"], 2),
            "exit_reason": p.get("exit_reason") or "-",
        } for p in book["positions"]]), width="stretch", hide_index=True)

# ---------------------------------------------------------------- 5. history
with tabs[4]:
    st.subheader("Agent history")
    st.caption("Both agents rank today's reading against their own recent observations, "
               "sampled every 15 minutes rather than every poll, so the window measures "
               "rarity rather than intraday drift. With days rather than months of history "
               "these ranks are indicative, not distributional claims.")
    for path, label in [("gex_history.csv", "Gamma Scout - wall GEX"),
                        ("volspread_history.csv", "Vol Surfer - IV spread")]:
        h = read_csv(path, names=["underlying", "ts", "value"])
        if h.empty:
            st.info(f"No {path} yet.")
            continue
        h["ts"] = pd.to_datetime(h["ts"], unit="s", utc=True)
        st.markdown(f"#### {label}")
        st.altair_chart(
            alt.Chart(h).mark_line(point=True).encode(
                x=alt.X("ts:T", title=None),
                y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=False)),
                color=alt.Color("underlying:N", title=None),
                tooltip=["ts:T", "underlying:N", "value:Q"],
            ).properties(height=220), width="stretch")
        st.caption(f"{len(h)} observations - " + " - ".join(
            f"{u}: {n}" for u, n in h["underlying"].value_counts().items()))

st.divider()
st.caption(f"Last Oracle decision {last.get('ts', '?')} · "
           f"rendered {datetime.now(timezone.utc).isoformat(timespec='seconds')} · "
           f"source `{run}`")
