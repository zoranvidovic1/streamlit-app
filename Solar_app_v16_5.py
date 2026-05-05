# -*- coding: utf-8 -*-
"""
ZV Consulting Forecast — PV Portfolio SaaS Dashboard
Multi-plant PV forecast with login/admin, weather provider architecture,
pvlib physical model, P10/P50/P90 scenarios, executive summary,
interactive charts, Excel/CSV/ZIP/PDF/HTML export.

Author: ZV Consulting
"""

import warnings
warnings.filterwarnings("ignore")

import platform
import uuid
import hashlib

import os
import io
import json
import zipfile
#import sqlite3
import tempfile
from datetime import datetime, timedelta

import bcrypt
import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pydeck as pdk

import pvlib
from pvlib.irradiance import get_total_irradiance, erbs
from pvlib.tracking import singleaxis
from pvlib.temperature import TEMPERATURE_MODEL_PARAMETERS, sapm_cell
from pvlib import atmosphere as pvatm
from pvlib.location import Location

try:
    from fpdf import FPDF
    HAVE_FPDF = True
except Exception:
    HAVE_FPDF = False

try:
    import plotly.io as pio
    HAVE_KALEIDO = True
except Exception:
    HAVE_KALEIDO = False

 

from supabase import create_client



supabase = create_client(
    st.secrets["SUPABASE_URL"],
    st.secrets["SUPABASE_KEY"]
)

# ==========================================================
# STREAMLIT CONFIG
# ==========================================================

st.set_page_config(
    page_title="ZV Consulting Forecast",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ==========================================================
# LOGIN / USERS DATABASE
# ==========================================================

# ==========================================================
# LOGIN / USERS DATABASE — SUPABASE VERSION
# ==========================================================

def get_machine_id() -> str:
    raw = f"{platform.node()}-{platform.system()}-{platform.machine()}-{uuid.getnode()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_user(username: str, password: str, days_valid: int, role: str = "user"):
    username = username.strip()

    created_at = datetime.now()
    expires_at = created_at + timedelta(days=int(days_valid))

    supabase.table("users").insert({
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "active": 1,
        "machine_id": None
    }).execute()


def ensure_default_admin():
    try:
        res = supabase.table("users").select("id").eq("username", "AdminZija").execute()

        if not res.data:
            create_user("AdminZija", "zokizija1990", 3650, role="admin")

    except Exception as e:
        st.error("Supabase greška pri kreiranju admin korisnika.")
        st.code(str(e))
        st.stop()

        


def authenticate(username: str, password: str):
    current_machine_id = get_machine_id()

    res = supabase.table("users").select("*").eq("username", username.strip()).execute()

    if not res.data:
        return False, "Korisnik ne postoji."

    user = res.data[0]

    if int(user.get("active", 0)) != 1:
        return False, "Nalog nije aktivan."

    expires_at = user.get("expires_at")
    if datetime.now() > datetime.fromisoformat(expires_at):
        return False, "Pristup je istekao."

    if not check_password(password, user["password_hash"]):
        return False, "Pogrešna lozinka."

    # 👉 ZA ADMINA PRESKOČI MACHINE LOCK
    if user.get("role") != "admin":

        machine_id = user.get("machine_id")

        if machine_id is None or str(machine_id).strip() == "":
            supabase.table("users").update({
                "machine_id": current_machine_id
            }).eq("username", user["username"]).execute()

            user["machine_id"] = current_machine_id

        elif machine_id != current_machine_id:
            # dozvoli login, ali prebaci na novi uređaj
            supabase.table("users").update({
                "machine_id": current_machine_id
            }).eq("username", user["username"]).execute()

            user["machine_id"] = current_machine_id

    return True, {
        "username": user["username"],
        "role": user["role"],
        "expires_at": user["expires_at"],
        "machine_id": user.get("machine_id")
    }


def get_users_df():
    res = supabase.table("users").select(
        "id, username, role, created_at, expires_at, active"
    ).order("id", desc=True).execute()

    df = pd.DataFrame(res.data)

    if not df.empty:
        df["created_at"] = pd.to_datetime(df["created_at"])
        df["expires_at"] = pd.to_datetime(df["expires_at"])
        df["days_left"] = (df["expires_at"] - pd.Timestamp.now()).dt.days
        df["status"] = np.where(
            df["active"].eq(1) & (df["expires_at"] > pd.Timestamp.now()),
            "ACTIVE",
            "INACTIVE / EXPIRED"
        )

    return df


def extend_user(username: str, extra_days: int):
    res = supabase.table("users").select("expires_at").eq("username", username).execute()

    if not res.data:
        raise ValueError("Korisnik ne postoji.")

    old_exp = datetime.fromisoformat(res.data[0]["expires_at"])
    base = max(old_exp, datetime.now())
    new_exp = base + timedelta(days=int(extra_days))

    supabase.table("users").update({
        "expires_at": new_exp.isoformat(),
        "active": 1
    }).eq("username", username).execute()


def set_user_active(username: str, active: int):
    supabase.table("users").update({
        "active": int(active)
    }).eq("username", username).execute()


def change_user_password(username: str, new_password: str):
    supabase.table("users").update({
        "password_hash": hash_password(new_password)
    }).eq("username", username).execute()


def reset_user_machine(username: str):
    supabase.table("users").update({
        "machine_id": None
    }).eq("username", username).execute()


def get_client_ip():
    try:
        headers = st.context.headers
        return (
            headers.get("x-forwarded-for", "").split(",")[0].strip()
            or headers.get("x-real-ip", "")
            or "unknown"
        )
    except Exception:
        return "unknown"


def get_browser_info():
    try:
        return st.context.headers.get("user-agent", "unknown")
    except Exception:
        return "unknown"
    

def create_session(username: str):
    try:
        session_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        machine_id = get_machine_id()

        supabase.table("user_sessions").update({
            "active": 0
        }).eq("username", username).execute()

        supabase.table("user_sessions").insert({
            "username": username,
            "session_id": session_id,
            "machine_id": machine_id,
            "ip_address": get_client_ip(),
            "browser_info": get_browser_info(),
            "created_at": now,
            "last_seen": now,
            "active": 1
        }).execute()

        return session_id

    except Exception as e:
        st.error("Greška u session sistemu")
        st.code(str(e))
        return None

def validate_active_session(timeout_minutes: int = 60):
    user = st.session_state.get("user", {})
    session_id = st.session_state.get("session_id", None)

    if not user or not session_id:
        return False

    res = supabase.table("user_sessions").select("*").eq(
        "session_id", session_id
    ).eq("active", 1).execute()

    if not res.data:
        return False

    sess = res.data[0]

    

    last_seen_str = sess.get("last_seen")

    if not last_seen_str:
        return False

    last_seen = datetime.fromisoformat(last_seen_str)
    if datetime.now() - last_seen > timedelta(minutes=timeout_minutes):
        supabase.table("user_sessions").update({
            "active": 0
        }).eq("session_id", session_id).execute()
        return False

    supabase.table("user_sessions").update({
        "last_seen": datetime.now().isoformat()
    }).eq("session_id", session_id).execute()

    return True


def logout_user():
    session_id = st.session_state.get("session_id", None)

    if session_id:
        supabase.table("user_sessions").update({
            "active": 0
        }).eq("session_id", session_id).execute()

    st.session_state["logged_in"] = False
    st.session_state["user"] = {}
    st.session_state["session_id"] = None
    st.rerun()

def log_login_attempt(username: str, success: bool, reason: str = ""):
    try:
        supabase.table("login_audit").insert({
            "username": username.strip() if username else "",
            "success": 1 if success else 0,
            "reason": reason,
            "machine_id": get_machine_id(),
            "created_at": datetime.now().isoformat()
        }).execute()
    except Exception:
        pass


def is_user_locked(username: str):
    try:
        res = supabase.table("security_locks").select("*").eq(
            "username", username.strip()
        ).execute()

        if not res.data:
            return False, None

        row = res.data[0]
        locked_until = row.get("locked_until")

        if locked_until and datetime.now() < datetime.fromisoformat(locked_until):
            return True, locked_until

        return False, None
    except Exception:
        return False, None


def register_failed_login(username: str, max_attempts: int = 5, lock_minutes: int = 15):
    username = (username or "").strip()

    if not username:
        return

    try:
        now = datetime.now()

        res = supabase.table("security_locks").select("*").eq(
            "username", username
        ).execute()

        if not res.data:
            supabase.table("security_locks").insert({
                "username": username,
                "failed_count": 1,
                "locked_until": None,
                "updated_at": now.isoformat()
            }).execute()
            return

        row = res.data[0]
        failed_count = int(row.get("failed_count", 0)) + 1

        locked_until = None
        if failed_count >= max_attempts:
            locked_until = (now + timedelta(minutes=lock_minutes)).isoformat()

        supabase.table("security_locks").update({
            "failed_count": failed_count,
            "locked_until": locked_until,
            "updated_at": now.isoformat()
        }).eq("username", username).execute()

    except Exception as e:
        st.warning("Login zaštita trenutno nije dostupna, ali aplikacija nastavlja rad.")
        st.code(str(e))
        


def reset_failed_logins(username: str):
    try:
        supabase.table("security_locks").update({
            "failed_count": 0,
            "locked_until": None,
            "updated_at": datetime.now().isoformat()
        }).eq("username", username.strip()).execute()
    except Exception:
        pass


def admin_panel():
    user = st.session_state.get("user", {})

    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"Prijavljen: **{user.get('username', '')}**  \n"
        f"Važi do: **{str(user.get('expires_at', ''))[:10]}**"
    )

    if st.sidebar.button("🚪 Logout", use_container_width=True):
        logout_user()

    if user.get("role") != "admin":
        return

    st.sidebar.markdown("---")
    st.sidebar.subheader("🔐 Admin panel")

    with st.sidebar.expander("➕ Dodaj korisnika"):
        new_username = st.text_input("Novi username", key="new_username")
        new_password = st.text_input("Nova lozinka", type="password", key="new_password")
        days_valid = st.number_input("Trajanje pristupa (dani)", min_value=1, max_value=3650, value=30)
        role = st.selectbox("Uloga", ["user", "admin"])

        if st.button("Kreiraj korisnika", use_container_width=True):
            try:
                create_user(new_username, new_password, int(days_valid), role=role)
                st.success(f"Korisnik '{new_username}' je dodat.")
            except Exception as e:
                st.error(f"Greška: {e}")

    with st.sidebar.expander("👥 Korisnici"):
        users_df = get_users_df()
        if not users_df.empty:
            st.dataframe(
                users_df[["username", "role", "expires_at", "days_left", "status"]],
                use_container_width=True,
                height=220
            )

    with st.sidebar.expander("⏳ Produži / aktiviraj korisnika"):
        users_df = get_users_df()
        if not users_df.empty:
            username = st.selectbox("Korisnik", users_df["username"].tolist(), key="extend_user")
            extra_days = st.number_input("Dodaj dana", 1, 3650, 30, key="extra_days")
            if st.button("Produži pristup", use_container_width=True):
                try:
                    extend_user(username, extra_days)
                    st.success("Pristup produžen.")
                except Exception as e:
                    st.error(str(e))

    with st.sidebar.expander("🛑 Aktivacija / deaktivacija"):
        users_df = get_users_df()
        if not users_df.empty:
            username = st.selectbox("Korisnik", users_df["username"].tolist(), key="activate_user")
            action = st.radio("Akcija", ["Aktiviraj", "Deaktiviraj"], horizontal=True)
            if st.button("Sačuvaj status", use_container_width=True):
                set_user_active(username, 1 if action == "Aktiviraj" else 0)
                st.success("Status promenjen.")

    with st.sidebar.expander("🔑 Promeni lozinku"):
        users_df = get_users_df()
        if not users_df.empty:
            username = st.selectbox("Korisnik", users_df["username"].tolist(), key="pass_user")
            new_pass = st.text_input("Nova lozinka", type="password", key="pass_new")
            if st.button("Promeni lozinku", use_container_width=True):
                if new_pass:
                    change_user_password(username, new_pass)
                    st.success("Lozinka promenjena.")
                else:
                    st.error("Unesi novu lozinku.")
    with st.sidebar.expander("💻 Reset računara korisnika"):
        users_df = get_users_df()
        if not users_df.empty:
            username = st.selectbox(
                "Korisnik",
                users_df["username"].tolist(),
                key="reset_machine_user"
            )

            if st.button("Resetuj vezani računar", use_container_width=True):
                reset_user_machine(username)
                st.success("Računar je resetovan. Korisnik se sada može prijaviti sa novog računara.")
                
    with st.sidebar.expander("🧾 Login audit"):
        try:
            audit = supabase.table("login_audit").select("*").order(
                "id", desc=True
            ).limit(20).execute()

            audit_df = pd.DataFrame(audit.data)

            if not audit_df.empty:
                st.dataframe(
                    audit_df[["username", "success", "reason", "created_at"]],
                    use_container_width=True,
                    height=250
                )
            else:
                st.info("Nema login zapisa.")
        except Exception as e:
            st.warning("Ne mogu da učitam login audit.")
    with st.sidebar.expander("🟢 Aktivne sesije"):
        try:
            sessions = supabase.table("user_sessions").select("*").eq(
                "active", 1
            ).order("last_seen", desc=True).execute()

            sess_df = pd.DataFrame(sessions.data)

            if not sess_df.empty:
                st.dataframe(
                    sess_df[["username", "ip_address", "browser_info", "machine_id", "created_at", "last_seen"]],
                    use_container_width=True,
                    height=250
                )

                kill_user = st.selectbox(
                    "Isključi korisnika",
                    sess_df["username"].unique().tolist(),
                    key="kill_session_user"
                )

                if st.button("Isključi aktivne sesije", use_container_width=True):
                    supabase.table("user_sessions").update({
                        "active": 0
                    }).eq("username", kill_user).execute()

                    st.success(f"Korisnik {kill_user} je isključen.")
                    st.rerun()
            else:
                st.info("Nema aktivnih sesija.")

        except Exception as e:
            st.warning("Ne mogu da učitam aktivne sesije.")
            st.code(str(e))

def login_page():
    st.title("PV Forecast - ZV Consulting: Login")

    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    if st.button("Login"):
        locked, locked_until = is_user_locked(username)

        if locked:
            st.error(f"Nalog je privremeno zaključan do: {locked_until}")
            log_login_attempt(username, False, "Account locked")
            st.stop()

        success, result = authenticate(username, password)

        if success:
            reset_failed_logins(username)
            log_login_attempt(username, True, "Login success")

            st.session_state["logged_in"] = True
            st.session_state["user"] = result
            session_id = create_session(result["username"])

            if not session_id:
                st.error("Greška pri kreiranju sesije. Pokušaj ponovo.")
                st.stop()

            st.session_state["session_id"] = session_id
            st.rerun()
        else:
            register_failed_login(username)
            log_login_attempt(username, False, str(result))
            st.error(result)


def require_login():
    ensure_default_admin()

    if "logged_in" not in st.session_state:
        st.session_state["logged_in"] = False

    if st.session_state["logged_in"]:

        user = st.session_state.get("user", {})

        # ADMIN: ne proveravaj session lock
        if user.get("role") != "admin":

            if not validate_active_session(timeout_minutes=10):
                st.session_state["logged_in"] = False
                st.session_state["user"] = {}
                st.session_state["session_id"] = None
                st.warning("Sesija je istekla ili je nalog prijavljen na drugom mestu.")
                login_page()
                st.stop()

    if not st.session_state["logged_in"]:
        login_page()
        st.stop()
 
require_login()
admin_panel()

# ==========================================================
# CSS / UI
# ==========================================================

st.markdown("""
<style>
.block-container {
    padding-top: 1.0rem;
    padding-bottom: 1rem;
}
.main-title {
    font-size: 1.55rem;
    font-weight: 850;
    color: #0f172a;
    margin-bottom: 0.25rem;
    line-height: 1.15;

    white-space: normal;
    word-break: keep-all;
    overflow-wrap: normal;
    letter-spacing: -0.01em;
}

.main-subtitle {
    color: #64748b;
    font-size: 0.82rem;
    margin-bottom: 1.0rem;
    line-height: 1.35;
    white-space: normal;
    word-break: normal;
}
.kpi {
    background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
    border: 1px solid #e5e7eb;
    border-radius: 18px;
    padding: 14px 16px;
    margin-bottom: 8px;
    box-shadow: 0 8px 18px rgba(15,23,42,0.05);
}
.kpi .big {
    font-weight: 800;
    font-size: 1.55rem;
    line-height: 1.1;
    color: #0f172a;
}
.kpi .lbl {
    color: #64748b;
    font-size: 0.82rem;
}
.card {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 18px;
    padding: 16px 18px;
    box-shadow: 0 8px 20px rgba(15,23,42,0.04);
}
.stTabs [data-baseweb="tab-list"] {
    gap: 8px;
}
.stTabs [data-baseweb="tab"] {
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 10px 14px;
    color: #111827 !important;
}
.stTabs [aria-selected="true"] {
    background: #e0f2fe !important;
    border-color: #7dd3fc !important;
    font-weight: 800;
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="main-title">☀️ ZV Forecast</div>
<div class="main-subtitle">
PV portfolio forecasting platform — Open-Meteo + pvlib + export reports
</div>
""", unsafe_allow_html=True)

PLOT_TEMPLATE = "plotly_white"


def style_fig(fig, title=None, height=390, ytitle="", xtitle=""):
    fig.update_layout(
        template=PLOT_TEMPLATE,
        height=height,
        margin=dict(l=10, r=10, t=50, b=20),
        title=dict(text=title, x=0.01, xanchor="left") if title else None,
        hovermode="x unified",
        xaxis=dict(showgrid=True, gridcolor="#e5e7eb"),
        yaxis=dict(showgrid=True, gridcolor="#e5e7eb", tickformat=","),
        legend=dict(orientation="h", y=1.12, x=0),
        font=dict(family="Arial", size=13, color="#111827")
    )
    if ytitle:
        fig.update_yaxes(title=ytitle)
    if xtitle:
        fig.update_xaxes(title=xtitle)
    return fig


def kpi_card(col, label, value, sub=""):
    col.markdown(f"""
    <div class="kpi">
      <div class="lbl">{label}</div>
      <div class="big">{value}</div>
      <div class="lbl">{sub}</div>
    </div>
    """, unsafe_allow_html=True)


def info_card(title, body):
    st.markdown(f"""
    <div class="card">
        <b>{title}</b><br>
        <span style="color:#475569;">{body}</span>
    </div>
    """, unsafe_allow_html=True)

# ==========================================================
# SAFE PLOTLY RENDERER (KLJUČNO)
# ==========================================================

def plotly_safe(fig, key: str, use_container_width: bool = True):
    st.plotly_chart(
        fig,
        use_container_width=use_container_width,
        key=key
    )


# ==========================================================
# DEFAULTS
# ==========================================================

PLANT_DEFAULTS = dict(
    name="Elektrana1",
    LATITUDE=45.28833,
    LONGITUDE=18.80472,
    TIMEZONE="Europe/Zagreb",
    PANEL_AREA_M2=49997,
    PANEL_EFF=22.7 / 100.0,
    TILT_DEG=30.0,
    AZIMUTH_DEG=180.0,
    ALBEDO=0.2,
    SYSTEM_LOSSES=0.14,
    GAMMA_PDC=-0.0035,
    INVERTER_EFF=0.97,
    AC_CAP_MW=None,
    RADIATION_BIAS_PCT=0.0,
    USE_TRACKER=False,
    BACKTRACK=True,
    AXIS_TILT=0.0,
    AXIS_AZIMUTH=0.0,
    MAX_ROTATION=60.0,
    GCR=0.35
)

APP_DEFAULTS = dict(
    RESAMPLE_MIN=15,
    FETCH_TTL_MIN=10,
    TEMP_MODEL_KEY="open_rack_glass_polymer",
    EXPORT_UTC=False
)


# ==========================================================
# GENERAL HELPERS
# ==========================================================

def _ensure_named_time_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.index.name != "time":
        out.index.name = "time"
    return out


def _strip_tz(idx, export_utc: bool):
    if idx.tz is None:
        return idx
    return (idx.tz_convert("UTC") if export_utc else idx).tz_localize(None)


def _safe_sheet_name(name: str) -> str:
    bad = '[]:*?/\\'
    clean = ''.join(ch for ch in str(name) if ch not in bad)
    return (clean[:28] or "Sheet")


def safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(name))


def pdf_setup_font(pdf, bold=False):
    supports_unicode = False
    font_name = "Helvetica"
    try:
        ttf_path = os.path.join(os.getcwd(), "DejaVuSans.ttf")
        if os.path.exists(ttf_path):
            pdf.add_font("DejaVu", "", ttf_path, uni=True)
            pdf.add_font("DejaVu", "B", ttf_path, uni=True)
            font_name = "DejaVu"
            supports_unicode = True
    except Exception:
        pass

    pdf.set_font(font_name, "B" if bold else "", 14 if bold else 10)
    return font_name, supports_unicode


def sanitize_text(s: str) -> str:
    if s is None:
        return ""
    repl = {
        "—": "-", "–": "-", "−": "-",
        "“": '"', "”": '"', "„": '"',
        "’": "'", "‘": "'", "…": "..."
    }
    for k, v in repl.items():
        s = str(s).replace(k, v)
    try:
        return s.encode("latin-1", "replace").decode("latin-1")
    except Exception:
        return str(s)


# ==========================================================
# WEATHER PROVIDERS
# ==========================================================

def normalize_weather_columns(df: pd.DataFrame, timezone: str) -> pd.DataFrame:
    df = df.copy()

    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")

    if df.index.tz is None:
        df.index = df.index.tz_localize(timezone)
    else:
        df.index = df.index.tz_convert(timezone)

    required_defaults = {
        "temperature_2m": 20.0,
        "relative_humidity_2m": 50.0,
        "dewpoint_2m": np.nan,
        "cloudcover": 0.0,
        "cloudcover_low": 0.0,
        "cloudcover_mid": 0.0,
        "cloudcover_high": 0.0,
        "pressure_msl": 1013.25,
        "windspeed_10m": 2.0,
        "winddirection_10m": 180.0,
        "shortwave_radiation": np.nan,
        "direct_radiation": np.nan,
        "diffuse_radiation": np.nan,
        "direct_normal_irradiance": np.nan,
    }

    for col, val in required_defaults.items():
        if col not in df.columns:
            df[col] = val

    df["total_cloud_cover"] = (
        df["cloudcover_low"].fillna(0)
        + df["cloudcover_mid"].fillna(0)
        + df["cloudcover_high"].fillna(0)
    ).clip(0, 100)

    if df["total_cloud_cover"].eq(0).all() and "cloudcover" in df.columns:
        df["total_cloud_cover"] = df["cloudcover"].fillna(0).clip(0, 100)

    df["wind_speed"] = df["windspeed_10m"]
    df["wind_direction"] = df["winddirection_10m"]

    return _ensure_named_time_index(df)


@st.cache_data(ttl=APP_DEFAULTS["FETCH_TTL_MIN"] * 60, show_spinner=True)
def fetch_open_meteo(latitude: float, longitude: float, timezone: str) -> pd.DataFrame:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&hourly=temperature_2m,relative_humidity_2m,dewpoint_2m,cloudcover,cloudcover_low,"
        "cloudcover_mid,cloudcover_high,pressure_msl,windspeed_10m,winddirection_10m,"
        "shortwave_radiation,direct_radiation,diffuse_radiation,direct_normal_irradiance"
        f"&timezone={timezone}"
        "&past_days=2&forecast_days=7"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    if "hourly" not in data:
        raise RuntimeError("Open-Meteo response nema 'hourly'.")

    df = pd.DataFrame(data["hourly"])
    return normalize_weather_columns(df, timezone)


@st.cache_data(ttl=APP_DEFAULTS["FETCH_TTL_MIN"] * 60, show_spinner=True)
def fetch_visual_crossing(latitude: float, longitude: float, timezone: str, api_key: str) -> pd.DataFrame:
    if not api_key:
        raise RuntimeError("Visual Crossing API key nije unet.")

    location = f"{latitude},{longitude}"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/"
        f"{location}/next7days"
        f"?unitGroup=metric&include=hours&key={api_key}&contentType=json"
    )

    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    rows = []
    for day in data.get("days", []):
        for h in day.get("hours", []):
            dt = pd.to_datetime(f"{day.get('datetime')} {h.get('datetime')}")
            rows.append({
                "time": dt,
                "temperature_2m": h.get("temp", np.nan),
                "relative_humidity_2m": h.get("humidity", np.nan),
                "cloudcover": h.get("cloudcover", np.nan),
                "windspeed_10m": h.get("windspeed", np.nan),
                "winddirection_10m": h.get("winddir", np.nan),
                "pressure_msl": h.get("pressure", np.nan),
                "shortwave_radiation": h.get("solarradiation", np.nan),
                "direct_radiation": np.nan,
                "diffuse_radiation": np.nan,
                "direct_normal_irradiance": np.nan,
            })

    if not rows:
        raise RuntimeError("Visual Crossing nije vratio hourly podatke.")

    return normalize_weather_columns(pd.DataFrame(rows), timezone)


@st.cache_data(ttl=APP_DEFAULTS["FETCH_TTL_MIN"] * 60, show_spinner=True)
def fetch_solcast_placeholder(latitude: float, longitude: float, timezone: str, api_key: str) -> pd.DataFrame:
    """
    Placeholder/fallback:
    Solcast je odličan za profesionalni PV forecast, ali zahteva API key
    i precizno podešavanje naloga. Ovde ne rušimo aplikaciju, nego vraćamo
    Open-Meteo i jasno obeležavamo da je fallback.
    """
    return fetch_open_meteo(latitude, longitude, timezone)


def fetch_weather(provider: str, latitude: float, longitude: float, timezone: str,
                  visual_crossing_key: str = "", solcast_key: str = "") -> pd.DataFrame:

    if provider == "Commercial Ensemble":
        return fetch_weather_commercial(latitude, longitude, timezone, visual_crossing_key)

    if provider == "Auto Ensemble":
        return fetch_weather_auto(latitude, longitude, timezone)

    if provider == "Open-Meteo only":
        return fetch_open_meteo(latitude, longitude, timezone)

    if provider == "Visual Crossing only":
        return fetch_visual_crossing(latitude, longitude, timezone, visual_crossing_key)

    if provider == "NASA POWER only":
        return fetch_nasa_power(latitude, longitude, timezone)

    if provider == "Solcast future":
        return fetch_open_meteo(latitude, longitude, timezone)

    return fetch_open_meteo(latitude, longitude, timezone)



def merge_weather_sources_commercial(sources, timezone):
    dfs = {k: v for k, v in sources.items() if isinstance(v, pd.DataFrame)}

    priority = ["visual_crossing", "open_meteo", "nasa_power"]
    base = dfs[next(k for k in priority if k in dfs)].copy()

    def weighted_col(col, weights):
        vals = []
        for src, w in weights.items():
            if src in dfs and col in dfs[src].columns:
                vals.append(dfs[src][col].rename(src) * w)
        if vals:
            return pd.concat(vals, axis=1).sum(axis=1)
        return base.get(col, np.nan)

    base["temperature_2m"] = weighted_col(
        "temperature_2m",
        {"visual_crossing": 0.35, "open_meteo": 0.50, "nasa_power": 0.15}
    )

    base["relative_humidity_2m"] = weighted_col(
        "relative_humidity_2m",
        {"visual_crossing": 0.45, "open_meteo": 0.40, "nasa_power": 0.15}
    )

    base["windspeed_10m"] = weighted_col(
        "windspeed_10m",
        {"visual_crossing": 0.35, "open_meteo": 0.50, "nasa_power": 0.15}
    )

    base["pressure_msl"] = weighted_col(
        "pressure_msl",
        {"visual_crossing": 0.40, "open_meteo": 0.40, "nasa_power": 0.20}
    )

    if "visual_crossing" in dfs and "cloudcover" in dfs["visual_crossing"]:
        base["cloudcover"] = dfs["visual_crossing"]["cloudcover"]
        base["source_cloud"] = "visual_crossing"
    elif "open_meteo" in dfs:
        base["cloudcover"] = dfs["open_meteo"].get("cloudcover", 0)
        base["source_cloud"] = "open_meteo"

    for src in ["visual_crossing", "open_meteo", "nasa_power"]:
        if src in dfs and "shortwave_radiation" in dfs[src].columns:
            s = dfs[src]["shortwave_radiation"]
            if not s.isna().all():
                base["shortwave_radiation"] = s
                base["source_radiation"] = src
                break

    base["source_temperature"] = "weighted"
    base["source_wind"] = "weighted"
    base["available_sources"] = ", ".join(dfs.keys())

    base = normalize_weather_columns(base, timezone)

    base["shortwave_radiation"] = base["shortwave_radiation"].clip(lower=0, upper=1200)
    base["temperature_2m"] = base["temperature_2m"].clip(-40, 55)
    base["windspeed_10m"] = base["windspeed_10m"].clip(0, 45)
    base["relative_humidity_2m"] = base["relative_humidity_2m"].clip(0, 100)

    base = base.interpolate(method="time").ffill().bfill()
    
    return base
 

def fetch_weather_commercial(latitude, longitude, timezone, visual_crossing_key):
    sources = {}

    try:
        sources["visual_crossing"] = fetch_visual_crossing(
            latitude, longitude, timezone, visual_crossing_key
        )
    except Exception as e:
        sources["visual_crossing_error"] = str(e)

    try:
        sources["open_meteo"] = fetch_open_meteo(latitude, longitude, timezone)
    except Exception as e:
        sources["open_meteo_error"] = str(e)

    try:
        sources["nasa_power"] = fetch_nasa_power(latitude, longitude, timezone)
    except Exception as e:
        sources["nasa_power_error"] = str(e)

    if not any(isinstance(v, pd.DataFrame) for v in sources.values()):
        raise RuntimeError("Nijedan izvor nije vratio podatke.")

    return merge_weather_sources_commercial(sources, timezone)


def fetch_weather_auto(latitude, longitude, timezone, keys=None):
    sources = {}

    try:
        sources["open_meteo"] = fetch_open_meteo(latitude, longitude, timezone)
    except Exception as e:
        sources["open_meteo_error"] = str(e)

    try:
        vc_key = st.secrets.get("VISUAL_CROSSING_KEY", "")
        if vc_key:
            sources["visual_crossing"] = fetch_visual_crossing(
                latitude, longitude, timezone, vc_key
            )
    except Exception as e:
        sources["visual_crossing_error"] = str(e)

    try:
        sources["nasa_power"] = fetch_nasa_power(latitude, longitude, timezone)
    except Exception as e:
        sources["nasa_power_error"] = str(e)

    if not any(isinstance(v, pd.DataFrame) for v in sources.values()):
        raise RuntimeError("Nijedan vremenski izvor nije vratio podatke.")

    return merge_weather_sources(sources, timezone)


@st.cache_data(ttl=6 * 60 * 60, show_spinner=True)
def fetch_nasa_power(latitude: float, longitude: float, timezone: str) -> pd.DataFrame:
    end = datetime.now().date()
    start = end - timedelta(days=7)

    url = (
        "https://power.larc.nasa.gov/api/temporal/hourly/point"
        f"?parameters=ALLSKY_SFC_SW_DWN,T2M,RH2M,WS10M,PS"
        f"&community=RE"
        f"&longitude={longitude}"
        f"&latitude={latitude}"
        f"&start={start.strftime('%Y%m%d')}"
        f"&end={end.strftime('%Y%m%d')}"
        f"&format=JSON"
    )

    r = requests.get(url, timeout=40)
    r.raise_for_status()
    data = r.json()

    params = data["properties"]["parameter"]

    idx = pd.to_datetime(list(params["T2M"].keys()), format="%Y%m%d%H")
    df = pd.DataFrame(index=idx)

    df["temperature_2m"] = pd.Series(params["T2M"]).astype(float).values
    df["relative_humidity_2m"] = pd.Series(params["RH2M"]).astype(float).values
    df["windspeed_10m"] = pd.Series(params["WS10M"]).astype(float).values
    df["pressure_msl"] = pd.Series(params["PS"]).astype(float).values
    df["shortwave_radiation"] = pd.Series(params["ALLSKY_SFC_SW_DWN"]).astype(float).values

    df.index.name = "time"
    return normalize_weather_columns(df, timezone)


# ==========================================================
# PV MODEL
# ==========================================================

def to_uniform(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes <= 0:
        return df
    return df.resample(f"{minutes}min").interpolate(method="time")


def solar_geometry(index, latitude, longitude):
    solpos = pvlib.solarposition.get_solarposition(index, latitude, longitude)
    zenith = solpos["apparent_zenith"]
    azimuth = solpos["azimuth"]
    sun_up = zenith < 90
    return solpos, zenith, azimuth, sun_up


def derive_irradiance(df: pd.DataFrame, zenith: pd.Series, bias: float) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)

    out["GHI"] = df.get("shortwave_radiation", np.nan)
    out["DNI"] = df.get("direct_normal_irradiance", df.get("direct_radiation", np.nan))
    out["DHI"] = df.get("diffuse_radiation", np.nan)

    if out["GHI"].isna().all():
        cloud = df.get("cloudcover", df.get("total_cloud_cover", 0)).fillna(0).clip(0, 100)
        out["GHI"] = 850.0 * (1 - (cloud / 100.0) ** 1.2)

    need_dhi = out["DHI"].isna().all()
    need_dni = out["DNI"].isna().all()

    if need_dhi or need_dni:
        ghi = out["GHI"].fillna(0).where(zenith < 90, 0)
        er = erbs(ghi, zenith.clip(0, 89.9), df.index)
        if need_dhi:
            out["DHI"] = er["dhi"].clip(lower=0)
        if need_dni:
            out["DNI"] = er["dni"].clip(lower=0)

    scale = 1.0 + bias
    for c in ["GHI", "DNI", "DHI"]:
        out[c] = (out[c].fillna(0) * scale).clip(lower=0)

    out.loc[zenith >= 90, ["GHI", "DNI", "DHI"]] = 0.0
    return out


def tracker_surfaces(solpos, axis_tilt, axis_azimuth, max_rot, backtrack, gcr):
    tr = singleaxis(
        solpos["apparent_zenith"],
        solpos["azimuth"],
        axis_tilt=axis_tilt,
        axis_azimuth=axis_azimuth,
        max_angle=max_rot,
        backtrack=backtrack,
        gcr=gcr
    )
    return tr["surface_tilt"].fillna(0), tr["surface_azimuth"].fillna(0)


def cell_temperature(poa_global, temp_air, wind_speed, model_key="open_rack_glass_polymer"):
    alias = {
        "open_rack_glassback": "open_rack_glass_polymer",
        "roof_mount_glass_polymer": "insulated_back_glass_polymer",
        "open_rack_glass_glass": "open_rack_glass_glass",
        "insulated_back_glass_glass": "insulated_back_glass_glass",
    }
    key = alias.get(model_key, model_key)
    try:
        params = TEMPERATURE_MODEL_PARAMETERS["sapm"][key]
    except KeyError:
        params = TEMPERATURE_MODEL_PARAMETERS["sapm"]["open_rack_glass_polymer"]

    return sapm_cell(poa_global, temp_air, wind_speed, **params)


def integrate_energy(power_kw: pd.Series) -> pd.Series:
    if power_kw.empty:
        return power_kw
    dt = power_kw.index.to_series().diff().dt.total_seconds()
    med = float(np.nanmedian(dt.values)) if np.isfinite(np.nanmedian(dt.values)) else 900.0
    dt = dt.fillna(med).clip(lower=1.0)
    return power_kw * (dt / 3600.0)


def capacity_from_area(area_m2, eff):
    return area_m2 * eff


def limit_ac(power_kw, inv_eff, ac_cap_mw=None):
    pac = power_kw * inv_eff
    if ac_cap_mw is not None:
        return pac.clip(upper=ac_cap_mw * 1000.0)
    return pac


def add_scenarios(df: pd.DataFrame, cloud_col="total_cloud_cover"):
    out = df.copy()
    cloud = out.get(cloud_col, pd.Series(0, index=out.index)).fillna(0).clip(0, 100)

    uncertainty = 0.08 + 0.22 * (cloud / 100.0)
    uncertainty = uncertainty.clip(0.08, 0.35)

    out["p50_kw"] = out["p_ac_kw"]
    out["p10_kw"] = (out["p_ac_kw"] * (1 - uncertainty)).clip(lower=0)
    out["p90_kw"] = (out["p_ac_kw"] * (1 + uncertainty)).clip(lower=0)

    out["energy_p50_kwh"] = integrate_energy(out["p50_kw"])
    out["energy_p10_kwh"] = integrate_energy(out["p10_kw"])
    out["energy_p90_kwh"] = integrate_energy(out["p90_kw"])

    return out


def kpi_summary(df15: pd.DataFrame, dc_kwp: float):
    df15 = _ensure_named_time_index(df15)
    total_kwh = df15["energy_kwh"].sum()
    span_h = (df15.index[-1] - df15.index[0]).total_seconds() / 3600.0 if len(df15) else 0.0
    avg_kw = total_kwh / span_h if span_h > 0 else 0.0
    cf = (total_kwh / (dc_kwp * span_h)) if (dc_kwp > 0 and span_h > 0) else 0.0

    daily = df15.resample("D").agg(
        energy_kwh=("energy_kwh", "sum"),
        energy_p10_kwh=("energy_p10_kwh", "sum"),
        energy_p50_kwh=("energy_p50_kwh", "sum"),
        energy_p90_kwh=("energy_p90_kwh", "sum"),
        avg_temp=("temperature_2m", "mean"),
        avg_rh=("relative_humidity_2m", "mean"),
        avg_cloud=("total_cloud_cover", "mean"),
        avg_wind=("wind_speed", "mean"),
        peak_kw=("p_ac_kw", "max"),
        cf_day_pct=("cf_instant_%", "mean")
    )
    daily = _ensure_named_time_index(daily)

    return dict(total_kwh=total_kwh, avg_kw=avg_kw, cf=cf, daily=daily)

def apply_bias_correction(df):
    df = df.copy()

    if "shortwave_radiation" not in df.columns:
        return df

    # proceni bias na osnovu poslednjih 24h
    last_24h = df.last("24H")

    if last_24h.empty:
        return df

    ghi = last_24h["shortwave_radiation"]
    ghi_cs = last_24h.get("poa_global_cs", ghi)

    valid = (ghi_cs > 50)

    if valid.sum() < 10:
        return df

    ratio = (ghi[valid] / ghi_cs[valid]).median()

    # ograniči korekciju
    ratio = np.clip(ratio, 0.7, 1.3)

    df["shortwave_radiation"] = df["shortwave_radiation"] * ratio
    df["bias_correction_factor"] = ratio

    return df


def compute_plant(plant: dict, res_min: int, temp_model_key: str,
                  weather_provider: str, visual_crossing_key: str, solcast_key: str) -> dict:
    p = {**PLANT_DEFAULTS, **plant}

    lat = float(p["LATITUDE"])
    lon = float(p["LONGITUDE"])
    tz = p["TIMEZONE"]
    area = float(p["PANEL_AREA_M2"])
    eff = float(p["PANEL_EFF"])
    tilt = float(p["TILT_DEG"])
    azim = float(p["AZIMUTH_DEG"])
    albedo = float(p["ALBEDO"])
    sys_losses = float(p["SYSTEM_LOSSES"])
    gamma = float(p["GAMMA_PDC"])
    inv_eff = float(p["INVERTER_EFF"])
    ac_cap_mw = None if p["AC_CAP_MW"] in (None, "", "None") else float(p["AC_CAP_MW"])
    bias = float(p["RADIATION_BIAS_PCT"]) / 100.0

    use_tracker = bool(p["USE_TRACKER"])
    backtrack = bool(p["BACKTRACK"])
    axis_tilt = float(p["AXIS_TILT"])
    axis_azimuth = float(p["AXIS_AZIMUTH"])
    max_rot = float(p["MAX_ROTATION"])
    gcr = float(p["GCR"])

    df_raw = fetch_weather(
        provider=weather_provider,
        latitude=lat,
        longitude=lon,
        timezone=tz,
        visual_crossing_key=visual_crossing_key,
        solcast_key=solcast_key
    )

    df = to_uniform(df_raw, res_min).copy()
    

    site = Location(lat, lon, tz)
    cs = site.get_clearsky(df.index, model="ineichen")
    cs.rename(columns={"ghi": "cs_GHI", "dni": "cs_DNI", "dhi": "cs_DHI"}, inplace=True)
    cs = _ensure_named_time_index(cs)

    solpos, zenith, az_sun, sun_up = solar_geometry(df.index, lat, lon)
    df["sun_up"] = sun_up

    irr = derive_irradiance(df, zenith, bias)
    for c in ["GHI", "DNI", "DHI"]:
        df[c] = irr[c]

    if use_tracker:
        s_tilt, s_az = tracker_surfaces(solpos, axis_tilt, axis_azimuth, max_rot, backtrack, gcr)
    else:
        s_tilt = pd.Series(tilt, index=df.index)
        s_az = pd.Series(azim, index=df.index)

    dni_extra = pvlib.irradiance.get_extra_radiation(df.index, method="spencer")
    airmass_rel = pvatm.get_relative_airmass(zenith.clip(0, 89.9), model="kastenyoung1989")

    poa = get_total_irradiance(
        surface_tilt=s_tilt,
        surface_azimuth=s_az,
        dni=df["DNI"],
        ghi=df["GHI"],
        dhi=df["DHI"],
        solar_zenith=zenith,
        solar_azimuth=az_sun,
        albedo=albedo,
        dni_extra=dni_extra,
        airmass=airmass_rel,
        model="haydavies"
    )
    df["poa_global"] = poa["poa_global"].clip(lower=0)

    poa_cs = get_total_irradiance(
        surface_tilt=s_tilt,
        surface_azimuth=s_az,
        dni=cs["cs_DNI"].reindex(df.index).fillna(0),
        ghi=cs["cs_GHI"].reindex(df.index).fillna(0),
        dhi=cs["cs_DHI"].reindex(df.index).fillna(0),
        solar_zenith=zenith,
        solar_azimuth=az_sun,
        albedo=albedo,
        dni_extra=dni_extra,
        airmass=airmass_rel,
        model="haydavies"
    )
    df["poa_global_cs"] = poa_cs["poa_global"].clip(lower=0)
    df = apply_bias_correction(df)

    df["clear_sky_index"] = np.where(
        cs["cs_GHI"].reindex(df.index).fillna(0) > 0,
        df["GHI"] / cs["cs_GHI"].reindex(df.index).replace(0, np.nan),
        np.nan
    )
    df["clear_sky_index"] = df["clear_sky_index"].clip(0, 1.5)

    df["t_cell"] = cell_temperature(
        df["poa_global"],
        df.get("temperature_2m", 20.0),
        df.get("wind_speed", 2.0),
        model_key=temp_model_key
    )

    eff_temp = (eff * (1.0 + gamma * (df["t_cell"] - 25.0))).clip(lower=0.0)

    p_dc_kw = (df["poa_global"] * area * eff_temp * (1.0 - sys_losses)) / 1000.0
    p_ac_kw = limit_ac(p_dc_kw, inv_eff, ac_cap_mw)

    df["p_dc_kw"] = p_dc_kw
    df["p_ac_kw"] = p_ac_kw
    df["energy_kwh"] = integrate_energy(df["p_ac_kw"])

    dc_kwp = capacity_from_area(area, eff)
    df["cf_instant_%"] = (df["p_ac_kw"] / (dc_kwp if dc_kwp > 0 else np.nan)) * 100.0

    df = add_scenarios(df)

    kpi = kpi_summary(df, dc_kwp)

    meta = dict(
        name=p["name"],
        latitude=lat,
        longitude=lon,
        timezone=tz,
        raster_min=res_min,
        weather_provider=weather_provider,
        tracker=use_tracker,
        gcr=gcr,
        backtrack=backtrack,
        axis_tilt=axis_tilt,
        axis_azimuth=axis_azimuth,
        max_rotation=max_rot,
        area_m2=area,
        eff_stc=eff,
        albedo=albedo,
        sys_losses=sys_losses,
        gamma=gamma,
        inverter_eff=inv_eff,
        ac_cap_mw=ac_cap_mw,
        bias_pct=bias * 100,
        temp_model=temp_model_key
    )

    df = _ensure_named_time_index(df)

    return dict(
        df=df,
        daily=kpi["daily"],
        cs=cs,
        kpi=kpi,
        meta=meta,
        dc_kwp=dc_kwp
    )


# ==========================================================
# SIDEBAR
# ==========================================================

with st.sidebar:
    st.header("⚙️ Forecast settings")

    weather_provider = st.selectbox(
        "Weather mode",
        [
            "Commercial Ensemble",
            "Auto Ensemble",
            "Open-Meteo only",
            "Visual Crossing only",
            "NASA POWER only",
            "Solcast future"
        ],
        index=0
    )

    visual_crossing_key = st.secrets.get("VISUAL_CROSSING_KEY", "")
    solcast_key = st.secrets.get("SOLCAST_KEY", "")

    if weather_provider in ["Commercial Ensemble", "Visual Crossing only"]:
        if not visual_crossing_key:
            st.error("Nedostaje VISUAL_CROSSING_KEY u Streamlit secrets.")
            st.stop()

    if weather_provider == "Solcast future":
        st.info("Solcast modul je ostavljen za buduću premium integraciju kada budeš imao API key.")

    res_min = st.selectbox(
        "Raster (min)",
        [5, 10, 15, 30, 60],
        index=[5, 10, 15, 30, 60].index(APP_DEFAULTS["RESAMPLE_MIN"])
    )

    temp_models = sorted(TEMPERATURE_MODEL_PARAMETERS["sapm"].keys())
    default_idx = next((i for i, k in enumerate(temp_models) if k == APP_DEFAULTS["TEMP_MODEL_KEY"]), 0)
    temp_model = st.selectbox("Temp model (SAPM)", options=temp_models, index=default_idx)

    export_utc = st.checkbox("📦 Export u UTC", value=APP_DEFAULTS["EXPORT_UTC"])

    st.markdown("---")
    st.subheader("📥 Elektrane")

    example_plants = [
        PLANT_DEFAULTS,
        {
            **PLANT_DEFAULTS,
            "name": "Elektrana 2",
            "LATITUDE": 45.8099,
            "LONGITUDE": 15.8980,
            "TIMEZONE": "Europe/Zagreb",
            "PANEL_AREA_M2": 42000,
            "TILT_DEG": 15.0,
            "AZIMUTH_DEG": 190.0,
            "AC_CAP_MW": 7.0
        },
        {
            **PLANT_DEFAULTS,
            "name": "Elektrana 3",
            "LATITUDE": 45.5488,
            "LONGITUDE": 18.6931,
            "TIMEZONE": "Europe/Zagreb",
            "PANEL_AREA_M2": 21000,
            "TILT_DEG": 20.0,
            "AZIMUTH_DEG": 180.0,
            "AC_CAP_MW": 3.5
        }
    ]

    default_json = json.dumps(example_plants, ensure_ascii=False, indent=2)

    if "plants_json" not in st.session_state:
        st.session_state["plants_json"] = default_json

     

    upl = st.file_uploader("Load plants JSON", type=["json"])
    if upl is not None:
        try:
            st.session_state["plants_json"] = upl.read().decode("utf-8")
            st.success("JSON učitan.")
            st.rerun()
        except Exception:
            st.warning("Ne mogu da učitam JSON.")

    with st.expander("🧙 Plant wizard"):
        w_name = st.text_input("Naziv elektrane", "Nova elektrana")
        w_lat = st.number_input("Latitude", value=float(PLANT_DEFAULTS["LATITUDE"]), format="%.6f")
        w_lon = st.number_input("Longitude", value=float(PLANT_DEFAULTS["LONGITUDE"]), format="%.6f")
        w_tz = st.text_input("Timezone", "Europe/Zagreb")

        w_mount = st.selectbox("Montaža", ["Roof mount", "Open rack (fixed)", "Tracker 1P"], index=1)
        w_module = st.selectbox("Modul", ["Glass-Polymer", "Glass-Glass"], index=0)

        lat_abs = abs(w_lat)
        if w_mount == "Roof mount":
            d_tilt, d_gcr, d_tracker = 15.0, 0.40, False
        elif w_mount == "Open rack (fixed)":
            d_tilt, d_gcr, d_tracker = float(np.clip(lat_abs, 10.0, 40.0)), 0.35, False
        else:
            d_tilt, d_gcr, d_tracker = 0.0, 0.35, True

        c1, c2 = st.columns(2)
        with c1:
            w_tilt = st.number_input("Tilt [°]", value=float(d_tilt), min_value=0.0, max_value=90.0)
            w_az = st.number_input("Azimut [°]", value=180.0, min_value=0.0, max_value=360.0)
            w_area = st.number_input("PANEL_AREA_M2", value=float(PLANT_DEFAULTS["PANEL_AREA_M2"]), min_value=1000.0)
            w_eff = st.number_input("PANEL_EFF [%]", value=22.0, min_value=10.0, max_value=28.0)
            w_ac = st.number_input("AC_CAP_MW", value=0.0, min_value=0.0)
        with c2:
            w_losses = st.number_input("SYSTEM_LOSSES [%]", value=14.0, min_value=0.0, max_value=35.0)
            w_inv = st.number_input("INVERTER_EFF [%]", value=97.0, min_value=85.0, max_value=99.9)
            w_gamma = st.number_input("GAMMA_PDC [%/°C]", value=-0.35, step=0.01)
            w_bias = st.number_input("RADIATION_BIAS_PCT [%]", value=0.0, min_value=-30.0, max_value=30.0)
            w_gcr = st.number_input("GCR", value=float(d_gcr), min_value=0.1, max_value=0.9)

        if d_tracker:
            st.markdown("**Tracker parametri**")
            t1, t2, t3, t4 = st.columns(4)
            with t1:
                w_axis_tilt = st.number_input("AXIS_TILT", value=0.0)
            with t2:
                w_axis_az = st.number_input("AXIS_AZIMUTH", value=180.0)
            with t3:
                w_max_rot = st.number_input("MAX_ROTATION", value=60.0)
            with t4:
                w_backtrack = st.checkbox("BACKTRACK", value=True)
        else:
            w_axis_tilt, w_axis_az, w_max_rot, w_backtrack = 0.0, 0.0, 60.0, False

        if st.button("➕ Dodaj elektranu", use_container_width=True):
            try:
                current_list = json.loads(st.session_state["plants_json"])
                assert isinstance(current_list, list)
            except Exception:
                current_list = []

            new_plant = {
                "name": w_name,
                "LATITUDE": float(w_lat),
                "LONGITUDE": float(w_lon),
                "TIMEZONE": w_tz,
                "PANEL_AREA_M2": float(w_area),
                "PANEL_EFF": float(w_eff) / 100.0,
                "TILT_DEG": float(w_tilt),
                "AZIMUTH_DEG": float(w_az),
                "ALBEDO": 0.2,
                "SYSTEM_LOSSES": float(w_losses) / 100.0,
                "GAMMA_PDC": float(w_gamma) / 100.0,
                "INVERTER_EFF": float(w_inv) / 100.0,
                "AC_CAP_MW": None if w_ac <= 0 else float(w_ac),
                "RADIATION_BIAS_PCT": float(w_bias),
                "USE_TRACKER": bool(d_tracker),
                "BACKTRACK": bool(w_backtrack),
                "AXIS_TILT": float(w_axis_tilt),
                "AXIS_AZIMUTH": float(w_axis_az),
                "MAX_ROTATION": float(w_max_rot),
                "GCR": float(w_gcr)
            }

            current_list.append(new_plant)
            st.session_state["plants_json"] = json.dumps(current_list, ensure_ascii=False, indent=2)
            st.success(f"Dodato: {w_name}")
            st.rerun()

    if st.button("🔄 Refresh forecast", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ==========================================================
# PARSE PLANTS AND COMPUTE
# ==========================================================

try:
    plants = json.loads(st.session_state["plants_json"])
    assert isinstance(plants, list) and len(plants) > 0
except Exception:
    st.error("JSON nije validan. Očekuje se lista objekata.")
    st.stop()



# ==========================================================
# SIDEBAR — PREGLED I IZMENA UKLJUČENIH ELEKTRANA
# ==========================================================

with st.sidebar:
    st.markdown("---")
    st.subheader("🏭 Uključene elektrane")

    plants_info = []

    for p in plants:
        area = float(p.get("PANEL_AREA_M2", 0))
        eff = float(p.get("PANEL_EFF", 0))
        dc_mwp = area * eff / 1000

        ac_cap = p.get("AC_CAP_MW", None)
        ac_txt = "n/a" if ac_cap in [None, "", "None"] else f"{float(ac_cap):.2f}"

        plants_info.append({
            "Elektrana": p.get("name", ""),
            "DC MWp": round(dc_mwp, 2),
            "AC MW": ac_txt,
            "Lat": round(float(p.get("LATITUDE", 0)), 5),
            "Lon": round(float(p.get("LONGITUDE", 0)), 5),
            "Tilt": p.get("TILT_DEG", ""),
            "Azimut": p.get("AZIMUTH_DEG", ""),
            "Tracker": "DA" if p.get("USE_TRACKER", False) else "NE"
        })

    st.dataframe(
        pd.DataFrame(plants_info),
        use_container_width=True,
        height=220
    )

    st.markdown("---")
    st.subheader("✏️ Izmeni elektranu")

    selected_idx = st.selectbox(
        "Izaberi elektranu za izmenu",
        range(len(plants)),
        format_func=lambda i: plants[i].get("name", f"Elektrana {i+1}"),
        key="edit_plant_select"
    )

    p_edit = plants[selected_idx].copy()

    with st.expander("Parametri izabrane elektrane", expanded=False):
        new_name = st.text_input("Naziv", p_edit.get("name", ""), key="edit_name")

        new_lat = st.number_input(
            "Latitude",
            value=float(p_edit.get("LATITUDE", 0.0)),
            format="%.6f",
            key="edit_lat"
        )

        new_lon = st.number_input(
            "Longitude",
            value=float(p_edit.get("LONGITUDE", 0.0)),
            format="%.6f",
            key="edit_lon"
        )

        new_tz = st.text_input(
            "Timezone",
            p_edit.get("TIMEZONE", "Europe/Zagreb"),
            key="edit_tz"
        )

        new_area = st.number_input(
            "PANEL_AREA_M2",
            value=float(p_edit.get("PANEL_AREA_M2", 0.0)),
            min_value=0.0,
            key="edit_area"
        )

        new_eff_pct = st.number_input(
            "PANEL_EFF [%]",
            value=float(p_edit.get("PANEL_EFF", 0.0)) * 100,
            min_value=0.0,
            max_value=100.0,
            key="edit_eff"
        )

        new_tilt = st.number_input(
            "Tilt [°]",
            value=float(p_edit.get("TILT_DEG", 0.0)),
            min_value=0.0,
            max_value=90.0,
            key="edit_tilt"
        )

        new_azimuth = st.number_input(
            "Azimut [°]",
            value=float(p_edit.get("AZIMUTH_DEG", 180.0)),
            min_value=0.0,
            max_value=360.0,
            key="edit_azimuth"
        )

        new_ac_cap = st.number_input(
            "AC_CAP_MW",
            value=float(p_edit.get("AC_CAP_MW") or 0.0),
            min_value=0.0,
            key="edit_ac_cap"
        )

        new_losses_pct = st.number_input(
            "SYSTEM_LOSSES [%]",
            value=float(p_edit.get("SYSTEM_LOSSES", 0.14)) * 100,
            min_value=0.0,
            max_value=50.0,
            key="edit_losses"
        )

        new_inv_pct = st.number_input(
            "INVERTER_EFF [%]",
            value=float(p_edit.get("INVERTER_EFF", 0.97)) * 100,
            min_value=0.0,
            max_value=100.0,
            key="edit_inv"
        )

        new_gamma_pct = st.number_input(
            "GAMMA_PDC [%/°C]",
            value=float(p_edit.get("GAMMA_PDC", -0.0035)) * 100,
            step=0.01,
            key="edit_gamma"
        )

        new_bias = st.number_input(
            "RADIATION_BIAS_PCT [%]",
            value=float(p_edit.get("RADIATION_BIAS_PCT", 0.0)),
            min_value=-50.0,
            max_value=50.0,
            key="edit_bias"
        )

        new_tracker = st.checkbox(
            "USE_TRACKER",
            value=bool(p_edit.get("USE_TRACKER", False)),
            key="edit_tracker"
        )

        col_save, col_delete = st.columns(2)

        with col_save:
            if st.button("💾 Sačuvaj", use_container_width=True, key="save_plant_edit"):
                plants[selected_idx] = {
                    **p_edit,
                    "name": new_name,
                    "LATITUDE": float(new_lat),
                    "LONGITUDE": float(new_lon),
                    "TIMEZONE": new_tz,
                    "PANEL_AREA_M2": float(new_area),
                    "PANEL_EFF": float(new_eff_pct) / 100.0,
                    "TILT_DEG": float(new_tilt),
                    "AZIMUTH_DEG": float(new_azimuth),
                    "AC_CAP_MW": None if new_ac_cap <= 0 else float(new_ac_cap),
                    "SYSTEM_LOSSES": float(new_losses_pct) / 100.0,
                    "INVERTER_EFF": float(new_inv_pct) / 100.0,
                    "GAMMA_PDC": float(new_gamma_pct) / 100.0,
                    "RADIATION_BIAS_PCT": float(new_bias),
                    "USE_TRACKER": bool(new_tracker)
                }

                st.session_state["plants_json"] = json.dumps(
                    plants,
                    ensure_ascii=False,
                    indent=2
                )

                st.success("Elektrana je izmenjena.")
                st.rerun()

        with col_delete:
            if st.button("🗑️ Obriši", use_container_width=True, key="delete_plant_edit"):
                plants.pop(selected_idx)

                if len(plants) == 0:
                    plants.append(PLANT_DEFAULTS.copy())

                st.session_state["plants_json"] = json.dumps(
                    plants,
                    ensure_ascii=False,
                    indent=2
                )

                st.warning("Elektrana je obrisana.")
                st.rerun()

results = {}
map_rows = []

with st.spinner("Računam PV forecast za sve elektrane..."):
    for plant in plants:
        try:
            r = compute_plant(
                plant=plant,
                res_min=res_min,
                temp_model_key=temp_model,
                weather_provider=weather_provider,
                visual_crossing_key=visual_crossing_key,
                solcast_key=solcast_key
            )
            results[plant["name"]] = r
            cap_mw = r["meta"].get("ac_cap_mw") or (r["dc_kwp"] / 1000.0)
            map_rows.append(dict(
                lat=float(plant["LATITUDE"]),
                lon=float(plant["LONGITUDE"]),
                name=plant["name"],
                cap=float(cap_mw)
            ))
        except Exception as e:
            st.error(f"Greška za elektranu '{plant.get('name', '?')}'")
            st.exception(e)

if not results:
    st.stop()


# ==========================================================
# TOTAL PORTFOLIO
# ==========================================================

dfs = []
for name, r in results.items():
    d = r["df"][[
        "p_ac_kw", "energy_kwh",
        "p10_kw", "p50_kw", "p90_kw",
        "energy_p10_kwh", "energy_p50_kwh", "energy_p90_kwh"
    ]].copy()
    d.columns = pd.MultiIndex.from_product([[name], d.columns])
    dfs.append(d)

total_df = pd.concat(dfs, axis=1).sort_index()
total_df = _ensure_named_time_index(total_df)

for col in ["p_ac_kw", "energy_kwh", "p10_kw", "p50_kw", "p90_kw",
            "energy_p10_kwh", "energy_p50_kwh", "energy_p90_kwh"]:
    total_df[("TOTAL", col)] = total_df.xs(col, level=1, axis=1).sum(axis=1).fillna(0)

total_power = total_df[("TOTAL", "p_ac_kw")]
total_energy = total_df[("TOTAL", "energy_kwh")]
total_p10 = total_df[("TOTAL", "p10_kw")]
total_p50 = total_df[("TOTAL", "p50_kw")]
total_p90 = total_df[("TOTAL", "p90_kw")]

total_daily = pd.DataFrame({
    "energy_kwh": total_df[("TOTAL", "energy_kwh")].resample("D").sum(),
    "energy_p10_kwh": total_df[("TOTAL", "energy_p10_kwh")].resample("D").sum(),
    "energy_p50_kwh": total_df[("TOTAL", "energy_p50_kwh")].resample("D").sum(),
    "energy_p90_kwh": total_df[("TOTAL", "energy_p90_kwh")].resample("D").sum(),
    "peak_kw": total_power.resample("D").max()
})
total_daily = _ensure_named_time_index(total_daily).dropna(how="all")

total_dc_kwp = sum(r["dc_kwp"] for r in results.values())
span_h = (total_energy.index[-1] - total_energy.index[0]).total_seconds() / 3600 if len(total_energy) > 0 else 0
avg_kw = total_energy.sum() / span_h if span_h > 0 else 0
cf_total = (total_energy.sum() / (total_dc_kwp * span_h)) if total_dc_kwp > 0 and span_h > 0 else 0
cf_total = float(np.clip(cf_total, 0.0, 1.0)) if np.isfinite(cf_total) else 0.0

tz0 = list(results.values())[0]["meta"]["timezone"]
today = pd.Timestamp.now(tz=tz0).normalize()
tomorrow = today + pd.Timedelta(days=1)

today_row = total_daily.loc[total_daily.index.normalize() == today] if not total_daily.empty else pd.DataFrame()
tomorrow_row = total_daily.loc[total_daily.index.normalize() == tomorrow] if not total_daily.empty else pd.DataFrame()

best_day = None
if not total_daily.empty:
    best_idx = total_daily["energy_kwh"].idxmax()
    best_day = (best_idx, total_daily.loc[best_idx, "energy_kwh"])

colA, colB, colC, colD, colE = st.columns(5)
kpi_card(colA, "DC kapacitet [MWp]", f"{total_dc_kwp / 1000:.2f}")
kpi_card(colB, "Energija period [MWh]", f"{total_energy.sum() / 1000:.2f}")
kpi_card(colC, "Prosečna snaga [MW]", f"{avg_kw / 1000:.2f}")
kpi_card(colD, "Capacity factor", f"{cf_total * 100:.1f} %")
if not tomorrow_row.empty:
    kpi_card(colE, "Sutra forecast", f"{tomorrow_row['energy_kwh'].iloc[0] / 1000:.2f} MWh",
             sub=f"Peak {tomorrow_row['peak_kw'].iloc[0]:,.0f} kW")
else:
    kpi_card(colE, "Sutra forecast", "—")

st.caption(
    f"Weather: {weather_provider} • Elektrane: {', '.join(results.keys())} • "
    f"Raster: {res_min} min • Temp model: {temp_model}"
)


# ==========================================================
# EXPORT BUILDERS
# ==========================================================

def build_excel_export():
    xbuf = io.BytesIO()

    with pd.ExcelWriter(xbuf, engine="openpyxl", datetime_format="yyyy-mm-dd hh:mm") as xw:
        readme = pd.DataFrame({
            "Field": [
                "Generated",
                "Weather provider",
                "Raster min",
                "Export timezone",
                "Plants",
                "Application"
            ],
            "Value": [
                datetime.now().isoformat(timespec="seconds"),
                weather_provider,
                res_min,
                "UTC" if export_utc else "LOCAL",
                ", ".join(results.keys()),
                "ZV Consulting Forecast"
            ]
        })
        readme.to_excel(xw, sheet_name="README", index=False)

        kpi_rows = []
        for name, r in results.items():
            kpi_rows.append({
                "plant": name,
                "dc_mwp": r["dc_kwp"] / 1000,
                "energy_mwh": r["kpi"]["total_kwh"] / 1000,
                "cf_pct": r["kpi"]["cf"] * 100,
                "weather_provider": r["meta"]["weather_provider"],
                "lat": r["meta"]["latitude"],
                "lon": r["meta"]["longitude"]
            })

        kpi_rows.append({
            "plant": "TOTAL",
            "dc_mwp": total_dc_kwp / 1000,
            "energy_mwh": total_energy.sum() / 1000,
            "cf_pct": cf_total * 100,
            "weather_provider": weather_provider,
            "lat": "",
            "lon": ""
        })

        pd.DataFrame(kpi_rows).to_excel(xw, sheet_name="KPI_summary", index=False)

        total_out = total_df[["TOTAL"]].droplevel(0, axis=1).copy()
        total_out.index = _strip_tz(total_out.index, export_utc)
        total_out.to_excel(xw, sheet_name="TOTAL_15min")

        hourly_total = total_out.resample("1h").agg({
            "p_ac_kw": "mean",
            "energy_kwh": "sum",
            "p10_kw": "mean",
            "p50_kw": "mean",
            "p90_kw": "mean",
            "energy_p10_kwh": "sum",
            "energy_p50_kwh": "sum",
            "energy_p90_kwh": "sum"
        })
        hourly_total.to_excel(xw, sheet_name="TOTAL_hourly")

        daily_total = total_daily.copy()
        daily_total.index = _strip_tz(daily_total.index, export_utc)
        daily_total.to_excel(xw, sheet_name="TOTAL_daily")

        for name, r in results.items():
            dfx = r["df"].copy()
            dfx.index = _strip_tz(dfx.index, export_utc)
            dfx.to_excel(xw, sheet_name=f"{_safe_sheet_name(name)}_15min")

            dailyx = r["daily"].copy()
            dailyx.index = _strip_tz(dailyx.index, export_utc)
            dailyx.to_excel(xw, sheet_name=f"{_safe_sheet_name(name)}_daily")

            params = pd.DataFrame(list(r["meta"].items()), columns=["parameter", "value"])
            params.to_excel(xw, sheet_name=f"{_safe_sheet_name(name)}_params", index=False)

    return xbuf.getvalue()


def build_zip_export():
    zbuf = io.BytesIO()

    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        excel_bytes = build_excel_export()
        zf.writestr("ZV_forecast_full_export.xlsx", excel_bytes)

        total_out = total_df[["TOTAL"]].droplevel(0, axis=1).copy()
        total_out.index = _strip_tz(total_out.index, export_utc)
        zf.writestr("csv/TOTAL_15min.csv", total_out.to_csv().encode("utf-8"))

        zf.writestr("json/plants_config.json", json.dumps(plants, ensure_ascii=False, indent=2).encode("utf-8"))

        for name, r in results.items():
            dfx = r["df"].copy()
            dfx.index = _strip_tz(dfx.index, export_utc)
            zf.writestr(f"csv/{safe_filename(name)}_15min.csv", dfx.to_csv().encode("utf-8"))
            zf.writestr(
                f"json/{safe_filename(name)}_parameters.json",
                json.dumps(r["meta"], ensure_ascii=False, indent=2).encode("utf-8")
            )

        html = """
        <html>
        <head><meta charset="utf-8"><title>ZV Consulting Forecast Export</title></head>
        <body>
        <h1>ZV Consulting Forecast</h1>
        <p>Export package contains Excel, CSV, JSON and model parameters.</p>
        </body>
        </html>
        """
        zf.writestr("README.html", html.encode("utf-8"))

    return zbuf.getvalue()


# ==========================================================
# CHART HELPERS
# ==========================================================

def portfolio_forecast_fig():
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=total_p90.index,
        y=total_p90,
        name="P90",
        mode="lines",
        line=dict(width=0),
        showlegend=False,
        hoverinfo="skip"
    ))

    fig.add_trace(go.Scatter(
        x=total_p10.index,
        y=total_p10,
        name="P10–P90 interval",
        mode="lines",
        line=dict(width=0),
        fill="tonexty",
        fillcolor="rgba(14,165,233,0.18)",
        hoverinfo="skip"
    ))

    fig.add_trace(go.Scatter(
        x=total_p50.index,
        y=total_p50,
        name="P50 forecast [kW]",
        mode="lines",
        line=dict(width=3),
        hovertemplate="%{x}<br>%{y:,.0f} kW<extra></extra>"
    ))

    return style_fig(fig, "Portfolio forecast — P10 / P50 / P90", ytitle="kW")


def daily_energy_fig():
    d = total_daily.reset_index()
    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=d["time"],
        y=d["energy_p50_kwh"],
        name="P50 daily energy",
        hovertemplate="%{x}<br>%{y:,.0f} kWh<extra></extra>"
    ))

    fig.add_trace(go.Scatter(
        x=d["time"],
        y=d["energy_p10_kwh"],
        name="P10",
        mode="lines+markers"
    ))

    fig.add_trace(go.Scatter(
        x=d["time"],
        y=d["energy_p90_kwh"],
        name="P90",
        mode="lines+markers"
    ))

    return style_fig(fig, "Daily energy forecast", height=340, ytitle="kWh")


def heatmap_fig(series: pd.Series, title: str):
    dfh = pd.DataFrame({"value": series})
    dfh["date"] = dfh.index.date
    dfh["hour"] = dfh.index.hour + dfh.index.minute / 60.0
    piv = dfh.pivot_table(index="hour", columns="date", values="value", aggfunc="mean")
    fig = px.imshow(
        piv,
        aspect="auto",
        title=title,
        labels=dict(x="Date", y="Hour", color="kW"),
        template=PLOT_TEMPLATE
    )
    return style_fig(fig, height=420)


# ==========================================================
# MAIN TABS
# ==========================================================

tabs = st.tabs(
    ["📌 Executive Summary", "🟡 Portfolio", "🧮 Compare", "📦 Export", "🗺️ Map"]
    + [f"🔹 {n}" for n in results.keys()]
)


# ----------------------------------------------------------
# EXECUTIVE SUMMARY
# ----------------------------------------------------------

with tabs[0]:
    st.subheader("📌 Executive Summary")

    c1, c2, c3 = st.columns(3)

    today_txt = "—"
    if not today_row.empty:
        today_txt = f"{today_row['energy_kwh'].iloc[0] / 1000:.2f} MWh"

    tomorrow_txt = "—"
    if not tomorrow_row.empty:
        tomorrow_txt = f"{tomorrow_row['energy_kwh'].iloc[0] / 1000:.2f} MWh"

    best_txt = "—"
    if best_day is not None:
        best_txt = f"{best_day[0].date()} — {best_day[1] / 1000:.2f} MWh"

    with c1:
        info_card("Today expected production", today_txt)
    with c2:
        info_card("Tomorrow expected production", tomorrow_txt)
    with c3:
        info_card("Best forecast day", best_txt)

    plotly_safe(
        portfolio_forecast_fig(),
        key="executive_portfolio_forecast"
    )   

    plotly_safe(
        daily_energy_fig(),
        key="executive_daily_energy"
    )

    st.markdown("### Operational comment")

    if not tomorrow_row.empty and not today_row.empty:
        today_mwh = today_row["energy_kwh"].iloc[0] / 1000
        tomorrow_mwh = tomorrow_row["energy_kwh"].iloc[0] / 1000
        diff = tomorrow_mwh - today_mwh
        pct = diff / today_mwh * 100 if today_mwh > 0 else np.nan

        if np.isfinite(pct):
            if pct > 10:
                st.success(f"Sutra se očekuje značajno veća proizvodnja: +{pct:.1f}% u odnosu na danas.")
            elif pct < -10:
                st.warning(f"Sutra se očekuje niža proizvodnja: {pct:.1f}% u odnosu na danas.")
            else:
                st.info(f"Sutra je forecast približno stabilan: promena {pct:.1f}% u odnosu na danas.")
    else:
        st.info("Nema dovoljno podataka za poređenje danas/sutra.")


# ----------------------------------------------------------
# PORTFOLIO TAB
# ----------------------------------------------------------

with tabs[1]:
    subt1, subt2, subt3, subt4 = st.tabs(
        ["🔋 Forecast", "📊 Heatmap", "📋 Tables", "🧰 QC"]
    )

    with subt1:
        plotly_safe(
            portfolio_forecast_fig(),
            key="portfolio_tab_forecast"
        )

        plotly_safe(
            daily_energy_fig(),
            key="portfolio_tab_daily_energy"
        )

    with subt2:
        plotly_safe(
            heatmap_fig(total_power, "Portfolio hourly power heatmap"),
            key="portfolio_heatmap"
        )

    with subt3:
        st.subheader("TOTAL — 15-min table")
        st.dataframe(total_df[["TOTAL"]].droplevel(0, axis=1).tail(300), use_container_width=True)

        st.subheader("TOTAL — daily summary")
        st.dataframe(total_daily.round(3), use_container_width=True)

    with subt4:
        st.write("Last 20 rows")
        st.dataframe(total_df[["TOTAL"]].droplevel(0, axis=1).tail(20), use_container_width=True)


# ----------------------------------------------------------
# COMPARE TAB
# ----------------------------------------------------------

with tabs[2]:
    compare = st.multiselect(
        "Odaberi elektrane za poređenje",
        list(results.keys()),
        default=list(results.keys())[:min(3, len(results))]
    )

    if compare:
        # -----------------------------
        # 1. AC POWER OVERLAY
        # -----------------------------
        figc = go.Figure()
        for n in compare:
            d = results[n]["df"]
            figc.add_trace(go.Scatter(
                x=d.index,
                y=d["p_ac_kw"],
                name=n,
                mode="lines",
                line=dict(width=2),
                hovertemplate=f"%{{x}}<br>{n}: %{{y:,.0f}} kW<extra></extra>"
            ))

        plotly_safe(
            style_fig(figc, "AC power overlay", ytitle="kW"),
            key="compare_ac_power"
        )

        # -----------------------------
        # 2. DAILY ENERGY STACK
        # -----------------------------
        stack = []
        for n in compare:
            dd = results[n]["df"]["energy_kwh"].resample("D").sum().rename(n)
            stack.append(dd)

        # 👉 OVDE se pravi stack_df (ključni deo)
        stack_df = pd.concat(stack, axis=1)
        stack_df.index.name = "time"

        figcs = go.Figure()
        for n in compare:
            figcs.add_bar(
                x=stack_df.index,
                y=stack_df[n],
                name=n
            )

        figcs.update_layout(barmode="stack", bargap=0.15)

        plotly_safe(
            style_fig(figcs, "Daily energy — stacked", height=340, ytitle="kWh"),
            key="compare_daily_energy_stacked"
        )

# ----------------------------------------------------------
# EXPORT TAB
# ----------------------------------------------------------

with tabs[3]:
    st.subheader("📦 Professional export package")

    c1, c2, c3 = st.columns(3)

    with c1:
        st.download_button(
            "⬇️ Excel full report",
            data=build_excel_export(),
            file_name=f"ZV_forecast_full_{'UTC' if export_utc else 'LOCAL'}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with c2:
        st.download_button(
            "⬇️ Full ZIP package",
            data=build_zip_export(),
            file_name=f"ZV_forecast_package_{'UTC' if export_utc else 'LOCAL'}.zip",
            mime="application/zip",
            use_container_width=True
        )

    with c3:
        total_csv = total_df[["TOTAL"]].droplevel(0, axis=1).copy()
        total_csv.index = _strip_tz(total_csv.index, export_utc)
        st.download_button(
            "⬇️ TOTAL CSV 15-min",
            data=total_csv.to_csv().encode("utf-8"),
            file_name=f"TOTAL_15min_{'UTC' if export_utc else 'LOCAL'}.csv",
            mime="text/csv",
            use_container_width=True
        )

    st.info("ZIP paket sadrži Excel, CSV fajlove po elektrani, JSON parametre i README.")


# ----------------------------------------------------------
# MAP TAB
# ----------------------------------------------------------

with tabs[4]:
    st.subheader("🗺️ Portfolio map")

    try:
        if map_rows:
            df_map = pd.DataFrame(map_rows)

            def _radius_from_cap(cap):
                try:
                    x = float(cap) * 1200.0
                except Exception:
                    x = 1200.0
                return max(500.0, min(4000.0, x))

            df_map["radius"] = df_map["cap"].apply(_radius_from_cap)

            center_lat = float(np.average(df_map["lat"]))
            center_lon = float(np.average(df_map["lon"]))

            tile_layer = pdk.Layer(
                "TileLayer",
                data="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                minZoom=0,
                maxZoom=19,
                tileSize=256
            )

            scatter = pdk.Layer(
                "ScatterplotLayer",
                data=df_map,
                get_position="[lon, lat]",
                get_radius="radius",
                pickable=True,
                radius_min_pixels=5,
                radius_max_pixels=45,
                get_fill_color=[14, 165, 233, 210],
            )

            text = pdk.Layer(
                "TextLayer",
                data=df_map,
                get_position="[lon, lat]",
                get_text="name",
                get_size=12,
                get_color=[15, 23, 42, 255],
                get_alignment_baseline="bottom",
            )

            view_state = pdk.ViewState(
                latitude=center_lat,
                longitude=center_lon,
                zoom=6,
                pitch=0
            )

            deck = pdk.Deck(
                layers=[tile_layer, scatter, text],
                initial_view_state=view_state,
                map_provider=None,
                map_style=None,
                tooltip={"text": "{name}\nlat: {lat}\nlon: {lon}\ncap: {cap} MW"}
            )

            st.pydeck_chart(deck, use_container_width=True)
        else:
            st.info("Nema podataka za mapu.")
    except Exception as e:
        st.warning("Mapa nije mogla da se prikaže.")
        st.exception(e)

tab_quality, tab_main = st.tabs(["📊 Data Quality", "📈 Forecast"])

with tab_quality:
    for name, r in results.items():
        df = r["df"]

        st.subheader(name)

        col1, col2, col3 = st.columns(3)

        col1.metric("Missing data %", f"{df.isna().mean().mean()*100:.2f}%")
        col2.metric("Avg cloud", f"{df['total_cloud_cover'].mean():.1f}%")
        col3.metric("Radiation source", df.get("source_radiation", "unknown"))

        st.write("Sources:", df.get("available_sources", ""))

# ----------------------------------------------------------
# PER-PLANT TABS
# ----------------------------------------------------------

for i, (name, r) in enumerate(results.items(), start=5):
    with tabs[i]:
        df = r["df"]
        daily = r["daily"]
        cs = r["cs"]
        meta = r["meta"]
        dc_kwp = r["dc_kwp"]
        ac_cap = meta.get("ac_cap_mw")

        st.caption(
            f"**{name}** — Lat/Lon: {meta['latitude']:.5f}, {meta['longitude']:.5f} • "
            f"TZ: {meta['timezone']} • Weather: {meta['weather_provider']} • "
            f"{'Tracker ON' if meta['tracker'] else 'Fixed-tilt'} • "
            f"Bias: {meta['bias_pct']:+.0f}%"
        )

        colA, colB, colC, colD, colE = st.columns(5)

        span_h_p = (df.index[-1] - df.index[0]).total_seconds() / 3600 if len(df) > 0 else 0
        avg_kw_p = r["kpi"]["total_kwh"] / span_h_p if span_h_p > 0 else 0

        today_p = pd.Timestamp.now(tz=meta["timezone"]).normalize()
        tomorrow_p = today_p + pd.Timedelta(days=1)
        today_row_p = daily.loc[daily.index.normalize() == today_p] if not daily.empty else pd.DataFrame()
        tomorrow_row_p = daily.loc[daily.index.normalize() == tomorrow_p] if not daily.empty else pd.DataFrame()

        kpi_card(colA, "DC [MWp]", f"{dc_kwp / 1000:.2f}")
        kpi_card(colB, "Energy [MWh]", f"{r['kpi']['total_kwh'] / 1000:.2f}")
        kpi_card(colC, "Avg power [MW]", f"{avg_kw_p / 1000:.2f}")
        kpi_card(colD, "CF", f"{r['kpi']['cf'] * 100:.1f} %")
        if not tomorrow_row_p.empty:
            kpi_card(colE, "Tomorrow", f"{tomorrow_row_p['energy_kwh'].iloc[0] / 1000:.2f} MWh")
        else:
            kpi_card(colE, "Tomorrow", "—")

        sub1, sub2, sub3, sub4, sub5, sub6 = st.tabs(
            ["🔋 Production", "☀️ Irradiance", "🌤️ Meteo", "📊 Statistics", "📋 Tables", "📦 Export"]
        )

        with sub1:
            fig = go.Figure()

            fig.add_trace(go.Scatter(
                x=df.index,
                y=df["p90_kw"],
                name="P90",
                mode="lines",
                line=dict(width=0),
                showlegend=False,
                hoverinfo="skip"
            ))

            fig.add_trace(go.Scatter(
                x=df.index,
                y=df["p10_kw"],
                name="P10–P90 interval",
                mode="lines",
                line=dict(width=0),
                fill="tonexty",
                fillcolor="rgba(14,165,233,0.18)",
                hoverinfo="skip"
            ))

            fig.add_trace(go.Scatter(
                x=df.index,
                y=df["p50_kw"],
                name="P50 AC [kW]",
                mode="lines",
                line=dict(width=3),
                hovertemplate="%{x}<br>%{y:,.0f} kW<extra></extra>"
            ))

            if ac_cap is not None:
                fig.add_hline(
                    y=ac_cap * 1000.0,
                    line_dash="dash",
                    annotation_text=f"AC limit {ac_cap:.2f} MW",
                    annotation_position="top left"
                )

            safe_name = safe_filename(name)

            plotly_safe(
                style_fig(fig, f"{name} — AC forecast P10/P50/P90", ytitle="kW"),
                key=f"{safe_name}_production"
            )

            d_daily = daily.reset_index()
            fig2 = go.Figure()
            fig2.add_bar(x=d_daily["time"], y=d_daily["energy_p50_kwh"], name="P50 energy")
            fig2.add_scatter(x=d_daily["time"], y=d_daily["energy_p10_kwh"], name="P10", mode="lines+markers")
            fig2.add_scatter(x=d_daily["time"], y=d_daily["energy_p90_kwh"], name="P90", mode="lines+markers")
            plotly_safe(
                style_fig(fig2, f"{name} — daily energy forecast", height=340, ytitle="kWh"),
                key=f"{safe_name}_daily_energy"
            )

        with sub2:
            c1, c2 = st.columns(2)

            with c1:
                f1 = go.Figure()
                for c in ["GHI", "DHI", "DNI"]:
                    f1.add_trace(go.Scatter(x=df.index, y=df[c], name=c, mode="lines"))
                f1.add_trace(go.Scatter(
                    x=df.index,
                    y=cs["cs_GHI"].reindex(df.index),
                    name="Clear-sky GHI",
                    line=dict(dash="dot")
                ))
                plotly_safe(
                    style_fig(f1, "Irradiance + clear-sky", height=340, ytitle="W/m²"),
                    key=f"{safe_name}_irradiance_clear_sky"
                )

            with c2:
                f2 = go.Figure()
                f2.add_trace(go.Scatter(x=df.index, y=df["poa_global"], name="POA"))
                f2.add_trace(go.Scatter(x=df.index, y=df["poa_global_cs"], name="POA clear-sky", line=dict(dash="dot")))
                st.plotly_chart(style_fig(f2, "POA global", height=340, ytitle="W/m²"), use_container_width=True)

            f3 = go.Figure()
            f3.add_trace(go.Scatter(x=df.index, y=df["clear_sky_index"], name="Clear-sky index"))
            st.plotly_chart(style_fig(f3, "Clear-sky index", height=300), use_container_width=True)

        with sub3:
            c1, c2 = st.columns(2)

            with c1:
                fm1 = go.Figure()
                fm1.add_trace(go.Scatter(x=df.index, y=df["temperature_2m"], name="Air temp [°C]"))
                fm1.add_trace(go.Scatter(x=df.index, y=df["t_cell"], name="Cell temp [°C]"))
                st.plotly_chart(style_fig(fm1, "Temperature", height=340, ytitle="°C"), use_container_width=True)

            with c2:
                fm2 = go.Figure()
                fm2.add_trace(go.Scatter(x=df.index, y=df["total_cloud_cover"], name="Cloud [%]"))
                fm2.add_trace(go.Scatter(x=df.index, y=df["wind_speed"], name="Wind [m/s]", yaxis="y2"))
                fm2.update_layout(
                    yaxis=dict(title="%"),
                    yaxis2=dict(title="m/s", overlaying="y", side="right")
                )
                st.plotly_chart(style_fig(fm2, "Cloud cover and wind", height=340), use_container_width=True)

        with sub4:
            c1, c2 = st.columns(2)

            with c1:
                hist = px.histogram(df, x="p_ac_kw", nbins=60, title="AC power distribution", template=PLOT_TEMPLATE)
                st.plotly_chart(style_fig(hist, height=340), use_container_width=True)

            with c2:
                scat = px.scatter(df, x="poa_global", y="p_ac_kw", title="POA vs AC power", template=PLOT_TEMPLATE)
                st.plotly_chart(style_fig(scat, height=340), use_container_width=True)

            corr_cols = [
                c for c in [
                    "p_ac_kw", "poa_global", "GHI", "DNI", "DHI",
                    "temperature_2m", "t_cell", "total_cloud_cover",
                    "wind_speed", "clear_sky_index"
                ] if c in df.columns
            ]

            if corr_cols:
                corr = df[corr_cols].corr().round(2)
                heat = px.imshow(
                    corr,
                    text_auto=True,
                    title="Correlation matrix",
                    zmin=-1,
                    zmax=1,
                    color_continuous_scale="Viridis",
                    template=PLOT_TEMPLATE
                )
                st.plotly_chart(style_fig(heat, height=420), use_container_width=True)

        with sub5:
            base_cols = [
                "p_ac_kw", "p10_kw", "p50_kw", "p90_kw",
                "energy_kwh", "energy_p10_kwh", "energy_p50_kwh", "energy_p90_kwh",
                "poa_global", "poa_global_cs",
                "GHI", "DNI", "DHI",
                "temperature_2m", "t_cell",
                "total_cloud_cover", "wind_speed",
                "clear_sky_index", "sun_up", "cf_instant_%"
            ]
            show_cols = [c for c in base_cols if c in df.columns]
            st.subheader("15-min data")
            st.dataframe(df[show_cols].tail(300), use_container_width=True)

            st.subheader("Daily summary")
            st.dataframe(daily.round(3), use_container_width=True)

        with sub6:
            c1, c2, c3 = st.columns(3)

            with c1:
                dfo = df.copy()
                dfo.index = _strip_tz(dfo.index, export_utc)
                st.download_button(
                    f"⬇️ CSV 15-min",
                    data=dfo.to_csv().encode("utf-8"),
                    file_name=f"{safe_filename(name)}_15min.csv",
                    mime="text/csv",
                    use_container_width=True
                )

            with c2:
                hourly = df.resample("1h").agg(
                    p_ac_kw=("p_ac_kw", "mean"),
                    p10_kw=("p10_kw", "mean"),
                    p50_kw=("p50_kw", "mean"),
                    p90_kw=("p90_kw", "mean"),
                    energy_kwh=("energy_kwh", "sum"),
                    poa_global=("poa_global", "mean"),
                    GHI=("GHI", "mean"),
                    DNI=("DNI", "mean"),
                    DHI=("DHI", "mean"),
                    cloud=("total_cloud_cover", "mean")
                )
                hourly.index = _strip_tz(hourly.index, export_utc)
                st.download_button(
                    f"⬇️ CSV hourly",
                    data=hourly.to_csv().encode("utf-8"),
                    file_name=f"{safe_filename(name)}_hourly.csv",
                    mime="text/csv",
                    use_container_width=True
                )

            with c3:
                st.download_button(
                    f"⬇️ Parameters JSON",
                    data=json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
                    file_name=f"{safe_filename(name)}_parameters.json",
                    mime="application/json",
                    use_container_width=True
                )


st.caption("© 2026 ZV Consulting Forecast")