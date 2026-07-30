# Campus Whispers 🤫

An anonymous rumor/confession board for your college class.
Classmates post **anonymously** (no login, no name, no IP stored).
Only **you** (the admin) can log in and see / delete everything.
All data lives in a single SQLite file on your server.

Built with the **test-driven-development** skill — 10 passing tests.

## Run it locally

```bash
cd campus-whispers
python -m venv venv
venv\Scripts\python -m pip install flask pytest
venv\Scripts\python run.py
```

Then open:
- Public board:  http://localhost:5000
- Admin panel:   http://localhost:5000/admin
  - Password: `admin123` (change it — see below)

Classmates on your Wi-Fi reach it at your LAN IP (shown in the server log),
e.g. `http://192.168.x.x:5000`.

## Change the admin password / secret

Set env vars before running (do NOT commit the real password):

```bash
set ADMIN_PASSWORD=your-strong-password
set SECRET_KEY=some-random-string
set DB_PATH=campus_whispers.db
venv\Scripts\python run.py
```

## Tests

```bash
venv\Scripts\python -m pytest -q
```

67 tests covering: register/login, password hashing, handle-only public feed,
admin identity reveal, ban, security headers, semantic HTML, the pluggable DB
layer (SQLite local / Postgres in prod), the engagement suite, **and the
privacy / account-recovery layer**:

| Feature | What it adds | Psych lever |
|---|---|---|
| Reactions (😂🔥💯😮) + "Me too" | `POST /api/rumors/<id>/react`, `/metoo` | Tribe reward / "I am not alone" |
| Anonymous comments | `POST/GET /api/rumors/<id>/comments`, `DELETE /api/comments/<id>` | Investment + Tribe |
| Posting streak | `GET /api/me` (streak + at-risk flag, 1-day grace) | Self reward / loss aversion |
| Hot/Rising feed + teaser | `GET /api/rumors?sort=hot|rising`, `/<id>/teaser` | Hunt reward / curiosity gap |
| Tags + follow | `GET /api/tags`, `POST/DELETE /api/tags/<name>/follow`, `GET /api/me/tags`, post `tags[]` | Investment / internal trigger |
| Daily digest | `POST /api/admin/digest/send` (admin) | External trigger (email) |

### Privacy & account recovery

Goal: a handle ties a person to their posts, so if a phone is inspected the
link must not be visible. The handle stays in the DB + admin view for
accountability, but is hidden from the whole user-facing UI.

| Feature | What it adds |
|---|---|
| No handle at signup | `POST /api/register` — `handle` optional; auto-generates `anon_xxxxxxxx` when omitted |
| Login by email | Handle field removed from login/register/chat UI |
| Anonymous chat | Comments render as "You" / "Anonymous" in the UI (API returns handle for admin only) |
| Forgot password | `POST /api/forgot-password` — no account enumeration (identical neutral reply whether or not the email exists); notifies admin via log |
| Admin reset | `POST /api/admin/users/<id>/reset-password` (admin-gated) |

## Engagement design notes

Built from real research, not guesswork:
- **Hook Model (Nir Eyal)**: Trigger -> Action -> Variable Reward -> Investment.
- **Information-Gap Theory (Loewenstein)**: the `/teaser` endpoint hides text
  to open a curiosity gap that pulls a click.
- **Streak grace window**: a single missed day does NOT reset the streak
  (Nikzad 2021 - broken streaks cause 22% churn unless recoverable within 72h).
- Reactions/comments never leak real name or email in public responses.

## Deploy: Render (free web) + Supabase Postgres (free, no expiry)

**Why this combo:** Netlify only hosts static sites — it can't run this
Flask backend or a database. Render runs the backend free (sleeps when
idle). SQLite is **not** viable on Render (ephemeral filesystem wipes it),
so we use an external **Supabase Postgres** (free 500 MB, permanent).

### 1. Create a free Supabase project
- Go to https://supabase.com → New project (free tier).
- Wait for it to provision. Open **Project Settings → Database**.
- Copy the **URI** (looks like
  `postgresql://postgres:<password>@db.<id>.supabase.co:5432/postgres`).
- The app creates the `users` / `rumors` tables automatically on first run.

### 2. Push to GitHub
- Create a repo (e.g. `campus-whispers`).
- `git remote add origin <your-repo-url> && git push -u origin main`

### 3. Deploy on Render
- https://render.com → **New → Web Service** → connect the GitHub repo.
- Render auto-detects `render.yaml`. Otherwise set:
  - Build: `pip install -r requirements.txt`
  - Start: `gunicorn "app:create_app()" --bind 0.0.0.0:$PORT`
- In **Environment**, add:
  - `DATABASE_URL` = your Supabase URI (from step 1)
  - `ADMIN_PASSWORD` = a strong password (you'll use it at `/admin`)
  - `SECRET_KEY` = any long random string
- Deploy. Render gives you a `https://campus-whispers.onrender.com` URL.
  The TLS cert (HTTPS) is automatic — satisfies the spec's encryption req.

### 4. Verify
- Open the URL → register a test account → post.
- `/admin` → log in with `ADMIN_PASSWORD` → see real names, ban users.

## Local dev vs prod
- **Local:** `run.py` uses SQLite (`campus_whispers.db`). No setup needed.
- **Prod:** if `DATABASE_URL` is set, the app transparently uses Postgres.
  Same code, no branching in routes.

## Important

This is for your class's fun/community use. Because posts name real people:
- You (admin) can delete anything — use it.
- Don't use it for bullying, threats, or defamation. Keep it light.
- The ban capability exists precisely so you can pull anything harmful.
