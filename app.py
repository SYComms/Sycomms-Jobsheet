"""
SY Comms — Engineer Job Sheets
Mobile-first Streamlit app for engineers on site.

Flow:  log in  →  pick today's job (pulled from Zoho)  →  fill in the sheet
       →  customer + engineer sign on screen  →  "Complete & sync job"
       →  PDF + note saved to the Zoho account, appointment marked complete,
          customer and office emailed.

Files:  app.py (this), zoho_client.py (Zoho API), pdf_builder.py (PDF)
"""
from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import io
import json
import re
import smtplib
import ssl
import uuid
from datetime import date, datetime, time as dtime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import streamlit as st
from PIL import Image, ImageOps

from pdf_builder import build_pdf
import zoho_client as _zc
from zoho_client import ZohoClient, ZohoError, demo_jobs

CODE_VERSION = getattr(_zc, "CODE_VERSION", "old")
if CODE_VERSION == "old" or not hasattr(ZohoClient, "is_deals"):
    st.error("zoho_client.py on GitHub is an older version than app.py. Upload all the files from the "
             "latest zip (app.py, zoho_client.py and pdf_builder.py), then reboot the app.")
    st.stop()

UK = ZoneInfo("Europe/London")
DATA = Path(__file__).parent / "data"
DRAFTS = DATA / "drafts"
HISTORY = DATA / "jobsheets"
for _d in (DRAFTS, HISTORY):
    _d.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="SY Comms Job Sheets", page_icon="🛠️", layout="centered")

# =====================================================================
#  EDITABLE LISTS — change these to suit the kit you install
# =====================================================================
VISIT_TYPES = [
    "New installation",
    "Fault / repair",
    "Additional equipment / moves & changes",
    "Maintenance / health check",
    "Site survey",
    "Collection / removal of equipment",
]
FAULT_TYPES = {"Fault / repair", "Maintenance / health check"}
COVER_OPTIONS = ["Phone system", "Broadband & network"]

QUOTE_PLATFORM = "UCM"      # the platform quotes from the quotation tool are sold on
PLATFORMS = ["UCM", "Legacy"]
HANDSET_MAKES = ["Grandstream", "Yealink", "Mix", "Other"]
HANDSET_TYPES = ["Desk phone", "Cordless", "DECT", "Softphone"]
SOFTWARE = ["Horizon Collaborate", "Horizon Integrator", "3CX app", "Yealink USB Connect",
            "Wildix Collaboration", "Microsoft Teams", "Call recording portal"]
FEATURES = ["Voicemail", "Voicemail to email", "Call groups / hunt groups", "Auto-attendant / IVR",
            "Call recording", "Out-of-hours routing", "Call queues", "Music on hold", "Call Scope"]
BB_TYPES = ["Full Fibre (FTTP)", "Fibre to cabinet (FTTC)", "SoGEA", "Ethernet leased line", "4G / 5G", "Other"]
# Broadband packages — same list as the SY Comms pricebook (quoting app). Keep in step with it.
BB_PACKAGES = ["FTTP 40/10 Unlimited", "FTTP 80/20 Unlimited", "FTTP 115/20 Unlimited", "FTTP 160/30 Unlimited",
               "FTTP 220/30 Unlimited", "FTTP 330/50 Unlimited", "FTTP 550/75 Unlimited", "FTTP 1000/115 Unlimited",
               "SOGEA 40/10 Unlimited", "SOGEA 80/20 Unlimited", "Leased Line / Other"]
PROVIDERS = ["SY", "Gamma", "BT Wholesale", "TalkTalk Business", "Zen", "Openreach (via SY)", "Customer's own"]
ROUTERS = ["Draytek Vigor 2927 (FTTP/SoGEA)", "Draytek 2927LAC (FTTP/Leased Line)", "Grandstream GWN7062E (FTTP)",
           "Zyxel DX Series (FTTP)", "TP Link NX200 (4G/5G)", "Customer's own router"]  # pricebook routers
NETWORK_CHECKS = ["Internet working", "Wi-Fi tested", "Wired connection tested", "Speed test completed"]
CALL_TESTS = ["Inbound", "Outbound", "Internal", "Emergency call process explained"]
FAULT_CATEGORIES = ["No inbound calls", "No outbound calls", "No dial tone / handsets offline", "Call quality / one-way audio",
                    "Handset fault", "Voicemail / call routing", "Broadband down", "Slow broadband",
                    "Wi-Fi coverage / drop-outs", "Router fault", "Power / cabling", "Other"]
ROOT_CAUSES = ["Configuration / routing", "Handset hardware", "Router / network equipment", "Cabling / patching",
               "Carrier / provider network fault", "Power issue", "Customer equipment / changes",
               "User training", "No fault found", "Other"]
FAULT_OUTCOMES = ["Resolved", "Temporary fix", "Return visit needed", "Escalated to carrier / provider"]
BILLING = ["Covered under contract", "Chargeable", "Office to confirm"]
UNAVAILABLE_REASONS = ["Customer not on site", "Customer declined to sign", "Signed on paper", "Other"]

# =====================================================================
#  SETTINGS / SECRETS
# =====================================================================
def secret(section, key=None, default=None):
    try:
        s = st.secrets[section]
        return s if key is None else s.get(key, default)
    except Exception:
        return default


BRAND = {
    "name": secret("brand", "name", "SY Comms"),
    "phone": secret("brand", "phone", "01743 667419"),
    "email": secret("brand", "email", "hello@sycomms.co.uk"),
    "office_email": secret("brand", "office_email", ""),
    "signoff": secret("brand", "signoff", "Kind regards - Sy Communications"),
}
DEMO = bool(secret("app", "demo_mode", False)) or secret("zoho") is None


def engineer_names():
    """Names offered on the login screen: [app] engineer_names in Secrets, else Zoho users."""
    names = list(secret("app", "engineer_names", []) or [])
    if not names and not DEMO and zoho_client():
        try:
            names = sorted({u["name"] for u in zoho_client().users().values() if u.get("name")})
        except Exception:
            names = []
    return names


@st.cache_resource(show_spinner=False)
def _zoho_for(code_version: str, cfg_json: str):
    return ZohoClient(json.loads(cfg_json))


def zoho_client():
    """One shared Zoho connection, rebuilt automatically whenever the code or the
    [zoho] Secrets change (so a stale connection can never linger after an update)."""
    cfg = secret("zoho")
    over = st.session_state.get("_zoho_over")
    if not cfg and not over:
        return None
    cfg = dict(cfg or {})
    if over:  # a token just made on the Zoho connection page, used before Secrets are updated
        cfg.update(over)
    return _zoho_for(CODE_VERSION, json.dumps(cfg, sort_keys=True, default=str))


# The scope this app needs. Everything it does: read the diary (Deals + Meetings) and customers,
# add the note + PDF, send emails through Zoho, list sending addresses and users.
ZOHO_SCOPE = ("ZohoCRM.modules.ALL,ZohoCRM.settings.emails.READ,ZohoCRM.send_mail.all.CREATE,"
              "ZohoCRM.Files.CREATE,ZohoCRM.users.READ,ZohoCRM.coql.READ")


def _zoho_checks(cfg):
    """Try each thing the app needs with these credentials; returns [(label, ok, note)]."""
    z = ZohoClient(cfg)
    out = []
    tests = [
        ("Log in to Zoho", lambda: z._access_token()),
        ("Read the diary — Deals", lambda: z._req("GET", "Deals", params={"fields": "Deal_Name", "per_page": 1})),
        ("Read the diary — Meetings", lambda: z._req("GET", "Events", params={"fields": "Event_Title", "per_page": 1})),
        ("Read customer accounts", lambda: z._req("GET", "Accounts", params={"fields": "Account_Name", "per_page": 1})),
        ("List sending addresses (hello@, support@…)", lambda: z.from_addresses()),
        ("List Zoho users (names on the login screen)", lambda: z._req("GET", "users", params={"type": "ActiveUsers", "per_page": 1})),
    ]
    for label, fn in tests:
        try:
            fn()
            out.append((label, True, ""))
        except Exception as e:
            out.append((label, False, str(e)[:160]))
        if label == "Log in to Zoho" and not out[-1][1]:
            break
    return out


def zoho_setup_ui(kp="_zt"):
    """Same flow as the quotation tool: copy the scope, paste the grant code, get a refresh token."""
    zs = dict(secret("zoho") or {})
    acc_url = zs.get("accounts_url", "https://accounts.zoho.eu").rstrip("/")
    st.markdown("**1. At api-console.zoho.eu → Self Client → Generate Code, paste this scope:**")
    st.code(ZOHO_SCOPE, language=None, wrap_lines=True)
    st.caption("Set Time Duration to 10 minutes and click Create. The code only works once and expires quickly, "
               "so paste it below straight away.")
    cid = st.text_input("Client ID", value=zs.get("client_id", ""), key=f"{kp}_cid")
    have_cs = zs.get("client_secret", "")
    cs = st.text_input("Client Secret", value="", type="password", key=f"{kp}_cs",
                       placeholder=(f"Leave blank to use the one in Secrets ({len(have_cs)} characters)"
                                    if have_cs else "Paste the client secret"))
    st.markdown("**2. Paste the code Zoho gives you:**")
    code = st.text_input("Grant code", key=f"{kp}_code", placeholder="1000.xxxxxxxx.xxxxxxxx")
    if st.button("Create refresh token", type="primary", key=f"{kp}_make", **FULL):
        clean = lambda v: "".join(str(v or "").split()).strip("\"'")
        cid_c, code_c, cs_c = clean(cid), clean(code), clean(cs or have_cs)
        if not (cid_c and cs_c and code_c):
            st.error("Fill in the Client ID, Client Secret and the grant code first.")
        elif len(cs_c) < 30:
            st.error(f"The Client Secret looks too short ({len(cs_c)} characters — a real one is about 42). "
                     "Nothing was sent to Zoho, so your grant code is still unused. Paste the full secret and try again.")
        else:
            try:
                r = requests.post(f"{acc_url}/oauth/v2/token", timeout=20, data={
                    "grant_type": "authorization_code", "client_id": cid_c, "client_secret": cs_c, "code": code_c})
                try:
                    j = r.json()
                except ValueError:
                    j = None
                if j and j.get("refresh_token"):
                    ss[f"{kp}_result"] = {"rt": j["refresh_token"], "cid": cid_c, "cs": cs_c,
                                          "scope": j.get("scope", "")}
                    ss.pop(f"{kp}_checks", None)
                elif j:
                    ss.pop(f"{kp}_result", None)
                    err = j.get("error", r.status_code)
                    hint = {"invalid_code": "the code has already been used or has expired — generate a fresh one and paste it straight away.",
                            "invalid_client": "the Client ID or Client Secret doesn't match — check they're from the same Self Client.",
                            "invalid_client_secret": "the Client Secret is wrong — paste the full secret."}.get(str(err), "generate a fresh code and try again.")
                    st.error(f"Zoho said '{err}': {hint}")
                else:
                    st.error(f"Zoho rejected the request (status {r.status_code}). Most likely the Client Secret is wrong or cut off.")
            except requests.RequestException as e:
                st.error(f"Couldn't reach Zoho: {e}")

    res = ss.get(f"{kp}_result")
    if res:
        st.success("Refresh token created. In Streamlit → Manage app → Settings → Secrets, replace the "
                   "refresh_token line under [zoho] with this, save, then reboot the app:")
        st.code(f'refresh_token = "{res["rt"]}"', language="toml", wrap_lines=True)
        if res["cid"] != zs.get("client_id", ""):
            st.caption("You used a different Client ID from the one in Secrets — update client_id and client_secret too.")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Check what it can do", icon=":material/fact_check:", key=f"{kp}_check", **FULL):
                cfg = dict(zs, client_id=res["cid"], client_secret=res["cs"], refresh_token=res["rt"])
                with st.spinner("Checking with Zoho…"):
                    ss[f"{kp}_checks"] = _zoho_checks(cfg)
        with c2:
            if st.button("Use it now (until Secrets are updated)", icon=":material/bolt:", key=f"{kp}_use", **FULL):
                ss["_zoho_over"] = {"client_id": res["cid"], "client_secret": res["cs"], "refresh_token": res["rt"]}
                try:
                    fetch_jobs.clear()
                except NameError:
                    pass
                st.success("This session is now using the new token. Still paste it into Secrets so it sticks.")
        for label, ok, note in ss.get(f"{kp}_checks") or []:
            st.markdown(f"{'✅' if ok else '❌'} **{esc(label)}**" + (f"  \n<span style='color:var(--muted);font-size:.8rem'>{esc(note)}</span>" if note else ""),
                        unsafe_allow_html=True)


def zoho_setup_gate():
    """Office-only panel on the login screen, behind the admin password (or the app password if none is set)."""
    with st.expander("Office: Zoho connection", icon=":material/key:"):
        admin_pw = secret("app", "admin_password") or secret("app", "password")
        if not ss.get("_zoho_admin"):
            pw = st.text_input("Admin password", type="password", key="_zt_admin_pw")
            if st.button("Unlock", key="_zt_admin_go", icon=":material/lock_open:"):
                if admin_pw and hmac.compare_digest(str(admin_pw), (pw or "").strip()):
                    ss["_zoho_admin"] = True
                    st.rerun()
                st.error("That password isn't right.")
            return
        zoho_setup_ui()


# =====================================================================
#  DESIGN
# =====================================================================
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root{--bg:#0A0E1A;--surface:#111827;--surface-2:#161F33;--surface-3:#1C2740;--border:rgba(148,163,184,.14);--border-strong:rgba(148,163,184,.26);--text:#E7EAF3;--muted:#8C98B0;--faint:#5E6A82;--accent:#7C83FF;--accent-2:#38D6F5;--accent-soft:rgba(124,131,255,.14);--good:#34D399;--warn:#FBBF24;--risk:#FB923C;--bad:#F87171;--radius:14px;--grad:linear-gradient(135deg,#7C83FF 0%,#38D6F5 100%);}
html,body,[class*="css"],.stApp,button,input,textarea,select{font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif!important}
.stApp{background:radial-gradient(1200px 500px at 85% -10%,rgba(56,214,245,.07),transparent 60%),radial-gradient(900px 500px at 10% -20%,rgba(124,131,255,.10),transparent 60%),var(--bg)}
[data-testid="stHeader"]{background:transparent}[data-testid="stDecoration"]{display:none}footer{visibility:hidden}
.block-container{padding-top:1.2rem!important;padding-bottom:4rem!important;max-width:880px}
[data-testid="stWidgetLabel"] p{font-size:.74rem!important;font-weight:600!important;color:var(--muted)!important;text-transform:uppercase;letter-spacing:.06em}
[data-testid="stCaptionContainer"]{color:var(--muted)!important}
.st-key-card-login,[class*="st-key-card-"]{background:linear-gradient(180deg,rgba(22,31,51,.85) 0%,rgba(17,24,39,.85) 100%);border:1px solid var(--border)!important;border-radius:var(--radius);padding:20px 20px 16px;box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 20px 40px -24px rgba(0,0,0,.6);margin-bottom:14px}
[class*="st-key-card-job-done"]{border-color:rgba(52,211,153,.35)!important}
[data-baseweb="input"],[data-baseweb="select"]>div,[data-baseweb="textarea"]{background:var(--surface)!important;border:1px solid var(--border-strong)!important;border-radius:10px!important}
[data-baseweb="input"]:focus-within,[data-baseweb="select"]>div:focus-within,[data-baseweb="textarea"]:focus-within{border-color:var(--accent)!important;box-shadow:0 0 0 3px var(--accent-soft)!important}
[data-baseweb="input"]>div,[data-baseweb="base-input"]{background:transparent!important}
[data-testid="stTextInputRootElement"],[data-testid="stTextAreaRootElement"],[data-testid="stNumberInputContainer"],[data-testid="stDateInputField"],[data-testid="stTimeInputTimeDisplay"],[data-testid="stSelectbox"] [role="group"],[data-testid="stMultiSelect"] [role="group"]{background:var(--surface)!important;border:1px solid var(--border-strong)!important;border-radius:10px!important}
[data-testid="stTextInputRootElement"]:focus-within,[data-testid="stTextAreaRootElement"]:focus-within,[data-testid="stNumberInputContainer"]:focus-within,[data-testid="stDateInputField"]:focus-within,[data-testid="stTimeInputTimeDisplay"]:focus-within,[data-testid="stSelectbox"] [role="group"]:focus-within,[data-testid="stMultiSelect"] [role="group"]:focus-within{border-color:var(--accent)!important;box-shadow:0 0 0 3px var(--accent-soft)!important}
.stButton button,.stDownloadButton button,.stFormSubmitButton button{border-radius:10px!important;font-weight:600!important;border:1px solid var(--border-strong)!important;background:var(--surface-2)!important;color:var(--text)!important;transition:all .15s ease;min-height:44px}
.stButton button:hover,.stDownloadButton button:hover{border-color:var(--accent)!important;transform:translateY(-1px)}
.stButton button[kind="primary"],.stDownloadButton button[kind="primary"],.stFormSubmitButton button,[data-testid="stBaseButton-primary"]{background:var(--grad)!important;border:none!important;color:#0A0E1A!important;box-shadow:0 8px 24px -10px rgba(124,131,255,.8)}
.stButton button[kind="primary"] p,[data-testid="stBaseButton-primary"] p,.stFormSubmitButton button p,.stDownloadButton button[kind="primary"] p{color:#0A0E1A!important;font-weight:700!important}
.stButton button:disabled{opacity:.45;transform:none}
[data-testid="stExpander"] details{background:var(--surface);border:1px solid var(--border)!important;border-radius:12px!important}
[data-testid="stAlert"]{border-radius:12px!important}
[data-testid="stCheckbox"] label p{font-size:.92rem}
/* pills + segmented: bigger tap targets */
[data-testid="stButtonGroup"] button{min-height:38px;border-radius:999px!important}
/* Selected options (Yes/No, features, etc.) highlight GREEN */
[data-testid="stButtonGroup"] button[data-selected="true"]{background:rgba(52,211,153,.16)!important;border-color:var(--good)!important;color:var(--good)!important;box-shadow:0 0 0 1px rgba(52,211,153,.35) inset}
[data-testid="stButtonGroup"] button[data-selected="true"] *{color:var(--good)!important}
[data-testid="stButtonGroup"] button:not([data-selected="true"]){background:var(--surface)!important;border-color:var(--border-strong)!important;color:var(--text)!important}
[data-testid="stButtonGroup"] button:not([data-selected="true"]) *{color:var(--text)!important}
/* Force the dark palette even if a browser/theme setting tries to go light */
html,body,.stApp{color-scheme:dark}
.stApp,.stApp p,.stApp label,.stApp li,[data-testid="stMarkdownContainer"]{color:var(--text)}
input,textarea,[data-baseweb="select"] *{color:var(--text)!important;-webkit-text-fill-color:var(--text)}
input::placeholder,textarea::placeholder{color:var(--faint)!important;-webkit-text-fill-color:var(--faint)}
[data-baseweb="popover"] [role="listbox"],[data-baseweb="popover"] ul,[data-baseweb="menu"]{background:var(--surface-2)!important}
[data-baseweb="popover"] li{color:var(--text)!important}
[data-baseweb="popover"] li[aria-selected="true"],[data-baseweb="popover"] li:hover{background:var(--surface-3)!important}
[data-baseweb="calendar"],[data-baseweb="calendar"] *{background-color:var(--surface-2);color:var(--text)}
/* Ticked checkboxes + switched-on toggles: GREEN (never Streamlit's default red) */
[data-testid="stCheckbox"] label[data-selected="true"] > div:first-of-type,[data-testid="stCheckbox"] label:has(input:checked) > div:first-of-type{background:var(--good)!important;border-color:var(--good)!important}
[data-testid="stCheckbox"] label[data-selected="true"] > div:first-of-type svg{color:#0A0E1A!important;stroke:#0A0E1A!important}
[data-testid="stCheckbox"] label:not([data-selected="true"]):not(:has(input[role="switch"])) > div:first-of-type{background:var(--surface)!important;border-color:var(--border-strong)!important}
label[data-selected="true"]:has(input[role="switch"]) > div:first-of-type,label:has(input[role="switch"]:checked) > div:first-of-type{background:var(--good)!important;border-color:var(--good)!important}
label:not([data-selected="true"]):has(input[role="switch"]) > div:first-of-type{background:var(--surface-3)!important}
/* Dropdown lists (select / multiselect) */
[data-testid="stSelectboxVirtualDropdown"],[data-testid="stMultiSelectVirtualDropdown"],[role="listbox"]{background:var(--surface-2)!important;color:var(--text)!important}
[role="option"]{color:var(--text)!important;background:transparent}
[role="option"]:hover,[role="option"][aria-selected="true"],[role="option"][data-focused="true"]{background:var(--surface-3)!important}
[data-baseweb="tag"],[data-testid="stMultiSelectTagsContainer"] > span{background:rgba(52,211,153,.16)!important;color:var(--good)!important;border:1px solid rgba(52,211,153,.4)!important}
[data-baseweb="tag"] *,[data-testid="stMultiSelectTagsContainer"] > span *{color:var(--good)!important;background:transparent!important}
[data-testid="stMultiSelectTagsContainer"] > span,[data-testid="stMultiSelectTagsContainer"] > span > *:first-child{background-color:rgba(52,211,153,.16)!important;border-radius:8px}
/* Icons inside inputs (dropdown arrows, +/-, show-password eye) */
[data-testid="stSelectbox"] svg,[data-testid="stMultiSelect"] svg,[data-testid="stDateInputField"] svg,[data-testid="stTextInputRootElement"] svg{color:var(--muted)!important}
[data-testid^="stNumberInputStep"]{color:var(--text)!important;background:transparent!important;opacity:.85}
[data-testid^="stNumberInputStep"]:disabled{opacity:.3}
[data-testid^="stNumberInputStep"]:not(:disabled):hover{color:var(--text)!important;background:var(--surface-3)!important}
/* Photo upload box */
[data-testid="stFileUploaderDropzone"]{background:var(--surface)!important;border:1px dashed var(--border-strong)!important;border-radius:12px!important}
[data-testid="stFileUploaderDropzone"] button{background:var(--surface-2)!important;color:var(--text)!important;border:1px solid var(--border-strong)!important}
[data-testid="stFileUploaderDropzone"] *{color:var(--muted)}
[data-testid="stFileUploaderDropzone"] button *{color:var(--text)!important}
[data-testid="stFileUploaderFile"] *{color:var(--text)}
/* Alerts / status box stay readable */
[data-testid="stAlert"] *{color:var(--text)!important}
[data-testid="stExpander"] summary *{color:var(--text)!important}
/* hero */
.pe-hero{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;padding:4px 2px 18px;margin-bottom:16px;border-bottom:1px solid var(--border)}
.pe-eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:.7rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--accent-2);margin-bottom:8px}
.pe-eyebrow .dot{width:7px;height:7px;border-radius:50%;background:var(--good);box-shadow:0 0 0 4px rgba(52,211,153,.15)}
.pe-eyebrow .dot.demo{background:var(--warn);box-shadow:0 0 0 4px rgba(251,191,36,.15)}
.pe-title{font-size:1.85rem;font-weight:800;letter-spacing:-.035em;line-height:1.1;color:var(--text)}
.pe-title span{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.pe-sub{color:var(--muted);font-size:.92rem;margin-top:8px;max-width:560px}
.pe-stepper{display:flex;align-items:center;gap:4px;background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:5px;overflow-x:auto;max-width:100%}
.pe-step{display:flex;align-items:center;gap:7px;padding:6px 12px 6px 6px;border-radius:999px;font-size:.78rem;font-weight:600;color:var(--faint);white-space:nowrap}
.pe-step .num{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:.7rem;font-weight:700;border:1px solid var(--border-strong)}
.pe-step.done{color:var(--muted)}.pe-step.done .num{background:rgba(52,211,153,.14);border-color:rgba(52,211,153,.45);color:var(--good)}
.pe-step.active{background:var(--surface-3);color:var(--text)}.pe-step.active .num{background:var(--grad);border:none;color:#0A0E1A}
.pe-step-sep{width:10px;height:1px;background:var(--border-strong);flex-shrink:0}
.pe-section{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.pe-section .badge{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent);font-weight:800;font-size:.85rem;border:1px solid rgba(124,131,255,.3);flex-shrink:0}
.pe-section .t{font-size:1.05rem;font-weight:700;color:var(--text)}.pe-section .s{font-size:.8rem;color:var(--muted);margin-top:2px}
.pe-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:.74rem;font-weight:600;background:var(--surface-3);color:var(--text);border:1px solid var(--border);white-space:nowrap;margin:0 6px 6px 0}
.pe-chip.accent{background:var(--accent-soft);color:#B9BDFF;border-color:rgba(124,131,255,.3)}
.pe-chip.good{background:rgba(52,211,153,.12);color:var(--good);border-color:rgba(52,211,153,.3)}
.pe-chip.warn{background:rgba(251,191,36,.12);color:var(--warn);border-color:rgba(251,191,36,.3)}
.pe-chip.bad{background:rgba(248,113,113,.12);color:var(--bad);border-color:rgba(248,113,113,.3)}
.pe-logo{width:40px;height:40px;border-radius:12px;background:var(--grad);display:grid;place-items:center;color:#0A0E1A;box-shadow:0 10px 24px -10px rgba(124,131,255,.9);flex-shrink:0}
.pe-login-head{text-align:center;margin:7vh 0 22px}.pe-login-head .pe-logo{width:54px;height:54px;margin:0 auto 16px;border-radius:16px}
.pe-login-head .t{font-size:1.6rem;font-weight:800;letter-spacing:-.03em;color:var(--text)}.pe-login-head .s{color:var(--muted);font-size:.92rem;margin-top:6px}
.pe-brand{display:flex;align-items:center;gap:10px}.pe-brand .n{font-weight:800;color:var(--text);letter-spacing:-.02em}.pe-brand .u{font-size:.78rem;color:var(--muted)}
.job-time{font-size:.8rem;font-weight:700;color:var(--accent-2);letter-spacing:.02em}
.job-title{font-size:1.08rem;font-weight:700;color:var(--text);margin:4px 0 2px}
.job-cust{font-size:.9rem;color:var(--muted);margin-bottom:10px}
.pe-note{background:rgba(56,214,245,.07);border:1px solid rgba(56,214,245,.25);border-radius:12px;padding:12px 14px;color:var(--text);font-size:.9rem;margin-bottom:14px}
.pe-note b{color:var(--accent-2);font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;display:block;margin-bottom:4px}
.pe-empty{text-align:center;padding:26px 10px 8px}
.pe-empty .ic{width:52px;height:52px;border-radius:14px;margin:0 auto 12px;display:grid;place-items:center;background:var(--accent-soft);color:var(--accent)}
.pe-empty .t{font-weight:700;color:var(--text);font-size:1.05rem}
.pe-empty ol{display:inline-block;text-align:left;color:var(--muted);font-size:.88rem;margin-top:10px}
.pe-step-row{display:flex;align-items:flex-start;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)}
.pe-step-row .ico{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-size:.8rem;font-weight:800;flex-shrink:0}
.pe-step-row .ico.ok{background:rgba(52,211,153,.15);color:var(--good)}.pe-step-row .ico.fail{background:rgba(248,113,113,.15);color:var(--bad)}.pe-step-row .ico.skip{background:var(--surface-3);color:var(--muted)}
.pe-step-row .l{font-weight:600;color:var(--text);font-size:.92rem}.pe-step-row .d{color:var(--muted);font-size:.8rem;margin-top:2px}
.pe-dayhead{font-size:.74rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--accent-2);margin:14px 2px 8px}
.pe-siglabel{font-size:.74rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:6px 0 6px}
[data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p,[data-testid="stToggle"] [data-testid="stWidgetLabel"] p,.stCheckbox [data-testid="stWidgetLabel"] p{text-transform:none!important;letter-spacing:0!important;font-size:.92rem!important;font-weight:500!important;color:var(--text)!important}
.st-key-topbar{justify-content:space-between;margin-bottom:6px}.st-key-topbar .stButton button{min-height:38px}
@media (max-width:640px){.pe-step:not(.active) .lb{display:none}.pe-step:not(.active){padding:5px}.pe-stepper{width:fit-content}}
@media (max-width:640px){.pe-title{font-size:1.5rem}.block-container{padding-left:12px!important;padding-right:12px!important}[class*="st-key-card-"]{padding:16px 14px 12px}}
"""

ICON_TOOL = ('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
             'stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0'
             'l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>')
ICON_CAL = ('<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/>'
            '<line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>')


def esc(v):
    return html_lib.escape(str(v if v is not None else ""), quote=True)


def render_html(markup, target=None):
    (target or st).markdown("".join(l.strip() for l in markup.splitlines()), unsafe_allow_html=True)


def chip(text, tone=""):
    return f'<span class="pe-chip {tone}">{esc(text)}</span>'


def section_header(num, title, subtitle=""):
    render_html(f'<div class="pe-section"><div class="badge">{num}</div><div><div class="t">{esc(title)}</div>'
                + (f'<div class="s">{esc(subtitle)}</div>' if subtitle else "") + "</div></div>")


FULL = {"width": "stretch"}

st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)

# =====================================================================
#  SIGNATURE PAD (custom component – works with finger or mouse)
# =====================================================================
SIG_JS = r"""
export default function(component) {
  const { parentElement, data, setStateValue } = component;
  let root = parentElement.querySelector('.sig');
  if (root) { root._set = setStateValue; return; }
  root = document.createElement('div');
  root.className = 'sig';
  root._set = setStateValue;
  root.innerHTML = '<canvas></canvas><div class="bar"><span class="hint">Sign above with a finger or mouse</span><button type="button">Clear</button></div>';
  parentElement.appendChild(root);
  const canvas = root.querySelector('canvas');
  const ctx = canvas.getContext('2d');
  const H = 170;
  let drawing = false, last = null, inked = false;
  const dpr = () => window.devicePixelRatio || 1;
  function paintBg() { ctx.save(); ctx.setTransform(1,0,0,1,0,0); ctx.fillStyle = '#ffffff'; ctx.fillRect(0,0,canvas.width,canvas.height); ctx.restore(); }
  function pen() { ctx.lineWidth = 2.4; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#0A0E1A'; ctx.fillStyle = '#0A0E1A'; }
  function drawUrl(url) {
    if (!url) return;
    const img = new Image();
    img.onload = () => { ctx.drawImage(img, 0, 0, canvas.width / dpr(), canvas.height / dpr()); inked = true; root.classList.add('inked'); };
    img.src = url;
  }
  function size() {
    const w = Math.max(220, Math.floor(root.clientWidth || 300));
    if (canvas._w === w) return;
    const snap = inked ? exportPng() : ((data && data.initial) || '');
    canvas._w = w;
    const r = dpr();
    canvas.width = w * r; canvas.height = H * r;
    canvas.style.width = w + 'px'; canvas.style.height = H + 'px';
    ctx.setTransform(r,0,0,r,0,0); paintBg(); pen(); drawUrl(snap);
  }
  function exportPng() {
    const out = document.createElement('canvas');
    const ow = 600, oh = Math.round(600 * H / (canvas.width / dpr()));
    out.width = ow; out.height = oh;
    const o = out.getContext('2d');
    o.fillStyle = '#fff'; o.fillRect(0,0,ow,oh); o.drawImage(canvas, 0, 0, ow, oh);
    return out.toDataURL('image/png');
  }
  function pos(e) { const b = canvas.getBoundingClientRect(); return { x: e.clientX - b.left, y: e.clientY - b.top }; }
  canvas.addEventListener('pointerdown', (e) => {
    drawing = true; canvas.setPointerCapture(e.pointerId); last = pos(e);
    pen(); ctx.beginPath(); ctx.arc(last.x, last.y, 1.1, 0, Math.PI * 2); ctx.fill(); e.preventDefault();
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!drawing) return;
    const p = pos(e); pen(); ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke(); last = p; inked = true;
  });
  const finish = () => { if (!drawing) return; drawing = false; inked = true; root.classList.add('inked'); root._set('png', exportPng()); };
  canvas.addEventListener('pointerup', finish);
  canvas.addEventListener('pointercancel', finish);
  root.querySelector('button').addEventListener('click', () => { paintBg(); inked = false; root.classList.remove('inked'); root._set('png', ''); });
  if (window.ResizeObserver) { new ResizeObserver(size).observe(root); }
  size();
}
"""
SIG_CSS = """
.sig{width:100%}
.sig canvas{display:block;border-radius:12px;border:1.5px dashed #5E6A82;background:#fff;touch-action:none;cursor:crosshair}
.sig.inked canvas{border-style:solid;border-color:#7C83FF}
.sig .bar{display:flex;justify-content:space-between;align-items:center;margin-top:6px;font:12px Inter,system-ui,sans-serif;color:#8C98B0}
.sig button{background:#161F33;color:#E7EAF3;border:1px solid rgba(148,163,184,.26);border-radius:8px;padding:7px 16px;font:600 13px Inter,system-ui,sans-serif;cursor:pointer}
"""
_SIG = st.components.v2.component("sy_signature_pad", js=SIG_JS, css=SIG_CSS)


# =====================================================================
#  SESSION STATE + FORM HELPERS
# =====================================================================
ss = st.session_state
ss.setdefault("screen", "jobs")
ss.setdefault("F", {})
ss.setdefault("job", None)
ss.setdefault("nonce", uuid.uuid4().hex[:6])
ss.setdefault("result", None)
F = ss.F


def wkey(n):
    return f"w_{ss.nonce}_{n}"


def txt(n, label, placeholder="", **kw):
    F[n] = st.text_input(label, value=F.get(n) or "", key=wkey(n), placeholder=placeholder, **kw)
    return F[n]


def area(n, label, placeholder="", height=110):
    F[n] = st.text_area(label, value=F.get(n) or "", key=wkey(n), placeholder=placeholder, height=height)
    return F[n]


def num(n, label):
    cur = F.get(n)
    F[n] = st.number_input(label, min_value=0, step=1, value=int(cur) if cur not in (None, "") else 0, key=wkey(n))
    return F[n]


def pick(n, label, options, allow_new=True, placeholder="Choose or type…"):
    opts = list(options)
    cur = F.get(n)
    if cur and cur not in opts:
        opts.append(cur)
    F[n] = st.selectbox(label, opts, index=opts.index(cur) if cur in opts else None, key=wkey(n),
                        placeholder=placeholder, accept_new_options=allow_new)
    return F[n]


def multi(n, label, options):
    cur = [x for x in (F.get(n) or []) if x in options]
    F[n] = st.pills(label, options, selection_mode="multi", default=cur, key=wkey(n)) or []
    return F[n]


def multi_select(n, label, options):
    opts = list(options) + [x for x in (F.get(n) or []) if x not in options]
    F[n] = st.multiselect(label, opts, default=F.get(n) or [], key=wkey(n),
                          placeholder="Choose or type…", accept_new_options=True)
    return F[n]


def one(n, label, options):
    cur = F.get(n) if F.get(n) in options else None
    F[n] = st.segmented_control(label, options, default=cur, key=wkey(n))
    return F[n]


def tm(n, label):
    cur = F.get(n)
    val = datetime.strptime(cur, "%H:%M").time() if cur else None
    v = st.time_input(label, value=val, key=wkey(n), step=300)
    F[n] = v.strftime("%H:%M") if v else ""
    return F[n]


def chk(n, label, default=False, **kw):
    F[n] = st.checkbox(label, value=F.get(n, default), key=wkey(n), **kw)
    return F[n]


def _package_chosen():
    """Package picked in the dropdown."""
    apply_package(ss.F, ss.get(wkey("package")) or "")


def apply_package(F, pkg):
    """Fill in broadband type, provider and expected speeds from a package name (e.g. 'FTTP 80/20')."""
    F["package"] = pkg
    up = pkg.upper()
    if up.startswith("FTTP"):
        F["bb_type"] = "Full Fibre (FTTP)"
    elif up.startswith("SOGEA"):
        F["bb_type"] = "SoGEA"
    elif up.startswith("FTTC"):
        F["bb_type"] = "Fibre to cabinet (FTTC)"
    elif "LEASED" in up or "ETHERNET" in up:
        F["bb_type"] = "Ethernet leased line"
    m = re.search(r"(\d+)\s*/\s*(\d+)", pkg)
    if m:
        F["exp_down"], F["exp_up"] = int(m.group(1)), int(m.group(2))
    if pkg in BB_PACKAGES and not F.get("provider"):
        F["provider"] = "SY"
    for k in ("bb_type", "exp_down", "exp_up", "provider"):
        ss.pop(wkey(k), None)  # redraw those boxes with the new values


# ---------------------------------------------------------------- quote JSON import
_PC_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
_QUOTE_IGNORE = ("q_bank_name", "q_acc_holder", "q_acc_no", "q_sort_code")  # never read bank details


def _qty(v):
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _make_of(models):
    """Grandstream / Yealink / Mix / Other from the handset names sold."""
    makes = {("Grandstream" if "grandstream" in m.lower() else "Yealink" if "yealink" in m.lower() else "Other")
             for m in models}
    if not makes:
        return ""
    return makes.pop() if len(makes) == 1 else "Mix"


def read_quote(raw: bytes):
    """Quotation-tool JSON -> (fields for the job sheet, kit summary text, company name)."""
    snap = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(snap, dict) or not any(k.startswith("q_") for k in snap):
        raise ValueError("That doesn't look like a quote saved from the quotation tool.")
    snap = {k: v for k, v in snap.items() if k not in _QUOTE_IGNORE}
    g = lambda k: str(snap.get(k) or "").strip()

    def picked(prefix):
        return {k[len(prefix):]: _qty(v) for k, v in snap.items() if k.startswith(prefix) and _qty(v) > 0}

    desk, cord, heads, other = picked("desk_"), picked("cord_"), picked("hs_"), picked("oth_")
    routers, switches = picked("rt_qty_"), picked("sw_qty_")
    soft = _qty(snap.get("standalone_softphones_key"))
    cord_handsets = {n: q for n, q in cord.items() if not re.search(r"base|repeater", n, re.I)}
    n_handsets = sum(desk.values()) + sum(cord_handsets.values())

    sw = []
    if snap.get("q_sw_studio"):
        sw.append("SY Comms Studio")
    if _qty(snap.get("q_sw_ctd")):
        sw.append(f"Click to Dial ×{_qty(snap.get('q_sw_ctd'))}")
    if _qty(snap.get("q_sw_wb")):
        sw.append(f"Call Recording Dashboard ×{_qty(snap.get('q_sw_wb'))}")
    if soft:
        sw.append(f"Softphone / mobile app users ×{soft}")

    addr_raw = g("q_address")
    pc = _PC_RE.search(addr_raw)
    postcode = f"{pc.group(1)} {pc.group(2)}".upper() if pc else ""
    lines = [l.strip(" ,") for l in addr_raw.replace("\r", "").split("\n") if l.strip(" ,")]
    if postcode:
        lines = [_PC_RE.sub("", l).strip(" ,") for l in lines]
    address = ", ".join(l for l in lines if l)

    contact = g("q_contact").split(" - ")[0].strip()
    bb_prov = g("q_bb_provider")
    has_bb = bool(bb_prov) and not bb_prov.lower().startswith("none")
    has_phones = bool(n_handsets or soft)

    f = {
        "customer_name": g("q_comp_name"), "contact_name": contact, "cust_sign_name": contact,
        "contact_phone": g("q_phone"), "contact_email": g("q_dir_email") or g("q_bill_email"),
        "address": address, "postcode": postcode, "visit_type": "New installation",
        "covered": [c for c, on in (("Phone system", has_phones), ("Broadband & network", has_bb)) if on],
    }
    if has_phones:
        f.update({
            "platform": QUOTE_PLATFORM,
            "handset_models": _make_of(list(desk) + list(cord)),
            "handsets_required": n_handsets,
            "handset_types": [t for t, on in (("Desk phone", bool(desk)), ("DECT", bool(cord_handsets)),
                                               ("Softphone", bool(soft))) if on],
            "users_number": n_handsets + soft,
            "software_included": "Yes" if sw else "No",
        })
        if sw:
            f["software_name"] = ", ".join(sw)
            f["licence_count"] = soft + _qty(snap.get("q_sw_ctd")) + (1 if snap.get("q_sw_studio") else 0)
        if any(k.startswith("cs_") and v is True for k, v in snap.items()):
            f["features"] = ["Call Scope"]
    if has_bb:
        f["provider"] = bb_prov
        f["_package"] = g("q_bb_package")
        if routers:
            f["router"] = next(iter(routers))
        elif snap.get("router_mode", "Auto-select") == "Auto-select":
            f["router"] = "Draytek Vigor 2927 (FTTP/SoGEA)"

    def kit(label, d):
        return f"{label}: " + ", ".join(f"{q} × {n}" for n, q in d.items()) if d else ""
    summary = [x for x in (
        kit("Desk phones", desk), kit("DECT / cordless", cord),
        f"Softphone / mobile app users: {soft}" if soft else "",
        kit("Headsets", heads), kit("Other hardware", other),
        kit("Router", routers) or (f"Router: {f['router']} (auto-selected)" if f.get("router") else ""),
        kit("Switches", switches),
        f"Broadband: {bb_prov} – {g('q_bb_package')}" if has_bb else "",
        f"Software: {', '.join(sw)}" if sw else "",
        f"Install: {g('q_install_type')}" + (f", {g('q_install_days')} day(s)" if g("q_install_days") else "") if g("q_install_type") else "",
        f"Sites: {g('q_num_sites')}" if _qty(snap.get("q_num_sites")) > 1 else "",
        f"Sold by: {g('q_rep_name')}" if g("q_rep_name") else "",
    ) if x]
    return f, "\n".join(summary), g("q_comp_name")


def apply_quote(fields, summary, overwrite_customer):
    """Put quote details onto the open sheet. Customer boxes are only filled when empty
    (Zoho stays in charge of those) unless this is a brand-new sheet started from the quote."""
    F = ss.F  # the sheet that's open now (may have just been created)
    cust_keys = {"customer_name", "contact_name", "cust_sign_name", "contact_phone", "contact_email", "address", "postcode"}
    for k, v in fields.items():
        if k.startswith("_") or v in (None, "", []):
            continue
        if k in cust_keys and not overwrite_customer and (F.get(k) or "").strip():
            continue
        if k == "covered":
            v = list(dict.fromkeys((F.get("covered") or []) + v))
        F[k] = v
        ss.pop(wkey(k), None)
    if fields.get("_package"):
        apply_package(F, fields["_package"])
        ss.pop(wkey("package"), None)
    if summary:
        ss.job["notes"] = ((ss.job.get("notes") or "").strip() + "\n\nFrom the quote:\n" + summary).strip()
    F["_quote_loaded"] = True
    ss.nonce = uuid.uuid4().hex[:6]  # redraw every box from the updated sheet


def set_now(n):
    F[n] = datetime.now(UK).strftime("%H:%M")
    ss.pop(wkey(n), None)


def signature(n, label):
    render_html(f'<div class="pe-siglabel">{esc(label)}</div>')
    res = _SIG(key=wkey(f"sig_{n}"), data={"initial": F.get(n) or ""}, on_png_change=lambda: None)
    v = res.get("png") if res is not None else None
    if v is not None:
        F[n] = v


# =====================================================================
#  STORAGE (drafts + finished sheets) — best-effort, on the app server
# =====================================================================
def _safe(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(s))[:80]


def draft_path(job):
    return DRAFTS / f"{_safe(job['id'])}.json"


def save_draft():
    job = ss.job
    if not job:
        return
    blob = json.dumps({"F": F, "job": job}, default=str)
    h = hashlib.md5(blob.encode()).hexdigest()
    if ss.get("_draft_hash") != h:
        try:
            draft_path(job).write_text(blob)
            ss["_draft_hash"] = h
        except OSError:
            pass


def load_draft(job):
    p = draft_path(job)
    if p.exists():
        try:
            return json.loads(p.read_text()).get("F")
        except Exception:
            return None
    return None


def history(engineer_name, days=7):
    out = []
    cutoff = date.today() - timedelta(days=days)
    for p in sorted(HISTORY.glob("*.json"), reverse=True):
        try:
            rec = json.loads(p.read_text())
        except Exception:
            continue
        if rec.get("engineer") == engineer_name and rec.get("visit_date", "") >= cutoff.isoformat():
            out.append(rec)
    return out


def done_job_ids():
    ids = set()
    for p in HISTORY.glob("*.json"):
        try:
            ids.add(json.loads(p.read_text()).get("job_id"))
        except Exception:
            pass
    return ids


# =====================================================================
#  LOGIN — one shared app password, then the engineer gives their name.
#  Fails closed: if [app] password isn't set in Secrets, nobody gets in.
# =====================================================================
def login_screen():
    render_html(f'<div class="pe-login-head"><div class="pe-logo">{ICON_TOOL}</div>'
                f'<div class="t">{esc(BRAND["name"])} Job Sheets</div>'
                '<div class="s">Engineer site visits — fill in, sign off, sync to Zoho.</div></div>')
    app_pw = secret("app", "password")
    with st.container(key="card-login"):
        if not app_pw:
            st.error("The app password isn't set yet. Add password under [app] in the app's Secrets.")
            st.stop()
        names = engineer_names()
        with st.form("login"):
            if names:
                who = st.selectbox("Your name", names, index=None, accept_new_options=True,
                                   placeholder="Pick or type your name")
            else:
                who = st.text_input("Your name", placeholder="e.g. Dave Jones")
            pw = st.text_input("App password", type="password")
            ok = st.form_submit_button("Log in", **FULL)
        if ok:
            who = (who or "").strip()
            if not hmac.compare_digest(str(app_pw), pw.strip()):
                st.error("That password isn't right.")
            elif len(who) < 2:
                st.error("Please enter your name — it goes on the job sheet.")
            else:
                ss.engineer = {"name": who.title() if who.islower() else who, "email": ""}
                st.rerun()
    zoho_setup_gate()
    st.stop()


if "engineer" not in ss:
    login_screen()

ENG = ss.engineer


# =====================================================================
#  JOB LIST
# =====================================================================
@st.cache_data(ttl=120, show_spinner=False)
def fetch_jobs(day_iso: str, _client, sources: str, conn: str = ""):
    jobs = _client.list_jobs(date.fromisoformat(day_iso))
    return jobs, list(_client.last_warnings)


def job_record(job):
    """(module, id) of the Zoho record the job sheet is saved to — an Account, or a Lead."""
    job = job or {}
    rid = job.get("record_id") or job.get("account_id")
    return (job.get("record_module") or "Accounts"), rid


def is_linked(job):
    return bool(job_record(job)[1]) and (job or {}).get("module") != "Demo" and not DEMO


def is_mine(job):
    e = job.get("engineer") or {}
    my_email = (ENG.get("email") or "").lower()
    if my_email and e.get("email") and e["email"] == my_email:
        return True
    if ENG.get("zoho_user_id") and e.get("id") == str(ENG["zoho_user_id"]):
        return True
    return bool(e.get("name")) and e["name"].strip().lower() == ENG["name"].strip().lower()


def fmt_time(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(UK).strftime("%H:%M")
    except ValueError:
        return ""


def fmt_day(iso):
    try:
        d = datetime.fromisoformat(iso).astimezone(UK).date()
    except (TypeError, ValueError):
        return "No date"
    return "Today" if d == date.today() else ("Tomorrow" if d == date.today() + timedelta(days=1) else d.strftime("%A %-d %B"))


def guess_visit(job):
    t = " ".join(str(job.get(k) or "") for k in ("title", "service", "notes")).lower()
    if any(w in t for w in ("fault", "repair", "not working", "no dial", "down", "issue")):
        return "Fault / repair"
    if any(w in t for w in ("survey",)):
        return "Site survey"
    if any(w in t for w in ("health", "maint", "check")):
        return "Maintenance / health check"
    if any(w in t for w in ("move", "additional", "add ", "extra")):
        return "Additional equipment / moves & changes"
    if any(w in t for w in ("collect", "removal", "cease")):
        return "Collection / removal of equipment"
    return "New installation"


def guess_cover(job, vt):
    if vt == "New installation":
        return list(COVER_OPTIONS)
    t = " ".join(str(job.get(k) or "") for k in ("title", "service", "notes")).lower()
    out = []
    if any(w in t for w in ("phone", "call", "handset", "inbound", "outbound", "dial", "voicemail", "extension")):
        out.append("Phone system")
    if any(w in t for w in ("broadband", "wifi", "wi-fi", "internet", "router", "fibre", "network")):
        out.append("Broadband & network")
    return out


def new_ref():
    return f"JS-{datetime.now(UK):%y%m%d}-{uuid.uuid4().hex[:4].upper()}"


def open_job(job):
    if job.get("module") not in ("Demo", "Manual") and not job.get("hydrated"):
        try:
            with st.spinner("Pulling customer details from Zoho…"):
                job = zoho_client().hydrate(job)
        except ZohoError as e:
            ss["flash"] = ("warn", f"Couldn't pull everything from Zoho ({e}). Fill in any gaps by hand.")
    vt = guess_visit(job)
    start = job.get("start")
    vdate = datetime.fromisoformat(start).astimezone(UK).date() if start else date.today()
    fresh = {
        "customer_name": job.get("account_name") or job.get("for_name") or "",
        "account_number": job.get("account_number", ""),
        "job_no": new_ref(),
        "address": job.get("address") or job.get("appt_address") or "",
        "postcode": job.get("postcode", ""),
        "contact_name": job.get("contact_name", ""),
        "contact_phone": job.get("contact_phone", ""),
        "contact_email": job.get("contact_email", ""),
        "visit_date": vdate.isoformat(),
        "engineer": ENG["name"],
        "visit_type": vt,
        "covered": guess_cover(job, vt),
        "fault_reported": job.get("notes", "") if vt in FAULT_TYPES else "",
        "cust_sign_name": job.get("contact_name", ""),
        "email_customer": True, "email_office": True, "save_zoho": True, "mark_complete": True,
    }
    draft = load_draft(job)
    if draft:
        fresh.update(draft)
        ss["flash"] = ("info", "Picked up where you left off — your unfinished sheet was restored.")
    ss.F = fresh
    ss.job = job
    ss.nonce = uuid.uuid4().hex[:6]
    ss.photos = []
    ss.pop("_draft_hash", None)
    ss.pop("preview_pdf", None)
    ss.screen = "sheet"


def jobs_screen():
    client = None if DEMO else zoho_client()
    deals_mode = bool(client and client.is_deals)
    c1, c2, c3 = st.columns([1.3, 1, 0.8], vertical_alignment="bottom")
    with c1:
        day = st.date_input("Diary date", value=ss.get("day", date.today()), format="DD/MM/YYYY", key="day_pick")
        ss.day = day
    with c2:
        view = st.segmented_control("Show", ["Day", "Next 7 days"], default=ss.get("view", "Next 7 days"),
                                    key="view_t", required=True)
        ss.view = view
    with c3:
        if st.button("Refresh", icon=":material/refresh:", **FULL):
            fetch_jobs.clear()
            if client:
                client._deals_cache = (0.0, None)
                client._users_until = 0.0
            st.rerun()
    mine = False

    days = [day] if view == "Day" else [day + timedelta(days=i) for i in range(7)]
    jobs, err, warns = [], None, set()
    for d in days:
        if DEMO:
            jobs += demo_jobs(d, ENG["name"])
            continue
        try:
            got, w = fetch_jobs(d.isoformat(), client, ",".join(client.sources), str(id(client)))
            jobs += got
            warns.update(w)
        except ZohoError as e:
            err = str(e)
            break
    if err:
        st.error(f"Couldn't load the diary from Zoho. {err}")
    for w in sorted(warns):
        src = w.split(":", 1)[0]
        if "oauth scope" in w.lower():
            st.warning(f"{src} aren't showing yet — the Zoho connection doesn't have permission to read them. "
                       "Office: log out, open 'Office: Zoho connection' on the login screen and make a new token.")
        else:
            st.warning(f"Part of the diary couldn't be loaded — {w}")

    finished = done_job_ids()

    users = client.users() if client else {}

    def people_of(j):
        names = list(j.get("people") or [])
        eng = j.get("engineer") or {}
        if eng.get("name"):
            names.append(eng["name"])
        for uid in list(j.get("people_ids") or []) + ([str(eng["id"])] if eng.get("id") else []):
            nm = users.get(str(uid), {}).get("name")  # names filled in even if they were missing when the diary loaded
            if nm:
                names.append(nm)
        return [n for n in dict.fromkeys(names) if n]

    def same_person(a, b):
        """'Sam' matches 'Sam Myatt'; 'sam myatt' matches 'Sam Myatt'."""
        ta, tb = a.lower().split(), b.lower().split()
        return bool(ta) and all(t in tb for t in ta)

    everyone = "Everyone"
    staff = {u.get("name") for u in users.values() if u.get("name")} | set(secret("app", "engineer_names", []) or [])
    hosts = sorted({n for j in jobs for n in people_of(j)} | staff, key=str.lower)  # everyone, every day
    me = next((h for h in hosts if same_person(ENG["name"], h)), None)
    if ss.get("host_f_owner") != ENG["name"]:  # new login: always start on the engineer's own bookings
        ss.host_f = me or ENG["name"]
        ss.host_f_owner = ENG["name"]
        ss.pop("host_f_w", None)
    elif ss.host_f == ENG["name"] and me and me != ENG["name"]:
        ss.host_f = me  # their Zoho name has turned up — use the exact spelling
        ss.pop("host_f_w", None)

    with st.container(key="card-diary"):
        if view == "Day":
            head = "Today" if day == date.today() else day.strftime("%A %-d %B")
        else:
            head = f"{day.strftime('%-d %b')} – {days[-1].strftime('%-d %b')}"
        f1, f2 = st.columns([1, 1.2])
        with f1:
            opts = [everyone] + hosts
            if ss.host_f not in opts:
                opts.append(ss.host_f)
            ss.host_f = st.selectbox("Host / attendee", opts, index=opts.index(ss.host_f), key="host_f_w",
                                     help="Shows bookings where this person is the host (organiser) "
                                          "or has been added as a participant.")
        with f2:
            q = st.text_input("Search", key="diary_q", placeholder="Customer or title…")

        shown = jobs
        if ss.host_f != everyone:
            shown = [j for j in shown if any(same_person(ss.host_f, n) for n in people_of(j))]
        if q and q.strip():
            words = q.lower().split()
            shown = [j for j in shown if all(w in " ".join(str(j.get(k) or "") for k in
                                                       ("title", "for_name", "account_name", "consultant")).lower()
                                             for w in words)]
        sub = (f"{len(shown)} of {len(jobs)} bookings" if len(shown) != len(jobs)
               else f"{len(jobs)} booking{'s' if len(jobs) != 1 else ''}")
        if ss.host_f != everyone:
            sub += f" · {ss.host_f}"
        section_header("01", f"{head} — diary", sub)
        if not shown:
            when = "this day" if view == "Day" else "the next 7 days"
            title = (f"No bookings for {ss.host_f} {when}" if ss.host_f != everyone and not (q and q.strip())
                     else "No bookings match")
            tips = ""
            if view == "Day":
                tips += '<li>Try "Next 7 days", or another date</li>'
            if ss.host_f != everyone or (q and q.strip()):
                tips += '<li>Pick "Everyone" (or another name) above, or clear the search</li>'
            tips += '<li>Not booked in Zoho? Use "Job not in the diary" below</li>'
            render_html(f'<div class="pe-empty"><div class="ic">{ICON_CAL}</div>'
                        f'<div class="t">{esc(title)}</div><ol>' + tips + '</ol></div>')
            if view == "Day" and ss.host_f != everyone:
                if st.button(f"Show {'my' if ss.host_f == me or same_person(ENG['name'], ss.host_f) else ss.host_f + chr(39) + 's'} next 7 days",
                             type="primary", icon=":material/date_range:", key="show_week", **FULL):
                    ss.view = "Next 7 days"
                    ss.pop("view_t", None)
                    st.rerun()
        if client and getattr(client, "users_error", "") and not users:
            st.caption(":orange[Couldn't load staff names from Zoho just now, so hosts may be missing — "
                       "tap Refresh in a moment.]")

    last_day = None
    for i, job in enumerate(shown):
        jd = fmt_day(job.get("start"))
        if view != "Day" and jd != last_day:
            render_html(f'<div class="pe-dayhead">{esc(jd)}</div>')
            last_day = jd
        done = job["id"] in finished
        with st.container(key=f"card-job-{'done-' if done else ''}{i}"):
            if job.get("start"):
                t = fmt_time(job["start"]) + (f"–{fmt_time(job['end'])}" if job.get("end") else "")
            else:
                t = "No time set"
            chips = chip(job.get("status") or "Booked", "accent")
            if done:
                chips = chip("Job sheet sent", "good")
            if job.get("module") == "Events":
                chips = chip("Meeting", "accent") if not done else chips
            elif job.get("module") == "Deals" and not done:
                chips = chip(job.get("status") or "Deal", "accent")
            who = job.get("consultant") or ((job.get("engineer") or {}).get("name") if not mine else "")
            if who:
                chips += chip((f"{job.get('booked_label', 'Consultant')}: " if job.get("consultant") else "") + who)
            title = job["title"]
            cust = job.get("for_name") or job.get("account_name") or ""
            render_html(f'<div class="job-time">{esc(t)}</div><div class="job-title">{esc(title)}</div>'
                        + (f'<div class="job-cust">{esc(cust)}</div>' if cust and cust != title else '<div style="height:8px"></div>')
                        + chips)
            if st.button("Open job sheet" if not done else "Open again", key=f"open_{job['id']}",
                         type="primary" if not done else "secondary", icon=":material/assignment:", **FULL):
                open_job(job)
                st.rerun()

    with st.expander("Job not in the diary?", icon=":material/search:"):
        if not DEMO:
            term = st.text_input("Search Zoho customers and leads", placeholder="Company name…", key="acc_search")
            if term and len(term.strip()) >= 2:
                try:
                    found = zoho_client().search_accounts(term)
                except ZohoError as e:
                    found = []
                    st.error(str(e))
                if not found:
                    st.caption("Nothing found — try one distinctive word, e.g. just \"Lettings\".")
                for a in found:
                    label = (f"{a['kind']}: " if a.get("kind") else "") + a["name"] + (f"  ·  {a['number']}" if a["number"] else "") + (f"  ·  {a['postcode']}" if a["postcode"] else "")
                    if st.button(label, key=f"acc_{a['id']}", **FULL):
                        try:
                            details = (zoho_client().lead_details(a["id"]) if a.get("module") == "Leads"
                                       else zoho_client().account_details(a["id"]))
                        except ZohoError as e:
                            details = ({"record_module": "Leads", "record_id": a["id"]} if a.get("module") == "Leads"
                                       else {"account_id": a["id"]})
                            details["account_name"] = a["name"]
                            ss["flash"] = ("warn", str(e))
                        open_job({"id": f"acc-{a['id']}-{date.today().isoformat()}", "module": "Manual",
                                  "title": "Unscheduled visit", "start": None, "end": None, "status": "",
                                  "hydrated": True, **details})
                        st.rerun()
        else:
            st.caption("Demo mode — Zoho search is switched off.")
        st.markdown("**Start from a quote**")
        qup = st.file_uploader("Quote JSON from the quotation tool", type=["json"], key="quote_start_up")
        if qup is not None and st.button("Create job sheet from this quote", type="primary",
                                         icon=":material/upload_file:", key="quote_start_go", **FULL):
            try:
                fields, summary, company = read_quote(qup.getvalue())
            except (ValueError, UnicodeDecodeError) as e:
                st.error(f"Couldn't read that file: {e}")
            else:
                job = {"id": f"quote-{uuid.uuid4().hex[:8]}", "module": "Manual", "title": f"Installation – {company}",
                       "start": None, "end": None, "status": "", "hydrated": True, "account_name": company}
                if not DEMO and zoho_client() and company:
                    try:  # link to the Zoho customer automatically when the name matches exactly
                        hit = [a for a in zoho_client().search_accounts(company)
                               if a["name"].strip().lower() == company.strip().lower()]
                        if len(hit) >= 1:
                            a = hit[0]
                            job.update(zoho_client().lead_details(a["id"]) if a["module"] == "Leads"
                                       else zoho_client().account_details(a["id"]))
                    except ZohoError:
                        pass
                open_job(job)
                apply_quote(fields, summary, overwrite_customer=not job.get("record_id"))
                st.rerun()
        st.markdown("**Or**")
        if st.button("Start a blank job sheet", icon=":material/note_add:", **FULL):
            open_job({"id": f"blank-{uuid.uuid4().hex[:8]}", "module": "Manual", "title": "Blank job sheet",
                      "start": None, "end": None, "status": "", "hydrated": True})
            st.rerun()

    recent = history(ENG["name"])
    if recent:
        with st.expander(f"My recent job sheets ({len(recent)})", icon=":material/history:"):
            for r in recent[:15]:
                pdf = HISTORY / f"{_safe(r['ref'])}-customer.pdf"
                c1, c2 = st.columns([3, 1], vertical_alignment="center")
                c1.markdown(f"**{esc(r['customer'])}**  \n{esc(r['visit_type'])} · {esc(r['visit_date'])} · `{esc(r['ref'])}`")
                if pdf.exists():
                    c2.download_button("PDF", data=pdf.read_bytes(), file_name=pdf.name, mime="application/pdf",
                                       key=f"dl_{r['ref']}", **FULL)


# =====================================================================
#  JOB SHEET
# =====================================================================
def prepare_photos():
    out = []
    for i, f in enumerate(ss.get("photos_upload") or []):
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(f.getvalue()))).convert("RGB")
            im.thumbnail((1600, 1600))
            b = io.BytesIO()
            im.save(b, "JPEG", quality=82)
            out.append((f"Photo {i + 1} – {f.name}", b.getvalue()))
        except Exception:
            continue
    return out


def problems():
    p = []
    if not (F.get("customer_name") or "").strip():
        p.append("Customer / business name")
    if not F.get("visit_type"):
        p.append("Visit type")
    if not (F.get("work_done") or "").strip():
        p.append("Work carried out / notes")
    if F.get("show_fault") and not F.get("fault_outcome"):
        p.append("Fault outcome")
    if not F.get("cust_unavailable"):
        if not (F.get("cust_sign_name") or "").strip():
            p.append("Customer name (sign-off)")
        if not F.get("sig_customer"):
            p.append("Customer signature")
    if not F.get("sig_engineer"):
        p.append("Engineer signature")
    return p


def sheet_screen():
    job = ss.job
    linked = is_linked(job)
    top1, top2 = st.columns([1, 2.4], vertical_alignment="center")
    with top1:
        if st.button("My jobs", icon=":material/arrow_back:", **FULL):
            save_draft()
            ss.screen = "jobs"
            st.rerun()
    with top2:
        chips = ""
        if job.get("start"):
            chips += chip(f"{fmt_time(job['start'])}–{fmt_time(job.get('end'))}", "accent")
        if F.get("account_number"):
            chips += chip(f"Acc {F['account_number']}")
        chips += chip("Linked to Zoho", "good") if linked else chip("Not linked to a Zoho account" if not DEMO else "Demo job", "warn")
        render_html(f"<div style='padding-top:6px'>{chips}</div>")

    if job.get("notes"):
        render_html(f'<div class="pe-note"><b>Office notes for this job</b>{esc(job["notes"]).replace(chr(10), "<br>")}</div>')

    if not linked and not DEMO and zoho_client():
        with st.expander("Link this job to a Zoho account", icon=":material/link:", expanded=True):
            st.caption("This booking isn't linked to a customer in Zoho. Find the customer (or lead) so the job "
                       "sheet, notes and emails are saved against it.")
            if job.get("link_info"):
                st.caption(f"What Zoho has on this booking: {job['link_info']}")
            guess = re.split(r"\s[–—-]\s|:", job.get("title") or "")[-1].strip()
            default_term = F.get("customer_name") or job.get("for_name") or guess
            term = st.text_input("Search Zoho customers and leads", value=default_term, key=wkey("link_search"),
                                 placeholder="Company name…")
            if term and len(term.strip()) >= 2:
                try:
                    found = zoho_client().search_accounts(term)
                except ZohoError as e:
                    found = []
                    st.error(str(e))
                if not found:
                    st.caption("Nothing found — try one distinctive word, e.g. just \"Lettings\".")
                for a in found[:10]:
                    label = (f"{a['kind']}: " if a.get("kind") else "") + a["name"] + (f"  ·  {a['number']}" if a["number"] else "") + (f"  ·  {a['postcode']}" if a["postcode"] else "")
                    if st.button(label, key=wkey(f"link_{a['id']}"), icon=":material/add_link:", **FULL):
                        link_account(a["id"], a.get("module", "Accounts"))
                        st.rerun()

    if not F.get("_quote_loaded"):
        with st.expander("Load details from the quote (JSON)", icon=":material/upload_file:"):
            st.caption("Upload the quote saved from the quotation tool — it fills in the handsets, software, "
                       "broadband package and router that were sold. Bank details in the file are ignored.")
            up = st.file_uploader("Quote JSON", type=["json"], key=wkey("quote_up"), label_visibility="collapsed")
            if up is not None:
                try:
                    fields, summary, _co = read_quote(up.getvalue())
                    apply_quote(fields, summary, overwrite_customer=False)
                    ss["flash"] = ("info", "Quote loaded — kit, software and broadband filled in. Check them on site.")
                    st.rerun()
                except (ValueError, UnicodeDecodeError) as e:
                    st.error(f"Couldn't read that file: {e}")

    # ---------------- 01 customer
    with st.container(key="card-s1"):
        section_header("01", "Customer & visit details", "Pulled from Zoho — correct anything that's changed")
        c1, c2 = st.columns([2, 1])
        with c1:
            txt("customer_name", "Customer / business name")
        with c2:
            txt("job_no", "Job / order no.")
        c1, c2 = st.columns([2, 1])
        with c1:
            txt("address", "Installation address")
        with c2:
            txt("postcode", "Postcode")
        c1, c2 = st.columns(2)
        with c1:
            txt("contact_name", "Site contact")
        with c2:
            txt("contact_phone", "Contact number")
        c1, c2 = st.columns(2)
        with c1:
            txt("contact_email", "Email address", placeholder="Customer copy is sent here")
        with c2:
            vd = st.date_input("Visit date", value=date.fromisoformat(F["visit_date"]), format="DD/MM/YYYY",
                               key=wkey("visit_date"))
            F["visit_date"] = vd.isoformat()
        c1, c2 = st.columns(2)
        with c1:
            tm("arrival", "Arrival time")
        with c2:
            tm("finish", "Finish time")
        with st.container(horizontal=True, gap="small"):
            st.button("Arrived now", key="arr_now", on_click=set_now, args=("arrival",),
                      icon=":material/login:", width="stretch")
            st.button("Finished now", key="fin_now", on_click=set_now, args=("finish",),
                      icon=":material/logout:", width="stretch")

    # ---------------- 02 work
    with st.container(key="card-s2"):
        section_header("02", "Visit type & work carried out")
        vt = pick("visit_type", "Visit type", VISIT_TYPES, allow_new=False, placeholder="Choose…")
        F["show_fault"] = vt in FAULT_TYPES
        multi("covered", "What did this visit involve?", COVER_OPTIONS)
        if F["show_fault"]:
            area("fault_reported", "Fault reported", placeholder="What the customer / office reported", height=80)
            c1, c2 = st.columns(2)
            with c1:
                pick("fault_category", "Fault category", FAULT_CATEGORIES)
            with c2:
                pick("root_cause", "Cause found", ROOT_CAUSES)
            one("fault_outcome", "Outcome", FAULT_OUTCOMES)
            txt("parts_used", "Parts / equipment used or swapped", placeholder="e.g. replaced T54W handset, PSU")
        area("work_done", "Work carried out / engineer notes",
             placeholder="What you did on site — the office and customer will see this")
        one("billable", "Billing", BILLING)

    covered = F.get("covered") or []
    n = 3
    # ---------------- 03 phones
    if "Phone system" in covered:
        with st.container(key="card-s3"):
            section_header(f"{n:02d}", "Phone system & handsets")
            one("platform", "Phone system / platform", PLATFORMS)
            one("handset_models", "Handset make / model", HANDSET_MAKES)
            if F.get("handset_models") == "Other":
                txt("handset_make_other", "Other make / model", placeholder="e.g. Poly VVX 350")
            c1, c2 = st.columns(2)
            with c1:
                num("handsets_required", "Handsets required")
            with c2:
                num("handsets_installed", "Handsets installed")
            req, inst = F.get("handsets_required") or 0, F.get("handsets_installed") or 0
            if req and inst and inst < req:  # only once an installed figure has been entered
                st.caption(f":orange[{req - inst} fewer handset{'s' if req - inst != 1 else ''} installed than required — "
                           "add the reason under Outstanding items.]")
            multi("handset_types", "Handset type(s)", HANDSET_TYPES)
            txt("handset_other", "Other handset type", placeholder="Optional")
            one("software_included", "Software included?", ["Yes", "No"])
            if F.get("software_included") == "Yes":
                c1, c2 = st.columns([2, 1])
                with c1:
                    pick("software_name", "Software / application", SOFTWARE)
                with c2:
                    num("licence_count", "Licence / seat count")
            c1, c2 = st.columns(2)
            with c1:
                num("users_number", "Number of users")
            with c2:
                num("users_configured", "Users configured")
            txt("ddi_range", "Extensions / DDI range", placeholder="e.g. 201–206 / 01743 123450–59")
            multi("features", "Features configured", FEATURES)
            txt("features_other", "Other features", placeholder="Optional")
            area("serials", "Equipment serials / MAC addresses", placeholder="One per line (optional)", height=80)
        n += 1

    # ---------------- 04 broadband
    if "Broadband & network" in covered:
        with st.container(key="card-s4"):
            section_header(f"{n:02d}", "Broadband & network")
            # Package first — picking one fills in type, provider and expected speeds
            opts = list(BB_PACKAGES)
            cur = F.get("package")
            if cur and cur not in opts:
                opts.append(cur)
            F["package"] = st.selectbox(
                "Broadband package (from the pricebook)", opts, index=opts.index(cur) if cur in opts else None,
                key=wkey("package"), placeholder="Choose the package, or type another",
                accept_new_options=True, on_change=_package_chosen)
            one("bb_type", "Broadband type", BB_TYPES)
            if F.get("bb_type") == "Other":
                txt("bb_type_other", "Other broadband type")
            pick("provider", "Provider", PROVIDERS)
            st.caption("Expected speeds fill in from the package — run a speed test and enter what you measured.")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                num("exp_down", "Expected ↓ Mbps")
            with c2:
                num("exp_up", "Expected ↑ Mbps")
            with c3:
                num("meas_down", "Measured ↓ Mbps")
            with c4:
                num("meas_up", "Measured ↑ Mbps")
            if F.get("exp_down") and F.get("meas_down"):
                pct = round(100 * F["meas_down"] / F["exp_down"])
                tone = "good" if pct >= 80 else "warn"
                up_txt = ""
                if F.get("exp_up") and F.get("meas_up"):
                    up_txt = f" · upload {round(100 * F['meas_up'] / F['exp_up'])}% of expected"
                render_html(chip(f"Download {pct}% of expected{up_txt}", tone))
            c1, c2 = st.columns(2)
            with c1:
                pick("router", "Router make / model", ROUTERS)
            with c2:
                txt("router_serial", "Router serial number")
            txt("wan_details", "WAN / ONT details", placeholder="e.g. ONT serial, PPPoE username, static IP")
            c1, c2 = st.columns(2)
            with c1:
                txt("ssid", "Wi-Fi SSID / network name")
            with c2:
                txt("wifi_pw", "Wi-Fi password", type="password")
            one("wifi_bands", "Wi-Fi band(s)", ["2.4 GHz", "5 GHz", "Both"])
            one("guest_wifi", "Guest Wi-Fi enabled?", ["Yes", "No", "N/A"])
            if F.get("guest_wifi") == "Yes":
                c1, c2 = st.columns(2)
                with c1:
                    txt("guest_ssid", "Guest SSID")
                with c2:
                    txt("guest_pw", "Guest password", type="password")
            multi("network_checks", "Network checks", NETWORK_CHECKS)
            c1, c2 = st.columns(2)
            with c1:
                one("coverage_checked", "Wi-Fi coverage / signal checked", ["Yes", "No"])
            with c2:
                one("pw_handover", "Password handed over securely", ["Yes", "No"])
        n += 1

    # ---------------- testing & sign-off
    with st.container(key="card-s5"):
        section_header(f"{n:02d}", "Testing, handover & sign-off")
        if "Phone system" in covered:
            multi("calls_tested", "Phone calls tested", CALL_TESTS)
        c1, c2 = st.columns(2)
        if "Broadband & network" in covered:
            with c1:
                one("bb_tested", "Broadband / Wi-Fi tested", ["Yes", "No"])
            with c2:
                one("shown_system", "Customer shown how to use system", ["Yes", "No"])
        else:
            with c1:
                one("shown_system", "Customer shown how to use system", ["Yes", "No"])
        one("followup_needed", "Follow-up visit needed?", ["Yes", "No"])
        area("outstanding", "Outstanding items / issues", placeholder="Anything left to do, ordered, or waiting on", height=80)
        st.file_uploader("Site photos (optional)", type=["jpg", "jpeg", "png", "heic", "webp"],
                         accept_multiple_files=True, key="photos_upload",
                         help="On a phone this opens the camera. Photos go on the PDF and onto the Zoho account.")
        st.divider()
        txt("cust_sign_name", "Customer name (printed)")
        chk("cust_unavailable", "Customer not available to sign")
        if F.get("cust_unavailable"):
            pick("unavailable_reason", "Reason", UNAVAILABLE_REASONS)
        else:
            signature("sig_customer", "Customer signature")
        signature("sig_engineer", f"Engineer signature — {ENG['name']}")

    # ---------------- send & sync
    with st.container(key="card-s6"):
        section_header(f"{n + 1:02d}", "Send & sync", "Check what happens when you press the button")
        has_email = bool(re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", (F.get("contact_email") or "").strip()))
        chk("email_customer", f"Email the customer a copy ({F.get('contact_email') or 'no email on file'})",
            disabled=not has_email)
        if "Broadband & network" in covered and (F.get("wifi_pw") or F.get("guest_pw")):
            chk("include_pw_customer", "Include Wi-Fi passwords on the customer's copy",
                help="The office copy and the Zoho copy never show Wi-Fi passwords.")
        chk("email_office", f"Email the office ({BRAND['office_email'] or 'not set up'})",
            disabled=not BRAND["office_email"])
        if F.get("email_customer") or F.get("email_office"):
            senders, sender_err = sender_options()
            if senders:
                emails = [x["email"] for x in senders]
                if F.get("send_from") not in emails:
                    F["send_from"] = default_sender(emails)
                F["send_from"] = st.selectbox(
                    "Send emails from", emails, index=emails.index(F["send_from"]), key=wkey("send_from"),
                    format_func=lambda e: next((f"{x['user_name']} <{e}>" for x in senders
                                                if x["email"] == e and x["user_name"] != e), e))
                if not linked:
                    st.caption(":orange[This job isn't linked to a Zoho account, so Zoho can't send it"
                               + (" — it will go via the backup email instead.]" if secret("smtp") else ".]"))
            elif sender_err:
                st.caption(f":orange[Couldn't load Zoho sending addresses: {sender_err}]")
        chk("save_zoho", "Save PDF + notes to the customer's Zoho account", disabled=not linked)
        appt = job.get("module") not in ("Demo", "Manual") and not DEMO
        if appt and ZohoClient.can_mark_complete_job(job):
            chk("mark_complete", "Mark the Zoho appointment as completed")

        issues = problems()
        if issues:
            render_html("<div style='margin:6px 0 4px;color:var(--muted);font-size:.8rem'>Still needed:</div>"
                        + "".join(chip(i, "warn") for i in issues))
        c1, c2 = st.columns([1, 1.6])
        with c1:
            st.download_button("Preview PDF", data=lambda: build_pdf(_pdf_fields(), BRAND, "customer", prepare_photos()),
                               file_name=f"{F.get('job_no')}-preview.pdf", mime="application/pdf",
                               icon=":material/visibility:", on_click="ignore", **FULL)
        with c2:
            if st.button("Complete & sync job", type="primary", icon=":material/cloud_upload:",
                         disabled=bool(issues), **FULL):
                do_sync()
                st.rerun()

    save_draft()


def link_account(account_id, module="Accounts"):
    """Attach an unlinked job to a Zoho account (or lead) and fill any empty customer boxes from it."""
    try:
        details = (zoho_client().lead_details(account_id) if module == "Leads"
                   else zoho_client().account_details(account_id))
    except ZohoError as e:
        ss["flash"] = ("warn", f"Couldn't load that account: {e}")
        return
    ss.job.update(details)
    fill = {"customer_name": "account_name", "account_number": "account_number", "address": "address",
            "postcode": "postcode", "contact_name": "contact_name", "contact_phone": "contact_phone",
            "contact_email": "contact_email", "cust_sign_name": "contact_name"}
    for fk, dk in fill.items():
        if details.get(dk) and not (F.get(fk) or "").strip():
            F[fk] = details[dk]
            ss.pop(wkey(fk), None)
    if details.get("account_name"):
        F["customer_name"] = details["account_name"]
        ss.pop(wkey("customer_name"), None)
    ss.nonce = uuid.uuid4().hex[:6]  # redraw every box from the updated sheet
    ss["flash"] = ("info", f"Linked to {details.get('account_name', 'the account')} in Zoho.")


def _pdf_fields():
    d = dict(F)
    try:
        d["visit_date_display"] = date.fromisoformat(F["visit_date"]).strftime("%d/%m/%Y")
    except Exception:
        d["visit_date_display"] = F.get("visit_date", "")
    if F.get("cust_unavailable"):
        d["cust_sign_name"] = F.get("cust_sign_name") or "—"
    hm = F.get("handset_models")
    if isinstance(hm, list):  # sheets saved before the make buttons existed
        hm = ", ".join(hm)
    if hm == "Other" and (F.get("handset_make_other") or "").strip():
        hm = f"Other – {F['handset_make_other'].strip()}"
    d["handset_models"] = hm or ""
    return d


# =====================================================================
#  SYNC: Zoho + email + local record
# =====================================================================
def note_text(d):
    lines = [f"Job sheet {d.get('job_no')} — {d.get('visit_type')}",
             f"Engineer: {d.get('engineer')}   Date: {d.get('visit_date_display')}   "
             f"Time on site: {d.get('arrival') or '?'}–{d.get('finish') or '?'}",
             f"Site contact: {d.get('contact_name') or '-'} {d.get('contact_phone') or ''}"]
    if d.get("show_fault"):
        lines += ["", f"Fault reported: {d.get('fault_reported') or '-'}",
                  f"Category: {d.get('fault_category') or '-'}   Cause: {d.get('root_cause') or '-'}",
                  f"Outcome: {d.get('fault_outcome') or '-'}   Parts: {d.get('parts_used') or '-'}"]
    lines += ["", "Work carried out:", d.get("work_done") or "-"]
    cov = d.get("covered") or []
    if "Phone system" in cov:
        lines += ["", f"Phones: {d.get('platform') or '-'} | handsets {d.get('handsets_installed') or 0}/"
                      f"{d.get('handsets_required') or 0} ({d.get('handset_models') or '-'}) | "
                      f"users {d.get('users_configured') or 0}/{d.get('users_number') or 0} | DDI {d.get('ddi_range') or '-'}"]
        if d.get("features"):
            lines.append("Features: " + ", ".join(d["features"]))
    if "Broadband & network" in cov:
        lines += [f"Broadband: {d.get('bb_type') or '-'} {d.get('provider') or ''} {d.get('package') or ''} | "
                  f"measured {d.get('meas_down') or '?'}/{d.get('meas_up') or '?'} Mbps | router {d.get('router') or '-'} "
                  f"SN {d.get('router_serial') or '-'} | SSID {d.get('ssid') or '-'} (password not stored here)"]
    lines += ["", f"Outstanding: {d.get('outstanding') or 'None'}",
              f"Follow-up needed: {d.get('followup_needed') or '-'}   Billing: {d.get('billable') or '-'}",
              "Signed by customer: " + ("NOT SIGNED – " + (d.get("unavailable_reason") or "")
                                        if d.get("cust_unavailable") else (d.get("cust_sign_name") or "-"))]
    return "\n".join(lines)


@st.cache_data(ttl=3600, show_spinner=False)
def _zoho_from_addresses(_client):
    return _client.from_addresses()


def sender_options():
    """(list of {email,user_name,type}, error). Zoho's list, narrowed to [email] allowed_from if set."""
    allowed = [a.lower() for a in (secret("email", "allowed_from", []) or [])]
    err, addrs = None, []
    if not DEMO and zoho_client():
        try:
            addrs = _zoho_from_addresses(zoho_client())
        except ZohoError as e:
            err = str(e)
    if allowed:
        if addrs:
            addrs = [a for a in addrs if a["email"].lower() in allowed]
        else:  # token can't list addresses: trust the configured list (organisation emails)
            addrs = [{"email": a, "user_name": BRAND["name"], "type": "org_email"} for a in allowed]
    if not addrs and secret("smtp"):
        cfg = secret("smtp")
        addrs = [{"email": cfg.get("from_email", cfg.get("user")), "user_name": cfg.get("from_name", BRAND["name"]),
                  "type": "smtp"}]
    return addrs, err


def default_sender(emails):
    for cand in (ENG.get("send_as"), secret("email", "default_from")):
        if cand and cand in emails:
            return cand
    return emails[0]


def deliver(to_email, to_name, subject, html, text, attachments, linked):
    """Send via Zoho (logged on the account). Falls back to the backup SMTP login if set up."""
    senders, _ = sender_options()
    sender = next((x for x in senders if x["email"] == F.get("send_from")), senders[0] if senders else None)
    zoho_err = None
    if linked and sender and sender.get("type") != "smtp":
        try:
            z = zoho_client()
            ids = [z.upload_file(name, data, f"{mt}/{sub}") for name, data, mt, sub in attachments]
            rmod, rid = job_record(ss.job)
            z.send_mail(rid, sender, [{"user_name": to_name or to_email, "email": to_email}],
                        subject, html, ids, module=rmod)
            return f"{to_email} — sent from {sender['email']} via Zoho"
        except ZohoError as e:
            zoho_err = str(e)
    if secret("smtp"):
        send_email(to_email, subject, html, text, attachments)
        return f"{to_email} — sent via backup email" + (f" (Zoho said: {zoho_err})" if zoho_err else "")
    if zoho_err:
        raise RuntimeError(zoho_err)
    raise RuntimeError("Not linked to a Zoho account, and no backup email is set up.")


def send_email(to, subject, body_html, body_text, attachments):
    cfg = secret("smtp")
    if not cfg:
        raise RuntimeError("Email isn't set up in Secrets ([smtp]).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg.get('from_name', BRAND['name'])} <{cfg.get('from_email', cfg['user'])}>"
    msg["To"] = to
    if BRAND["office_email"]:
        msg["Reply-To"] = BRAND["office_email"]
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")
    for name, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    host, port = cfg.get("host", "smtp.gmail.com"), int(cfg.get("port", 587))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)


def customer_email(d):
    first = (d.get("cust_sign_name") or d.get("contact_name") or "").split(" ")[0] or "there"
    done = esc(d.get("work_done") or "").replace("\n", "<br>")
    outstanding = (d.get("outstanding") or "").strip()
    still = (f"<p><b>Still to do:</b> {esc(outstanding)} — we'll be in touch about this.</p>" if outstanding else "")
    html = f"""
    <div style="font-family:Arial,sans-serif;color:#1d2333;max-width:600px">
      <div style="background:#1f1450;color:#fff;padding:18px 22px;border-bottom:3px solid #00b5a3">
        <div style="font-size:12px;letter-spacing:2px;color:#00b5a3;font-weight:bold">{esc(BRAND['name']).upper()}</div>
        <div style="font-size:20px;font-weight:bold;margin-top:4px">Your engineer visit is complete</div>
      </div>
      <div style="padding:18px 22px;font-size:14px;line-height:1.55">
        <p>Hi {esc(first)},</p>
        <p>Thanks for having us on site today. {esc(d.get('engineer'))} has completed your
        <b>{esc((d.get('visit_type') or 'visit').lower())}</b> at {esc(d.get('customer_name'))}.
        Your job sheet (ref <b>{esc(d.get('job_no'))}</b>) is attached for your records.</p>
        <p style="background:#f4f5fa;border-left:3px solid #00b5a3;padding:10px 12px"><b>Work carried out</b><br>{done}</p>
        {still}
        <p>If anything isn't working as it should, just reply to this email or call us on {esc(BRAND['phone'])}.</p>
        <p>{esc(BRAND['signoff'])}</p>
      </div>
    </div>"""
    text = (f"Hi {first},\n\nThanks for having us on site today. {d.get('engineer')} has completed your "
            f"{(d.get('visit_type') or 'visit').lower()}. Your job sheet ({d.get('job_no')}) is attached.\n\n"
            f"Work carried out:\n{d.get('work_done')}\n\n"
            + (f"Still to do: {outstanding}\n\n" if outstanding else "")
            + f"Any problems, reply to this email or call {BRAND['phone']}.\n\n{BRAND['signoff']}")
    return html, text


def office_email(d):
    flags = []
    if d.get("followup_needed") == "Yes":
        flags.append("FOLLOW-UP VISIT NEEDED")
    if d.get("fault_outcome") and d["fault_outcome"] != "Resolved":
        flags.append(d["fault_outcome"].upper())
    if d.get("cust_unavailable"):
        flags.append("NOT SIGNED BY CUSTOMER")
    if d.get("billable") == "Chargeable":
        flags.append("CHARGEABLE")
    body = note_text(d)
    html = ("<div style='font-family:Arial,sans-serif;font-size:13px'>"
            + (f"<p style='color:#b45309;font-weight:bold'>⚑ {' · '.join(flags)}</p>" if flags else "")
            + f"<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>{esc(body)}</pre></div>")
    return flags, html, body


def do_sync():
    d = _pdf_fields()
    job = ss.job
    linked = is_linked(job)
    rmod, rid = job_record(job)
    photos = prepare_photos()
    steps = []
    with st.status("Completing job…", expanded=True) as status:
        st.write("Building the PDFs…")
        cust_pdf = build_pdf(d, BRAND, "customer", photos)
        office_pdf = build_pdf(d, BRAND, "office", photos)
        ref = d.get("job_no") or new_ref()
        safe_cust = _safe(d.get("customer_name") or "customer")[:40]
        fname = f"{ref}_{safe_cust}.pdf"

        # Zoho
        if DEMO or job.get("module") == "Demo":
            steps.append(("Saved to Zoho", None, "Demo mode — nothing was sent to Zoho"))
        elif not F.get("save_zoho") or not rid:
            steps.append(("Saved to Zoho", None, "Skipped — no Zoho account linked" if not rid else "Skipped"))
        else:
            st.write("Saving to Zoho…")
            z = zoho_client()
            try:
                z.add_note(rid, f"Job sheet {ref} – {d.get('visit_type')} – {d.get('engineer')}", note_text(d), module=rmod)
                z.attach_file(rid, fname, office_pdf, module=rmod)
                for pname, pdata in photos:
                    z.attach_file(rid, f"{ref}_{_safe(pname)}.jpg", pdata, "image/jpeg", module=rmod)
                steps.append(("Saved to Zoho", True, f"Note + PDF{f' + {len(photos)} photo(s)' if photos else ''} added to {d.get('customer_name')}"))
            except ZohoError as e:
                steps.append(("Saved to Zoho", False, str(e)))
            if job.get("module") not in ("Manual",) and F.get("mark_complete"):
                try:
                    if z.mark_completed(job["id"], job.get("module")):
                        steps.append(("Appointment marked complete", True, f"Status set to {z.completed_status}"))
                except ZohoError as e:
                    steps.append(("Appointment marked complete", False, str(e)))

        # Customer email
        if F.get("email_customer") and d.get("contact_email"):
            st.write("Emailing the customer…")
            try:
                h, t = customer_email(d)
                where = deliver(d["contact_email"].strip(), d.get("contact_name"),
                                f"Your {BRAND['name']} job sheet – {ref}", h, t,
                                [(fname, cust_pdf, "application", "pdf")], linked)
                steps.append(("Customer emailed", True, where))
            except Exception as e:
                steps.append(("Customer emailed", False, str(e)))
        else:
            steps.append(("Customer emailed", None, "Skipped"))

        # Office email
        if F.get("email_office") and BRAND["office_email"]:
            st.write("Emailing the office…")
            try:
                flags, h, t = office_email(d)
                subj = f"Job sheet {ref} – {d.get('customer_name')} – {d.get('visit_type')}"
                if flags:
                    subj = f"[{flags[0]}] " + subj
                atts = [(fname, office_pdf, "application", "pdf")]
                where = deliver(BRAND["office_email"], "Office", subj, h, t, atts, linked)
                steps.append(("Office emailed", True, where))
            except Exception as e:
                steps.append(("Office emailed", False, str(e)))
        else:
            steps.append(("Office emailed", None, "Skipped"))

        # Local record
        try:
            (HISTORY / f"{_safe(ref)}-customer.pdf").write_bytes(cust_pdf)
            (HISTORY / f"{_safe(ref)}.json").write_text(json.dumps({
                "ref": ref, "job_id": job["id"], "customer": d.get("customer_name"), "engineer": ENG["name"],
                "visit_type": d.get("visit_type"), "visit_date": F.get("visit_date"),
                "synced_at": datetime.now(UK).isoformat(), "steps": steps}, default=str))
            draft_path(job).unlink(missing_ok=True)
        except OSError:
            pass
        failed = any(s[1] is False for s in steps)
        status.update(label="Finished with some problems" if failed else "Job complete",
                      state="error" if failed else "complete")
    ss.result = {"ref": ref, "steps": steps, "cust_pdf": cust_pdf, "office_pdf": office_pdf,
                 "fname": fname, "customer": d.get("customer_name")}
    ss.screen = "done"


# =====================================================================
#  DONE SCREEN
# =====================================================================
def done_screen():
    r = ss.result
    failed = [s for s in r["steps"] if s[1] is False]
    with st.container(key="card-done"):
        section_header("!" if failed else "✓", f"Job sheet {r['ref']} sent" if not failed else f"Job sheet {r['ref']} — needs attention",
                       r["customer"] or "")
        rows = ""
        for label, ok, detail in r["steps"]:
            cls, ico = ("ok", "✓") if ok else (("fail", "!") if ok is False else ("skip", "–"))
            rows += (f'<div class="pe-step-row"><div class="ico {cls}">{ico}</div><div><div class="l">{esc(label)}</div>'
                     f'<div class="d">{esc(detail)}</div></div></div>')
        render_html(rows)
        if failed:
            st.warning("Something didn't go through. Download the PDF below and send it to the office, "
                       "or go back and try again when you have signal.")
            if st.button("Back to the sheet to retry", icon=":material/replay:", **FULL):
                ss.screen = "sheet"
                st.rerun()
        c1, c2 = st.columns(2)
        c1.download_button("Customer copy", data=r["cust_pdf"], file_name=r["fname"], mime="application/pdf",
                           icon=":material/download:", **FULL)
        c2.download_button("Office copy", data=r["office_pdf"], file_name=r["fname"].replace(".pdf", "_office.pdf"),
                           mime="application/pdf", icon=":material/download:", **FULL)
    if st.button("Back to my jobs", type="primary", icon=":material/arrow_back:", **FULL):
        ss.screen = "jobs"
        ss.job = None
        ss.F = {}
        ss.result = None
        st.rerun()


# =====================================================================
#  TOP BAR + ROUTER + HERO
# =====================================================================
with st.container(horizontal=True, vertical_alignment="center", key="topbar"):
    render_html(f'<div class="pe-brand"><div class="pe-logo">{ICON_TOOL}</div><div><div class="n">{esc(BRAND["name"])} Job Sheets</div>'
                f'<div class="u">{esc(ENG["name"])}</div></div></div>')
    if st.button("Log out", key="logout", icon=":material/logout:"):
        save_draft()
        for k in list(ss.keys()):
            del ss[k]
        st.rerun()

hero_slot = st.empty()
flash = ss.pop("flash", None)
if flash:
    (st.warning if flash[0] == "warn" else st.info)(flash[1])

if ss.screen == "sheet" and ss.job:
    sheet_screen()
elif ss.screen == "done" and ss.result:
    done_screen()
else:
    ss.screen = "jobs"
    jobs_screen()

# hero rendered last so the stepper reflects this run
screen = ss.screen
sheet_ok = screen == "sheet" and not [p for p in problems() if "signature" not in p.lower() and "sign-off" not in p.lower()]
signed = screen == "sheet" and bool(F.get("sig_engineer")) and (bool(F.get("sig_customer")) or bool(F.get("cust_unavailable")))
states = {
    "jobs": ["active", "", "", ""],
    "sheet": ["done", "done" if sheet_ok else "active", "done" if signed else ("active" if sheet_ok else ""), ""],
    "done": ["done", "done", "done", "done"],
}[screen]
labels = ["Pick job", "Fill in", "Sign off", "Synced"]
stepper = ""
for i, (lab, stt) in enumerate(zip(labels, states)):
    if i:
        stepper += '<div class="pe-step-sep"></div>'
    stepper += f'<div class="pe-step {stt}"><div class="num">{"✓" if stt == "done" else i + 1}</div><span class="lb">{lab}</span></div>'
hour = datetime.now(UK).hour
greet = "Morning" if hour < 12 else ("Afternoon" if hour < 17 else "Evening")
first = ENG["name"].split(" ")[0]
if screen == "jobs":
    title, sub = f"{greet}, <span>{esc(first)}</span>", "Your jobs from Zoho. Open one to start the job sheet."
elif screen == "sheet":
    title = f"{esc(F.get('customer_name') or 'New job')} <span>job sheet</span>"
    sub = esc(F.get("visit_type") or "")
else:
    bad = ss.result and any(x[1] is False for x in ss.result["steps"])
    title, sub = (("Nearly <span>done</span>", "Some steps need attention — see below.") if bad
                  else ("All <span>done</span>", "The job sheet has been sent."))
eyebrow = ('<span class="dot demo"></span>Demo mode — not connected to Zoho' if DEMO
           else '<span class="dot"></span>Connected to Zoho')
render_html(f'<div class="pe-hero"><div><div class="pe-eyebrow">{eyebrow}</div><div class="pe-title">{title}</div>'
            f'<div class="pe-sub">{sub}</div></div><div class="pe-stepper">{stepper}</div></div>', hero_slot)
