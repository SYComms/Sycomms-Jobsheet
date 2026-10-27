# SY Comms Engineer Job Sheets

Mobile-first web app that replaces the paper Installation Job Sheet.

**Engineer flow:** log in with the shared app password + their name → see today's jobs from Zoho → open one (customer, address,
contact, email and office notes are pre-filled) → tap through the sheet → customer and engineer sign
on screen → **Complete & sync job**.

**What "Complete & sync" does**
1. Builds a branded A4 PDF (customer copy + office copy).
2. Adds a **Note** and the **PDF** (plus any site photos) to the customer's Zoho **Account**.
3. Marks the Zoho **Appointment** as Completed.
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

## Zoho setup (api-console.zoho.eu)
Use a **Self Client** and generate a code with these scopes:

```
ZohoCRM.modules.ALL,ZohoCRM.coql.READ,ZohoCRM.users.READ,ZohoCRM.settings.emails.READ,ZohoCRM.send_mail.all.CREATE,ZohoCRM.Files.CREATE
```

| Scope | Used for | If it's missing |
|---|---|---|
| `modules.ALL` | Reading appointments/accounts, notes, attachments, status | App can't work |
| `send_mail.all.CREATE` | Sending the emails | Falls back to the backup [smtp] login, if set |
| `Files.CREATE` | Attaching the PDF to the email | Same as above |
| `settings.emails.READ` | Listing hello@ / support@ etc. | Uses the `allowed_from` list in Secrets instead |
| `coql.READ` | Fast diary lookup | Slower fallback is used automatically |
| `users.READ` | Name list on the login screen + "Only my jobs" | Add `engineer_names` in Secrets |

Then swap the code for a refresh token (POST to
`https://accounts.zoho.eu/oauth/v2/token?grant_type=authorization_code&client_id=…&client_secret=…&code=…`)
and put `client_id`, `client_secret`, `refresh_token` in Secrets. You can reuse an existing refresh
token from another app (e.g. the Prospect Engine one) — the table shows what happens if a scope is
missing, so you can try it first and only regenerate if needed.

**Sending addresses:** hello@ and support@ must be set up in Zoho CRM as *Organization Emails*
(Setup → Channels → Email → Organization Emails) and verified — the same ones your other apps send from.

**Where the jobs come from**
- Default: Zoho CRM **Appointments** module (`Appointments__s`). The appointment's *Appointment For*
  can be an Account, Contact or Deal — the app finds the Account either way.
- If visits are booked as **Meetings** instead, set `appointments_module = "Events"`.
- "Only my jobs" matches the appointment **Owner** to the name the engineer logs in with, so names
  should be spelt as they are in Zoho (the login list offers your Zoho users' names, or the
  `engineer_names` list in Secrets). If engineers are recorded in a different field, set
  `engineer_field` to that field's API name. They can always switch "Only my jobs" off.
- Not in the diary? Engineers can search any Zoho account, or start a blank sheet.

## Good to know
- Unfinished sheets auto-save as you go, so if an engineer loses signal or closes the page, reopening
  the job restores it (stored on the app server; cleared if Streamlit restarts the app).
- If a sync step fails (no signal, Zoho down), the done screen says exactly which one and lets the
  engineer download the PDF or go back and retry.
