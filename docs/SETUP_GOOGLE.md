# Google Cloud setup — click by click

You need **two kinds of credentials**:

| Credential | Used for | Where it runs |
|---|---|---|
| **Service account** (JSON key) | read/write the Google Sheet | GitHub Actions |
| **OAuth Desktop client + refresh token** | upload to *your* YouTube channel | refresh token created once by you, then used by Actions |

Estimated time: 15 minutes.

---

## Part A — project & APIs

1. Go to <https://console.cloud.google.com/> and sign in with the Google account
   that owns the YouTube channel.
2. Top bar → project selector → **New project** → name `shorts-bot` → **Create**.
3. Make sure the new project is selected, then **APIs & Services → Library**:
   * search **Google Sheets API** → **Enable**
   * search **YouTube Data API v3** → **Enable**

---

## Part B — service account (for the Sheet)

4. **APIs & Services → Credentials → + Create credentials → Service account**
   * Name: `sheet-writer` → **Create and continue**
   * Role: leave empty (access is granted per-sheet by sharing) → **Continue** → **Done**
5. Click the new service account → **Keys** tab → **Add key → Create new key → JSON → Create**.
   A `.json` file downloads. Rename it **`service_account.json`**.
6. Open it in a text editor and copy the `client_email` value —
   looks like `sheet-writer@shorts-bot-123456.iam.gserviceaccount.com`.
   **This is the address you share your Google Sheet with (Editor).**

> In GitHub you don't upload the file: paste its whole contents into the
> `GCP_SA_JSON` secret.

---

## Part C — OAuth client (for YouTube uploads)

7. **APIs & Services → OAuth consent screen**
   * User type: **External** → **Create**
   * App name: `Shorts Bot`, support email: your email, developer email: your email → **Save and continue**
   * **Scopes** → *Add or remove scopes* → tick
     `https://www.googleapis.com/auth/youtube` → **Update** → **Save and continue**
   * **Test users** → **+ Add users** → add your own Gmail → **Save and continue**
8. **Back on the consent screen page, click “Publish app” → confirm
   (“In production”).**

   > ### ⚠️ This step matters
   > While the app is in **Testing** status, Google expires refresh tokens after
   > **7 days** — your bot would die every week. Publishing makes the token
   > permanent. Because the app is unverified you'll see an
   > “Google hasn’t verified this app” screen during login: click
   > **Advanced → Go to Shorts Bot (unsafe)**. That warning is expected for a
   > private, single-user app; nothing is being reviewed or shared publicly.

   **Before step 9, add yourself as a tester**, otherwise `auth_setup.py` fails with
   *“Access blocked: shorts-bot has not completed the Google verification process”*:

   * OAuth consent screen → **Audience** tab (older console: scroll down to **Test users**)
   * **+ ADD USERS** → your Gmail (e.g. `loginid7809@gmail.com`) → **Save**
   * Status must be **Testing**; then wait ~1 minute.

   See [§ Access blocked (403 access_denied)](#access-blocked-403-access_denied) below.

9. **APIs & Services → Credentials → + Create credentials → OAuth client ID**
   * Application type: **Desktop app** → name `shorts-bot-desktop` → **Create**
   * **Download JSON** → rename it **`client_secret.json`**.

---

## Part D — mint the refresh token (on your computer, once)

```bash
# in the project folder, with client_secret.json next to main.py
pip install -r requirements.txt
python auth_setup.py
```

* A browser opens → choose the account that owns the channel.
* If you see “Access blocked: this app’s request is invalid”, the OAuth client is not
  a **Desktop app** client — recreate it as Desktop and retry.
* If you see the unverified-app warning → **Advanced → Go to … (unsafe)**.
* Approve, then the terminal prints:

```
YT_CLIENT_ID=...
YT_CLIENT_SECRET=...
YT_REFRESH_TOKEN=1//0g...
```

Copy those three into GitHub secrets. Done — Actions can now upload forever.

**Re-running is safe:** if you ever lose the token, run `auth_setup.py` again and
replace the secret. Revoke old access at <https://myaccount.google.com/permissions>.

---

## Part E — the spreadsheet

1. Create a blank Google Sheet named e.g. **Shorts Queue**.
2. **Share** → paste the service-account `client_email` → role **Editor** → Send
   (you can untick "notify people").
3. Copy the id from the URL:
   `https://docs.google.com/spreadsheets/d/`**`1AbC…xyz`**`/edit#gid=0`
4. Create the tabs:

```bash
export SHEET_ID=1AbC…xyz
python tools/setup_sheet.py --show-email   # sanity check: prints the email again
python tools/setup_sheet.py                # creates Pending / Uploaded / Failed / Log / State
```

If you get `403 PERMISSION_DENIED`, the sheet isn't shared with that exact email —
that is the single most common setup mistake.

---

## Part F — GitHub secrets checklist

Repo → **Settings → Secrets and variables → Actions → Secrets**:

| Secret | From |
|---|---|
| `GCP_SA_JSON` | full contents of `service_account.json` (one line or pretty-printed, both fine) |
| `SHEET_ID` | Sheet URL id |
| `YT_CLIENT_ID` | `auth_setup.py` output |
| `YT_CLIENT_SECRET` | `auth_setup.py` output |
| `YT_REFRESH_TOKEN` | `auth_setup.py` output |
| `IG_COOKIES_B64` *(optional)* | see TROUBLESHOOTING §Instagram blocked |
| `HTTP_PROXY_URL` *(optional)* | residential proxy URL |

Then: **Actions → Instagram to YouTube Shorts → Run workflow** with `dry_run = true`
first. Check the *Log* tab in the Sheet and the workflow log; then run it for real.

---

## Access blocked (403 access_denied)

Symptom, right after `python auth_setup.py` opens the browser:

> **Access blocked: shorts-bot has not completed the Google verification process**
> shorts-bot has not completed the Google verification process. The app is currently
> being tested and can only be accessed by developer-approved testers.
> Error 403: access_denied

This is **not** a code problem. The OAuth app is in *Testing* mode and Google only lets
**developer-approved testers** in. Three things cause it:

### 1. Your account isn't a test user (most common)
* Cloud Console → **APIs & Services → OAuth consent screen → Audience**
  (old console: **Test users** section) → **+ ADD USERS** → `your-address@gmail.com`
  → **Save**.
* The address must be the **exact** Google account you sign in with. `@googlemail.com`
  and `@gmail.com` are the same mailbox but Google treats the strings separately —
  add the one shown in the error message.
* Propagation takes ~30–60 seconds. Re-run `auth_setup.py` afterwards.
* Testing mode allows 100 test users, so no need to remove anyone.

### 2. The browser is signed in to a different Google account
The OAuth window uses the account your browser session already has — if you're signed
into several accounts, it silently picks the default one, which may be the wrong
account. Fix:
* run the flow in an **incognito/private window**, or
* sign out of all accounts at <https://accounts.google.com> first, or
* add **every** account you use in that browser as a test user.

### 3. The OAuth client and the consent screen are in different projects
Test users are per **project**. If `client_secret.json` came from project *A* while you
added your email in project *B*, you get this exact 403. Check the `"project_id"` field
inside `client_secret.json` (`auth_setup.py` now prints it) and make sure the project
selector at the top of the Cloud Console shows the same project when you edit the
consent screen.

### After it works: publish the app
**OAuth consent screen → Audience → Publish app → Confirm.** Without this, a
*Testing*-mode refresh token **expires after 7 days** and your GitHub Action starts
failing with `invalid_grant`. Publishing (with the unverified-app warning) makes it
permanent. Only you use this app, so the 100-user unverified cap is irrelevant.

### What you should see when it's working
1. “Google hasn't verified this app” → **Advanced → Go to shorts-bot (unsafe)**.
2. The consent screen listing the YouTube permission → **Continue/Allow**
   (leave both scope checkboxes ticked).
3. Terminal prints `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` and writes
   `youtube_secrets.json`.

If you previously granted access and want a clean retry:
<https://myaccount.google.com/permissions> → remove *shorts-bot* → run `auth_setup.py` again.
