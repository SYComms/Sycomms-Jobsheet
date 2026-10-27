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
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import requests

UK = ZoneInfo("Europe/London")
CODE_VERSION = "2026-10-27.9"  # bump when this file changes


class ZohoError(Exception):
    pass


def _lookup_id(v):
    return v.get("id") if isinstance(v, dict) else None


def _lookup_name(v):
    if isinstance(v, dict):
        return v.get("name") or v.get("full_name") or ""
    return v if isinstance(v, str) else ""


def _norm_company(v):
    v = re.sub(r"[^a-z0-9 ]+", " ", str(v or "").lower())
    v = re.sub(r"\b(ltd|limited|plc|llp|the|co|uk)\b", " ", v)
    return " ".join(v.split())


def _same_booking(meeting, deal):
    """A meeting the consultant booked for a hidden (e.g. Dem Booked) deal: same person, same
    day (already the case), and the meeting is for the same company."""
    m_eng, d_eng = meeting.get("engineer") or {}, deal.get("engineer") or {}
    same_person = (m_eng.get("id") and str(m_eng.get("id")) == str(d_eng.get("id"))) or \
                  (m_eng.get("name") and m_eng.get("name", "").lower() == d_eng.get("name", "").lower())
    if not same_person:
        return False
    deal_names = {_norm_company(deal.get("title")), _norm_company(deal.get("for_name"))} - {""}
    for v in (meeting.get("title"), meeting.get("for_name")):
        mv = _norm_company(v)
        if not mv:
            continue
        for dn in deal_names:
            if mv == dn or (len(dn) >= 5 and dn in mv) or (len(mv) >= 5 and mv in dn):
                return True
    return False


class ZohoClient:
    def __init__(self, cfg: dict):
        self.client_id = cfg["client_id"]
        self.client_secret = cfg["client_secret"]
        self.refresh_token = cfg["refresh_token"]
        self.accounts_url = cfg.get("accounts_url", "https://accounts.zoho.eu").rstrip("/")
        self.api_domain = cfg.get("api_domain", "https://www.zohoapis.eu").rstrip("/")
        self.version = cfg.get("api_version", "v6")
        self.module = cfg.get("appointments_module", "Deals")
        # Diary sources read side by side. Default: Deals (like the Upgrade Tracker) + Meetings.
        src = cfg.get("diary_sources")
        if not src:
            src = ["Deals", "Events"] if self.module.lower() in ("deals", "events", "meetings") else [self.module]
        self.sources = [("Events" if m.lower() == "meetings" else m) for m in src]
        self.last_warnings = []
        self.engineer_field = cfg.get("engineer_field", "Owner")
        self.completed_status = cfg.get("completed_status", "Completed")
        # Deals diary (same as the Upgrade Tracker): the date/time field on the Deal,
        # and the owner email domain that marks SY Comms deals (Deals also holds SY Plus).
        self.deal_date_field = cfg.get("deal_date_field", "Appt_Date_Time")
        self.deal_owner_domain = (cfg.get("deal_owner_domain", "@sycomms.co.uk") or "").lower()
        # Sales appointments engineers don't attend. Deals at these stages are hidden, along with
        # the meeting the consultant booked for the same company on the same day.
        hide = cfg.get("hide_deal_stages", ["Dem Booked"])
        self.hide_stages = {str(x).strip().lower() for x in (hide or [])}
        self._deals_cache = (0.0, None)
        self._token = None
        self._token_exp = 0.0
        self._users = None
        self._users_until = 0.0
        self.users_error = ""

    # ------------------------------------------------------------ plumbing
    @property
    def is_events(self) -> bool:
        return self.sources == ["Events"]

    @property
    def is_deals(self) -> bool:
        """True when the diary includes Deals/Meetings, whose owner is whoever booked it
        (not the engineer), so 'Only my jobs' doesn't apply."""
        return any(m in ("Deals", "Events") for m in self.sources)

    @staticmethod
    def can_mark_complete_job(job) -> bool:
        return (job or {}).get("module") == "Appointments__s"

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
        """id -> {name, email}. Kept for an hour once loaded; if Zoho refuses (busy, or no
        users permission) it's retried after 30 seconds rather than remembered as empty."""
        now = time.time()
        fresh = self._users is not None and now < self._users_until
        if not fresh:
            try:
                data = self._req("GET", "users", params={"type": "ActiveUsers", "per_page": 200})
                self._users = {
                    str(u["id"]): {"name": u.get("full_name", ""), "email": (u.get("email") or "").lower()}
                    for u in data.get("users", [])
                }
                self._users_until = now + 3600
                self.users_error = ""
            except ZohoError as e:
                self._users = self._users or {}
                self._users_until = now + 30
                self.users_error = str(e)
        return self._users

    # ------------------------------------------------------------ diary
    def _fields(self, module):
        if module == "Events":
            return dict(title="Event_Title", start="Start_DateTime", end="End_DateTime",
                        extra=["Owner", "What_Id", "Who_Id", "Venue", "Description"])
        extra = ["Owner", "Status", "Appointment_For"]
        if self.engineer_field not in extra:
            extra.append(self.engineer_field)
        return dict(title="Appointment_Name", start="Appointment_Start_Time",
                    end="Appointment_End_Time", extra=extra)

    def list_jobs(self, day: date) -> list[dict]:
        """All diary entries for the day across every source. One source failing doesn't
        hide the others — its error is kept in last_warnings."""
        jobs, errors, hidden_deals = [], [], []
        for module in self.sources:
            try:
                if module == "Deals":
                    for j in self._all_deal_jobs():
                        if j["day"] != day.isoformat():
                            continue
                        if (j.get("status") or "").strip().lower() in self.hide_stages:
                            hidden_deals.append(j)
                        else:
                            jobs.append(j)
                else:
                    jobs += self._list_module(day, module)
            except ZohoError as e:
                errors.append(f"{'Meetings' if module == 'Events' else module}: {e}")
        self.last_warnings = errors
        if errors and len(errors) == len(self.sources):
            raise ZohoError(" | ".join(errors))
        if hidden_deals:
            jobs = [j for j in jobs if not (j.get("module") == "Events" and
                                            any(_same_booking(j, d) for d in hidden_deals))]
        return sorted(jobs, key=lambda j: j.get("start") or "")

    def _list_module(self, day, module):
        f = self._fields(module)
        start = datetime.combine(day, dtime(0, 0), UK).isoformat()
        end = datetime.combine(day, dtime(23, 59, 59), UK).isoformat()
        errs = []
        # 1) COQL (needs ZohoCRM.coql.READ) — paged, so busy days aren't cut off at 200
        base = ["id", f["title"], f["start"], f["end"]]
        variants = [base + f["extra"] + (["Participants"] if module == "Events" else []),
                    base + f["extra"], base + ["Owner"]]
        where = f"from {module} where {f['start']} between '{start}' and '{end}' order by {f['start']} asc"
        for cols in variants:
            try:
                rows = []
                for pg in range(20):
                    data = self._req("POST", "coql", json={
                        "select_query": f"select {', '.join(cols)} {where} limit {pg * 200}, 200"})
                    got = data.get("data", []) or []
                    rows += got
                    if len(got) < 200 and not (data.get("info") or {}).get("more_records"):
                        break
                return self._fill_names([self._light(rec, module) for rec in rows], module)
            except ZohoError as e:
                errs.append(str(e))
        # 2) Search API (only needs the module scope) — paged
        try:
            crit = f"(({f['start']}:greater_equal:{start})and({f['start']}:less_equal:{end}))"
            rows = []
            for page in range(1, 21):
                data = self._req("GET", f"{module}/search", params={"criteria": crit, "per_page": 200, "page": page})
                rows += data.get("data", []) or []
                if not (data.get("info") or {}).get("more_records"):
                    break
            return self._fill_names(sorted([self._light(rec, module) for rec in rows], key=lambda j: j.get("start") or ""), module)
        except ZohoError as e:
            errs.append(str(e))
        # 3) Plain list of recently changed records, filtered by day
        try:
            jobs = []
            for page in range(1, 6):
                data = self._req("GET", module, params={
                    "fields": ",".join([f["title"], f["start"], f["end"], *f["extra"]]),
                    "per_page": 200, "page": page, "sort_by": "Modified_Time", "sort_order": "desc"})
                for rec in data.get("data", []) or []:
                    try:
                        if datetime.fromisoformat(rec.get(f["start"])).astimezone(UK).date() == day:
                            jobs.append(self._light(rec, module))
                    except (TypeError, ValueError):
                        pass
                if not (data.get("info") or {}).get("more_records"):
                    break
            return self._fill_names(sorted(jobs, key=lambda j: j.get("start") or ""), module)
        except ZohoError as e:
            raise ZohoError(f"{e}  (other lookups also failed: {errs[-1] if errs else ''})")

    def _fill_names(self, jobs, module):
        """The quick diary lookup gives a meeting's customer as an ID only. Fetch the day's
        meetings again in batches of 100 (one request each) to get the customer names for the list."""
        if module != "Events":
            return jobs
        need = [j for j in jobs if not j.get("for_name") and j.get("id")]
        for i in range(0, len(need), 100):
            batch = need[i:i + 100]
            try:
                data = self._req("GET", "Events", params={"ids": ",".join(str(j["id"]) for j in batch),
                                                          "fields": "What_Id,Who_Id,Participants"})
            except ZohoError:
                return jobs  # names will still fill in when the job is opened
            by_id = {str(r.get("id")): r for r in data.get("data", []) or []}
            for j in batch:
                r = by_id.get(str(j["id"])) or {}
                name = _lookup_name(r.get("What_Id")) or _lookup_name(r.get("Who_Id"))
                if not name:
                    for pt in r.get("Participants") or []:
                        if isinstance(pt, dict) and pt.get("type") in ("contact", "lead") and pt.get("name"):
                            name = pt["name"]
                            break
                if name:
                    j["for_name"] = name
        return jobs

    def _engineer_of(self, rec):
        v = rec.get(self.engineer_field)
        users = self.users() if isinstance(v, dict) and not v.get("name") else {}
        if isinstance(v, dict):
            uid = v.get("id")
            u = users.get(str(uid), {})
            return {"id": uid, "name": v.get("name") or u.get("name", ""),
                    "email": (v.get("email") or u.get("email", "")).lower()}
        if isinstance(v, str):
            return {"id": None, "name": v, "email": ""}
        return {"id": None, "name": "", "email": ""}

    def _light(self, rec, module):
        f = self._fields(module)
        is_ev = module == "Events"
        link = rec.get("What_Id") if is_ev else rec.get("Appointment_For")
        status = "Meeting" if is_ev else (rec.get("Status") or "")
        if isinstance(status, dict):
            status = status.get("name", "")
        eng = self._engineer_of(rec)
        out = {
            "id": rec.get("id"),
            "module": module,
            "title": rec.get(f["title"]) or "(untitled job)",
            "start": rec.get(f["start"]),
            "end": rec.get(f["end"]),
            "status": status,
            "engineer": eng,
            "for_name": _lookup_name(link) or (_lookup_name(rec.get("Who_Id")) if is_ev else ""),
            "hydrated": False,
        }
        people = [eng.get("name", "")]
        people_ids = [str(eng["id"])] if eng.get("id") else []
        if is_ev:
            out["consultant"] = eng.get("name", "")
            out["booked_label"] = "Host"
            users = None
            for pt in rec.get("Participants") or []:
                if not isinstance(pt, dict) or pt.get("type") not in ("user", None):
                    continue  # staff only — customer contacts/leads invited to a meeting aren't hosts
                nm = pt.get("name") or ""
                if pt.get("participant"):
                    people_ids.append(str(pt.get("participant")))
                if not nm and pt.get("type") == "user":
                    users = users if users is not None else self.users()
                    nm = users.get(str(pt.get("participant")), {}).get("name", "")
                if nm:
                    people.append(nm)
        out["people"] = [p for p in dict.fromkeys(people) if p]
        out["people_ids"] = list(dict.fromkeys(people_ids))
        return out

    # ------------------------------------------------------------ deals diary
    def _all_deal_jobs(self) -> list[dict]:
        """Every Deal with a diary date, read the same way as the Upgrade Tracker
        (plain paged list — no COQL scope needed). Cached for 3 minutes."""
        ts, cached = self._deals_cache
        if cached is not None and time.time() - ts < 180:
            return cached
        df = self.deal_date_field
        field_sets = [f"Deal_Name,Owner,Account_Name,Contact_Name,Stage,Description,{df}",
                      f"Deal_Name,Owner,Account_Name,Stage,{df}"]
        records, err = None, None
        for fields in field_sets:
            try:
                records = []
                for page in range(1, 51):
                    data = self._req("GET", "Deals", params={
                        "fields": fields, "per_page": 200, "page": page,
                        "sort_by": "Modified_Time", "sort_order": "desc"})
                    records.extend(data.get("data", []) or [])
                    if not (data.get("info") or {}).get("more_records"):
                        break
                break
            except ZohoError as e:
                records, err = None, e
        if records is None:
            raise err
        jobs = []
        for r in records:
            raw = r.get(df)
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(raw)
                dt = dt.astimezone(UK) if dt.tzinfo else dt.replace(tzinfo=UK)
            except ValueError:
                continue
            owner = r.get("Owner") or {}
            if self.deal_owner_domain and not (owner.get("email") or "").lower().endswith(self.deal_owner_domain):
                continue
            acc = r.get("Account_Name") or {}
            jobs.append({
                "id": r.get("id"),
                "module": "Deals",
                "title": r.get("Deal_Name") or acc.get("name") or "(untitled deal)",
                "start": dt.isoformat(),
                "end": None,
                "day": dt.date().isoformat(),
                "status": r.get("Stage") or "",
                "engineer": {"id": owner.get("id"), "name": owner.get("name", ""),
                             "email": (owner.get("email") or "").lower()},
                "consultant": owner.get("name", ""),
                "booked_label": "Consultant",
                "people": [owner.get("name", "")] if owner.get("name") else [],
                "for_name": acc.get("name", "") if isinstance(acc, dict) else "",
                "account_id": _lookup_id(acc),
                "contact_id": _lookup_id(r.get("Contact_Name")),
                "notes": r.get("Description") or "",
                "hydrated": False,
            })
        jobs.sort(key=lambda j: j["start"])
        self._deals_cache = (time.time(), jobs)
        return jobs

    # ------------------------------------------------------------ full job
    def hydrate(self, job: dict) -> dict:
        if job.get("module") == "Deals":
            job = dict(job)
            contact = None
            if job.get("contact_id"):
                try:
                    contact = self._get_record("Contacts", job["contact_id"])
                except ZohoError:
                    contact = None
            if job.get("account_id"):
                job.update(self.account_details(job["account_id"], contact))
            elif contact:
                job.update(self._contact_fields(contact))
            job.setdefault("account_name", job.get("for_name", ""))
            job["hydrated"] = True
            return job
        module = job.get("module") or self.module
        is_ev = module == "Events"
        rec = self._get_record(module, job["id"])
        job = dict(job)
        f = self._fields(module)
        job["title"] = rec.get(f["title"]) or job.get("title")
        job["start"] = rec.get(f["start"]) or job.get("start")
        job["end"] = rec.get(f["end"]) or job.get("end")
        job["notes"] = rec.get("Remarks") or rec.get("Description") or ""
        job["service"] = _lookup_name(rec.get("Service_Name"))
        addr = rec.get("Venue") if is_ev else rec.get("Address")
        job["appt_address"] = addr if isinstance(addr, str) else ""

        if is_ev:
            link, mod, who = rec.get("What_Id"), rec.get("$se_module"), rec.get("Who_Id")
        else:
            link = rec.get("Appointment_For")
            mod = rec.get("$se_module") or rec.get("$appointment_for_module")
            if isinstance(link, dict) and not mod:
                m = link.get("module")
                mod = m.get("api_name") if isinstance(m, dict) else m
            who = None

        # Work out which customer the booking belongs to. Zoho can record it in several places:
        # "Related To" (What_Id + $se_module), the contact (Who_Id), or — on newer meeting
        # screens — the Participants list. Try them all, and keep a note of what Zoho said.
        info, account_id, contact, lead_id = [], None, None, None
        norm = {"account": "Accounts", "accounts": "Accounts", "deal": "Deals", "deals": "Deals",
                "potentials": "Deals", "contact": "Contacts", "contacts": "Contacts",
                "lead": "Leads", "leads": "Leads"}

        def resolve(m, rid):
            nonlocal account_id, contact, lead_id
            if m == "Contacts":
                contact = self._get_record("Contacts", rid)
                account_id = account_id or _lookup_id(contact.get("Account_Name"))
            elif m == "Deals":
                deal = self._get_record("Deals", rid)
                account_id = account_id or _lookup_id(deal.get("Account_Name"))
                cid = _lookup_id(deal.get("Contact_Name"))
                if cid and not contact:
                    contact = self._get_record("Contacts", cid)
            elif m == "Leads":
                if not self._get_record("Leads", rid):
                    raise ZohoError("lead not found")
                lead_id = rid
            else:
                if not self._get_record("Accounts", rid):
                    raise ZohoError("account not found")
                account_id = rid

        lid = _lookup_id(link)
        mod_n = norm.get(str(mod or "").strip().lower())
        if lid:
            info.append(f"Related to: {_lookup_name(link) or lid} ({mod or 'type not given'})")
            for m in ([mod_n] if mod_n else ["Accounts", "Deals", "Contacts", "Leads"]):
                try:
                    resolve(m, lid)
                    break
                except ZohoError as e:
                    info.append(f"couldn't open it as {m}: {str(e)[:80]}")
        if not account_id and not lead_id and _lookup_id(who):
            info.append(f"Contact: {_lookup_name(who) or _lookup_id(who)}")
            try:
                resolve("Contacts", _lookup_id(who))
            except ZohoError as e:
                info.append(f"couldn't open the contact: {str(e)[:80]}")
        if is_ev and not account_id and not lead_id:
            for pt in rec.get("Participants") or []:
                if not isinstance(pt, dict) or pt.get("type") not in ("contact", "lead"):
                    continue
                pid = pt.get("participant") or pt.get("id")
                info.append(f"Participant: {pt.get('name') or pid} ({pt.get('type')})")
                try:
                    resolve("Contacts" if pt.get("type") == "contact" else "Leads", pid)
                except ZohoError as e:
                    info.append(f"couldn't open it: {str(e)[:80]}")
                if account_id or lead_id:
                    break
        job["link_info"] = "; ".join(info) or "Zoho has no customer, contact or participant on this booking"

        if account_id:
            job.update(self.account_details(account_id, contact))
        elif lead_id:
            job.update(self.lead_details(lead_id))
        elif contact:
            job.update(self._contact_fields(contact))
            job["account_name"] = _lookup_name(contact.get("Account_Name")) or job.get("for_name", "")
        job["hydrated"] = True
        return job

    def lead_details(self, lead_id) -> dict:
        """A meeting booked against a Lead (not yet a customer). Job sheet notes/emails go on the Lead."""
        ld = self._get_record("Leads", lead_id)
        name = ld.get("Full_Name") or " ".join(x for x in [ld.get("First_Name"), ld.get("Last_Name")] if x)
        street = ", ".join(x.strip() for x in [ld.get("Street") or "", ld.get("City") or "", ld.get("State") or ""] if x and x.strip())
        return {
            "record_module": "Leads", "record_id": lead_id,
            "account_name": ld.get("Company") or name, "account_number": "",
            "address": street, "postcode": (ld.get("Zip_Code") or "").strip(),
            "contact_name": name or "", "contact_phone": ld.get("Mobile") or ld.get("Phone") or "",
            "contact_email": ld.get("Email") or "",
        }

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
            "record_module": "Accounts", "record_id": account_id,
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
        """Customers (Accounts) and not-yet-converted Leads matching the words typed."""
        term = term.strip()
        if len(term) < 2:
            return []
        res, errs = [], []
        try:
            data = self._req("GET", "Accounts/search", params={"word": term, "per_page": 15})
            for a in data.get("data", []) or []:
                res.append({
                    "id": a["id"], "module": "Accounts", "kind": "Customer",
                    "name": a.get("Account_Name", ""),
                    "number": a.get("Account_Number") or "",
                    "town": a.get("Shipping_City") or a.get("Billing_City") or "",
                    "postcode": a.get("Shipping_Code") or a.get("Billing_Code") or "",
                })
        except ZohoError as e:
            errs.append(e)
        try:
            data = self._req("GET", "Leads/search", params={"word": term, "per_page": 10})
            for l in data.get("data", []) or []:
                person = l.get("Full_Name") or " ".join(x for x in [l.get("First_Name"), l.get("Last_Name")] if x)
                res.append({
                    "id": l["id"], "module": "Leads", "kind": "Lead",
                    "name": l.get("Company") or person or "(lead)",
                    "number": person if l.get("Company") else "",
                    "town": l.get("City") or "", "postcode": l.get("Zip_Code") or "",
                })
        except ZohoError as e:
            errs.append(e)
        if not res and len(errs) == 2:
            raise errs[0]
        return res

    # ------------------------------------------------------------ write back
    def add_note(self, account_id, title, content, module="Accounts"):
        self._req("POST", f"{module}/{account_id}/Notes",
                  json={"data": [{"Note_Title": title[:120], "Note_Content": content}]})

    def attach_file(self, account_id, filename, content: bytes, mime="application/pdf", module="Accounts"):
        self._req("POST", f"{module}/{account_id}/Attachments",
                  files={"file": (filename, content, mime)})

    # ------------------------------------------------------------ email via Zoho
    def from_addresses(self) -> list[dict]:
        """Addresses Zoho lets us send from (org emails like hello@/support@ + the token user's)."""
        data = self._req("GET", "settings/emails/actions/from_addresses")
        out = []
        for a in data.get("from_addresses", []) or []:
            if a.get("email"):
                out.append({"email": a["email"], "user_name": a.get("user_name") or a["email"],
                            "type": a.get("type", ""), "default": bool(a.get("default"))})
        return out

    def upload_file(self, filename, content: bytes, mime="application/pdf") -> str:
        """Upload to Zoho's file store; returns the encrypted file id used for email attachments."""
        data = self._req("POST", "files", files={"file": (filename, content, mime)})
        rows = data.get("data") or []
        fid = rows[0].get("details", {}).get("id") if rows else None
        if not fid:
            raise ZohoError(f"Zoho didn't return a file id for {filename}.")
        return fid

    def send_mail(self, account_id, sender: dict, to: list[dict], subject: str, html: str,
                  attachment_ids=(), cc: list[dict] | None = None, module="Accounts"):
        """Send an email from the customer's Account record — Zoho logs it under the account's Emails."""
        msg = {
            "from": {"user_name": sender.get("user_name") or sender["email"], "email": sender["email"]},
            "to": to,
            "subject": subject,
            "content": html,
            "mail_format": "html",
        }
        if sender.get("type") == "org_email":
            msg["org_email"] = True
        if cc:
            msg["cc"] = cc
        if attachment_ids:
            msg["attachments"] = [{"id": i} for i in attachment_ids]
        data = self._req("POST", f"{module}/{account_id}/actions/send_mail", json={"data": [msg]})
        rows = data.get("data") or []
        if rows and str(rows[0].get("status", "")).lower() == "error":
            raise ZohoError(f"{rows[0].get('code')}: {rows[0].get('message')}")
        return True

    def mark_completed(self, job_id, module="Appointments__s"):
        if module != "Appointments__s":
            return False  # Meetings have no status field – note + attachment is enough
        self._req("PUT", f"Appointments__s/{job_id}",
                  json={"data": [{"Status": self.completed_status}]})
        return True


# ---------------------------------------------------------------- demo data
def demo_jobs(day: date, engineer_name: str) -> list[dict]:
    def at(h, m):
        return datetime.combine(day, dtime(h, m), UK).isoformat()

    eng = {"id": "demo", "name": engineer_name, "email": ""}
    return [
        {"id": f"demo-1-{day.isoformat()}", "module": "Demo", "title": "New phone system + FTTP install",
         "start": at(9, 0), "end": at(13, 0), "status": "Upcoming", "engineer": eng,
         "for_name": "Harbour View Dental Practice", "hydrated": True,
         "account_id": "demo-acc-1", "account_name": "Harbour View Dental Practice",
         "account_number": "SYC0142", "address": "Unit 4, Meole Brace Retail Park, Shrewsbury",
         "postcode": "SY3 9XX", "contact_name": "Rachel Evans", "contact_phone": "01743 000111",
         "contact_email": "rachel@example.com", "service": "Installation",
         "notes": "6 x Yealink T54W, 1 x W73P DECT for reception. Gamma FTTP 500/75. Customer wants hunt group for reception + voicemail to email."},
        {"id": f"demo-2-{day.isoformat()}", "module": "Demo", "title": "FAULT – no inbound calls on main number",
         "start": at(14, 0), "end": at(15, 30), "status": "Upcoming", "engineer": eng,
         "for_name": "Oakfield Motors Ltd", "hydrated": True,
         "account_id": "demo-acc-2", "account_name": "Oakfield Motors Ltd",
         "account_number": "SYC0098-FT", "address": "12 Station Road, Wellington",
         "postcode": "TF1 0XX", "contact_name": "Gary Hughes", "contact_phone": "07700 900123",
         "contact_email": "gary@example.com", "service": "Fault visit",
         "notes": "Customer reports main number rings out / goes to busy since Monday. Outbound OK."},
        {"id": f"demo-3-{day.isoformat()}", "module": "Demo", "title": "Wi-Fi coverage health check",
         "start": at(16, 0), "end": at(17, 0), "status": "Upcoming", "engineer": eng,
         "for_name": "The Riverside Café", "hydrated": True,
         "account_id": "demo-acc-3", "account_name": "The Riverside Café",
         "account_number": "SYC0177", "address": "3 Welsh Bridge, Shrewsbury",
         "postcode": "SY1 1XX", "contact_name": "Tom Price", "contact_phone": "07700 900456",
         "contact_email": "tom@example.com", "service": "Maintenance", "notes": "Weak Wi-Fi in the back seating area."},
    ]
