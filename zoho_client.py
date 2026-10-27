"""
Zoho CRM helper for the SY Comms Engineer Job Sheet app.

Reads the engineer diary (Zoho CRM Appointments, or Meetings/Events),
pulls the linked Account + Contact, and writes the finished job sheet
back (note + PDF attachment + appointment status).

All settings come from Streamlit secrets, section [zoho]:
    client_id, client_secret, refresh_token
    accounts_url  (default https://accounts.zoho.eu)
    api_domain    (default https://www.zohoapis.eu)
    api_version   (default v6)
    appointments_module  "Appointments__s" (Zoho Appointments) or "Events" (Meetings)
    engineer_field       field that holds the engineer (default "Owner")
    completed_status     value to set when the job is done (default "Completed")
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")


class ZohoError(Exception):
    pass


def _lookup_id(v):
    return v.get("id") if isinstance(v, dict) else None


def _lookup_name(v):
    if isinstance(v, dict):
        return v.get("name") or v.get("full_name") or ""
    return v if isinstance(v, str) else ""


class ZohoClient:
    def __init__(self, cfg: dict):
        self.client_id = cfg["client_id"]
        self.client_secret = cfg["client_secret"]
        self.refresh_token = cfg["refresh_token"]
        self.accounts_url = cfg.get("accounts_url", "https://accounts.zoho.eu").rstrip("/")
        self.api_domain = cfg.get("api_domain", "https://www.zohoapis.eu").rstrip("/")
        self.version = cfg.get("api_version", "v6")
        self.module = cfg.get("appointments_module", "Appointments__s")
        self.engineer_field = cfg.get("engineer_field", "Owner")
        self.completed_status = cfg.get("completed_status", "Completed")
        self._token = None
        self._token_exp = 0.0
        self._users = None

    # ------------------------------------------------------------ plumbing
    @property
    def is_events(self) -> bool:
        return self.module.lower() in ("events", "meetings")

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post(
            f"{self.accounts_url}/oauth/v2/token",
            params={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        data = r.json() if r.content else {}
        if "access_token" not in data:
            raise ZohoError(f"Couldn't log in to Zoho: {data.get('error', r.text[:200])}")
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _req(self, method, path, **kw):
        url = f"{self.api_domain}/crm/{self.version}/{path.lstrip('/')}"
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Zoho-oauthtoken {self._access_token()}"
        r = requests.request(method, url, headers=headers, timeout=30, **kw)
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
        except ValueError:
            raise ZohoError(f"Zoho returned an unexpected reply ({r.status_code}).")
        if r.status_code >= 400:
            msg = data.get("message") or data.get("code") or str(data)[:200]
            if isinstance(data.get("data"), list) and data["data"]:
                d0 = data["data"][0]
                msg = f"{d0.get('code', '')}: {d0.get('message', '')} {json.dumps(d0.get('details', {}))[:150]}"
            raise ZohoError(f"Zoho {r.status_code} on {path.split('?')[0]} — {msg}")
        return data

    def _get_record(self, module, rid):
        data = self._req("GET", f"{module}/{rid}")
        rows = data.get("data") or []
        return rows[0] if rows else {}

    # ------------------------------------------------------------ users
    def users(self) -> dict:
        """id -> {name, email}. Empty dict if the users scope is missing."""
        if self._users is None:
            try:
                data = self._req("GET", "users", params={"type": "ActiveUsers", "per_page": 200})
                self._users = {
                    u["id"]: {"name": u.get("full_name", ""), "email": (u.get("email") or "").lower()}
                    for u in data.get("users", [])
                }
            except ZohoError:
                self._users = {}
        return self._users

    # ------------------------------------------------------------ diary
    def _fields(self):
        if self.is_events:
            return dict(title="Event_Title", start="Start_DateTime", end="End_DateTime",
                        extra=["Owner", "What_Id", "Venue"])
        extra = ["Owner", "Status", "Appointment_For"]
        if self.engineer_field not in extra:
            extra.append(self.engineer_field)
        return dict(title="Appointment_Name", start="Appointment_Start_Time",
                    end="Appointment_End_Time", extra=extra)

    def list_jobs(self, day: date) -> list[dict]:
        f = self._fields()
        start = datetime.combine(day, dtime(0, 0), UK).isoformat()
        end = datetime.combine(day, dtime(23, 59, 59), UK).isoformat()
        where = f"{f['start']} between '{start}' and '{end}'"
        tail = f"from {self.module} where {where} order by {f['start']} asc limit 200"
        selects = [
            ["id", f["title"], f["start"], f["end"], *f["extra"]],
            ["id", f["title"], f["start"], f["end"], "Owner"],
        ]
        last_err = None
        for cols in selects:
            try:
                data = self._req("POST", "coql", json={"select_query": f"select {', '.join(cols)} {tail}"})
                return [self._light(rec) for rec in data.get("data", [])]
            except ZohoError as e:
                last_err = e
        raise last_err

    def _engineer_of(self, rec):
        v = rec.get(self.engineer_field)
        users = self.users()
        if isinstance(v, dict):
            uid = v.get("id")
            u = users.get(uid, {})
            return {"id": uid, "name": v.get("name") or u.get("name", ""),
                    "email": (v.get("email") or u.get("email", "")).lower()}
        if isinstance(v, str):
            return {"id": None, "name": v, "email": ""}
        return {"id": None, "name": "", "email": ""}

    def _light(self, rec):
        f = self._fields()
        link = rec.get("What_Id") if self.is_events else rec.get("Appointment_For")
        status = rec.get("Status") or ""
        if isinstance(status, dict):
            status = status.get("name", "")
        return {
            "id": rec.get("id"),
            "module": self.module,
            "title": rec.get(f["title"]) or "(untitled job)",
            "start": rec.get(f["start"]),
            "end": rec.get(f["end"]),
            "status": status,
            "engineer": self._engineer_of(rec),
            "for_name": _lookup_name(link),
            "hydrated": False,
        }

    # ------------------------------------------------------------ full job
    def hydrate(self, job: dict) -> dict:
        rec = self._get_record(self.module, job["id"])
        job = dict(job)
        f = self._fields()
        job["title"] = rec.get(f["title"]) or job.get("title")
        job["start"] = rec.get(f["start"]) or job.get("start")
        job["end"] = rec.get(f["end"]) or job.get("end")
        job["notes"] = rec.get("Remarks") or rec.get("Description") or ""
        svc = rec.get("Service_Name")
        job["service"] = _lookup_name(svc)
        addr = rec.get("Address") if not self.is_events else rec.get("Venue")
        job["appt_address"] = addr if isinstance(addr, str) else ""

        if self.is_events:
            link, mod = rec.get("What_Id"), rec.get("$se_module")
            who = rec.get("Who_Id")
        else:
            link = rec.get("Appointment_For")
            mod = rec.get("$se_module") or rec.get("$appointment_for_module")
            if isinstance(link, dict) and not mod:
                m = link.get("module")
                mod = m.get("api_name") if isinstance(m, dict) else m
            who = None

        account_id, contact = None, None
        lid = _lookup_id(link)
        if lid:
            if mod == "Contacts":
                contact = self._get_record("Contacts", lid)
                account_id = _lookup_id(contact.get("Account_Name"))
            elif mod == "Deals":
                deal = self._get_record("Deals", lid)
                account_id = _lookup_id(deal.get("Account_Name"))
                cid = _lookup_id(deal.get("Contact_Name"))
                if cid:
                    contact = self._get_record("Contacts", cid)
            else:  # Accounts (or unknown -> assume account)
                account_id = lid
        if not contact and _lookup_id(who):
            contact = self._get_record("Contacts", _lookup_id(who))

        if account_id:
            job.update(self.account_details(account_id, contact))
        elif contact:
            job.update(self._contact_fields(contact))
            job["account_name"] = _lookup_name(contact.get("Account_Name")) or job.get("for_name", "")
        job["hydrated"] = True
        return job

    def _contact_fields(self, c):
        name = c.get("Full_Name") or " ".join(x for x in [c.get("First_Name"), c.get("Last_Name")] if x)
        return {
            "contact_name": name or "",
            "contact_phone": c.get("Mobile") or c.get("Phone") or "",
            "contact_email": c.get("Email") or "",
        }

    def account_details(self, account_id, contact=None) -> dict:
        acc = self._get_record("Accounts", account_id)

        def addr(prefix):
            street = acc.get(f"{prefix}_Street") or ""
            parts = [street, acc.get(f"{prefix}_City"), acc.get(f"{prefix}_State")]
            return ", ".join(p.strip() for p in parts if p and p.strip()), (acc.get(f"{prefix}_Code") or "").strip()

        site, pc = addr("Shipping")
        if not site:
            site, pc = addr("Billing")
        out = {
            "account_id": account_id,
            "account_name": acc.get("Account_Name", ""),
            "account_number": acc.get("Account_Number") or "",
            "address": site,
            "postcode": pc,
            "account_phone": acc.get("Phone") or "",
        }
        if not contact:
            try:
                data = self._req("GET", f"Accounts/{account_id}/Contacts",
                                 params={"fields": "Full_Name,First_Name,Last_Name,Email,Phone,Mobile", "per_page": 5})
                rows = data.get("data") or []
                contact = rows[0] if rows else None
            except ZohoError:
                contact = None
        if contact:
            out.update(self._contact_fields(contact))
        if not out.get("contact_phone"):
            out["contact_phone"] = out["account_phone"]
        return out

    def search_accounts(self, term: str) -> list[dict]:
        term = term.strip()
        if len(term) < 2:
            return []
        data = self._req("GET", "Accounts/search", params={"word": term, "per_page": 15})
        res = []
        for a in data.get("data", []) or []:
            res.append({
                "id": a["id"],
                "name": a.get("Account_Name", ""),
                "number": a.get("Account_Number") or "",
                "town": a.get("Shipping_City") or a.get("Billing_City") or "",
                "postcode": a.get("Shipping_Code") or a.get("Billing_Code") or "",
            })
        return res

    # ------------------------------------------------------------ write back
    def add_note(self, account_id, title, content):
        self._req("POST", f"Accounts/{account_id}/Notes",
                  json={"data": [{"Note_Title": title[:120], "Note_Content": content}]})

    def attach_file(self, account_id, filename, content: bytes, mime="application/pdf"):
        self._req("POST", f"Accounts/{account_id}/Attachments",
                  files={"file": (filename, content, mime)})

    def mark_completed(self, job_id):
        if self.is_events:
            return False  # Meetings have no status field – note + attachment is enough
        self._req("PUT", f"{self.module}/{job_id}",
                  json={"data": [{"Status": self.completed_status}]})
        return True


# ---------------------------------------------------------------- demo data
def demo_jobs(day: date, engineer_name: str) -> list[dict]:
    def at(h, m):
        return datetime.combine(day, dtime(h, m), UK).isoformat()

    eng = {"id": "demo", "name": engineer_name, "email": ""}
    return [
        {"id": "demo-1", "module": "Demo", "title": "New phone system + FTTP install",
         "start": at(9, 0), "end": at(13, 0), "status": "Upcoming", "engineer": eng,
         "for_name": "Harbour View Dental Practice", "hydrated": True,
         "account_id": "demo-acc-1", "account_name": "Harbour View Dental Practice",
         "account_number": "SYC0142", "address": "Unit 4, Meole Brace Retail Park, Shrewsbury",
         "postcode": "SY3 9XX", "contact_name": "Rachel Evans", "contact_phone": "01743 000111",
         "contact_email": "rachel@example.com", "service": "Installation",
         "notes": "6 x Yealink T54W, 1 x W73P DECT for reception. Gamma FTTP 500/75. Customer wants hunt group for reception + voicemail to email."},
        {"id": "demo-2", "module": "Demo", "title": "FAULT – no inbound calls on main number",
         "start": at(14, 0), "end": at(15, 30), "status": "Upcoming", "engineer": eng,
         "for_name": "Oakfield Motors Ltd", "hydrated": True,
         "account_id": "demo-acc-2", "account_name": "Oakfield Motors Ltd",
         "account_number": "SYC0098-FT", "address": "12 Station Road, Wellington",
         "postcode": "TF1 0XX", "contact_name": "Gary Hughes", "contact_phone": "07700 900123",
         "contact_email": "gary@example.com", "service": "Fault visit",
         "notes": "Customer reports main number rings out / goes to busy since Monday. Outbound OK."},
        {"id": "demo-3", "module": "Demo", "title": "Wi-Fi coverage health check",
         "start": at(16, 0), "end": at(17, 0), "status": "Upcoming", "engineer": eng,
         "for_name": "The Riverside Café", "hydrated": True,
         "account_id": "demo-acc-3", "account_name": "The Riverside Café",
         "account_number": "SYC0177", "address": "3 Welsh Bridge, Shrewsbury",
         "postcode": "SY1 1XX", "contact_name": "Tom Price", "contact_phone": "07700 900456",
         "contact_email": "tom@example.com", "service": "Maintenance", "notes": "Weak Wi-Fi in the back seating area."},
    ]
