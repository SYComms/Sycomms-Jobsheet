# SY Comms Engineer Job Sheets

Mobile-first web app that replaces the paper Installation Job Sheet.

**Theme:** always the dark SY Comms design with green selections, whatever the phone or browser is set to
(make sure the hidden `.streamlit` folder with `config.toml` is uploaded to GitHub too).

**Engineer flow:** log in with the shared app password + their name → see today's jobs from Zoho → open one (customer, address,
contact, email and office notes are pre-filled) → tap through the sheet → customer and engineer sign
on screen → **Complete & sync job**.

**What "Complete & sync" does**
1. Builds a branded A4 PDF (customer copy + office copy).
2. Adds a **Note** and the **PDF** (plus any site photos) to the customer's Zoho **Account**.
3. (Appointments module only) marks the Zoho **Appointment** as Completed.
4. Emails the customer their copy and the office its copy **through Zoho**, from the address the
   engineer picks (hello@, support@ …). Both emails are logged under the account's Emails in Zoho.
   The office subject gets flags like `[FOLLOW-UP VISIT NEEDED]`, `[CHARGEABLE]`, `[NOT SIGNED BY CUSTOMER]`.

Wi-Fi passwords are never put in Zoho or the office copy; they only appear on the customer's copy
if the engineer ticks the box.

## Files
| File | What it is |
|---|---|
| `app.py` | The app (screens, form, signatures, sync, emails). Dropdown lists are at the top — edit freely. |
| `zoho_client.py` | Zoho CRM connection (EU data centre). |
| `pdf_builder.py` | The PDF job sheet. |
| `.streamlit/config.toml` | Dark theme. |
| `secrets_TEMPLATE.toml` | What to paste into Streamlit Secrets. |

## Deploy (same as your other apps)
1. New GitHub repo (e.g. `sycomms-jobsheets`) → upload everything **except** `secrets_TEMPLATE.toml` values / any real secrets.
2. Streamlit Cloud → New app → pick the repo, main file `app.py`.
3. Settings → Secrets → paste `secrets_TEMPLATE.toml` with real values.
4. Engineers add the link to their phone home screen (Share → Add to Home Screen) so it opens like an app.

Tip: set `demo_mode = true` first to try it with sample jobs before connecting Zoho.

## Zoho setup — the easy way (inside the app)
On the login screen, open **Office: Zoho connection** and unlock it with the `admin_password` from
Secrets (or the app password if none is set). It shows the scope to copy into api-console.zoho.eu →
Self Client → Generate Code; paste the code back, press **Create refresh token**, and copy the line it
gives you into Secrets under `[zoho]`. **Check what it can do** confirms Deals, Meetings, accounts,
sending addresses and users all work; **Use it now** switches this session over straight away.

The scope it asks for:

```
ZohoCRM.modules.ALL,ZohoCRM.settings.emails.READ,ZohoCRM.send_mail.all.CREATE,ZohoCRM.Files.CREATE,ZohoCRM.users.READ,ZohoCRM.coql.READ
```

| Scope | Used for | If it's missing |
|---|---|---|
| `modules.ALL` | Reading appointments/accounts, notes, attachments, status | App can't work |
| `send_mail.all.CREATE` | Sending the emails | Falls back to the backup [smtp] login, if set |
| `Files.CREATE` | Attaching the PDF to the email | Same as above |
| `settings.emails.READ` | Listing hello@ / support@ etc. | Uses the `allowed_from` list in Secrets instead |
| `coql.READ` | Quicker lookup for the Appointments/Meetings diary | Not needed for the Deals diary |
| `users.READ` | Name list on the login screen + "Only my jobs" | Add `engineer_names` in Secrets |

Then swap the code for a refresh token (POST to
`https://accounts.zoho.eu/oauth/v2/token?grant_type=authorization_code&client_id=…&client_secret=…&code=…`)
and put `client_id`, `client_secret`, `refresh_token` in Secrets. You can reuse an existing refresh
token from another app (e.g. the Prospect Engine one) — the table shows what happens if a scope is
missing, so you can try it first and only regenerate if needed.

**Sending addresses:** hello@ and support@ must be set up in Zoho CRM as *Organization Emails*
(Setup → Channels → Email → Organization Emails) and verified — the same ones your other apps send from.

**Where the jobs come from** — `diary_sources` in Secrets (default: Deals + Meetings)
- **Deals**, read exactly like the Upgrade Tracker: each deal's `Appt_Date_Time` is a diary entry, only
  deals owned by @sycomms.co.uk staff (Deals also holds SY Plus). Chip shows the sales consultant.
- **Meetings** booked in Zoho, shown as "Meeting · Booked by …". A meeting can be linked to an Account,
  Contact, Deal or Lead — the app finds the customer either way. If it isn't linked to anything, the
  sheet shows a "Link this job to a Zoho account" search so the job sheet still lands on the right account.
  Meetings booked against a **Lead** are saved to (and emailed from) that Lead.
- Day view or "Next 7 days". If one source can't be read, the others still show with a warning.
- Using Zoho's Appointments module instead? Add `"Appointments__s"` to `diary_sources`
  (it also supports "Only my jobs" and marking the appointment completed).
- Not in the diary at all? Engineers can search any Zoho account, or start a blank sheet.

**Quotes from the quotation tool** — upload the quote's saved JSON:
- *Job not in the diary? → Start from a quote* creates the job sheet from it and links it to the Zoho
  customer automatically when the company name matches.
- *Load details from the quote* on any open job sheet fills in the kit sold (handsets and counts,
  softphone users, software, broadband package + expected speeds, router) and lists the full kit —
  headsets, Wi-Fi APs, switches etc. — in the office notes box. Customer details from Zoho are kept.
- Bank details in the quote file are never read or shown.

## Good to know
- Unfinished sheets auto-save as you go, so if an engineer loses signal or closes the page, reopening
  the job restores it (stored on the app server; cleared if Streamlit restarts the app).
- If a sync step fails (no signal, Zoho down), the done screen says exactly which one and lets the
  engineer download the PDF or go back and retry.
