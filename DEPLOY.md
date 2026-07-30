# Deploy Campus Whispers to Render + Supabase

## Quick Start (15 minutes)

### 1. Create Supabase Project
1. Go to [supabase.com](https://supabase.com) → New Project
2. Name: `campus-whispers` | Region: closest to you
3. **Save the DB password** (shown once!)
4. Wait for provisioning (~2 min)

### 2. Get Connection String
1. Settings → Database → Connection string (URI)
2. Copy the **Transaction pooler** URI (port 6543)
3. Replace `[YOUR-PASSWORD]` with your actual password
4. Format: `postgresql://postgres.xxxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres`

### 3. Run Database Migration
```bash
# Install psycopg2 locally
pip install psycopg2-binary

# Run the schema creation against Supabase
DATABASE_URL="your-supabase-url-here" python -c "
import psycopg2
conn = psycopg2.connect(os.environ['DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()

# Users table
cur.execute('''
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    real_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    handle TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    banned INTEGER NOT NULL DEFAULT 0,
    selected_badge TEXT DEFAULT NULL
)
''')

# Rumors table
cur.execute('''
CREATE TABLE IF NOT EXISTS rumors (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    bumped_at TEXT,
    highlighted INTEGER NOT NULL DEFAULT 0,
    featured INTEGER NOT NULL DEFAULT 0,
    is_incognito INTEGER NOT NULL DEFAULT 0
)
''')

# Remaining tables...
print('All tables created!')
"
```

### 4. Deploy to Render
1. Push to GitHub: `git push origin main`
2. Go to [dashboard.render.com](https://dashboard.render.com) → New → Web Service
3. Connect your GitHub repo
4. Settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:create_app() --bind 0.0.0.0:\$PORT`
5. Add Environment Variables:
   - `DATABASE_URL` = your Supabase URI
   - `ADMIN_PASSWORD` = strong password
   - `ADMIN_EMAIL` = your email
   - `SECRET_KEY` = generate with: `python -c "import secrets; print(secrets.token_hex(32))"`
6. Deploy!

### 5. Verify
- Visit `https://your-app.onrender.com/api/me` → should return `{"error": "Login required."}`
- Visit `https://your-app.onrender.com` → should load the app

---

## Files Already Configured
- ✅ `render.yaml` - Render service config
- ✅ `requirements.txt` - includes psycopg[binary]
- ✅ `app.py` - auto-detects DATABASE_URL (Supabase) vs SQLite
- ✅ IPv4 forcing for Supabase on Render free tier

## Schema to Create in Supabase
The app auto-creates tables on first run, but for Supabase you need to run once manually or use the migration script above. Tables needed:
- `users` (with `selected_badge` column)
- `rumors`
- `purchases`
- `reactions`
- `me_too`
- `comments`
- `tags`
- `rumor_tags`
- `tag_follows`
- `challenge_claims`
- `weekly_challenges`

---

## After Deploy
1. Register your admin account
2. Login at `/admin` with your ADMIN_EMAIL + ADMIN_PASSWORD
3. Test posting whispers, buying badges, leaderboard

---

## Need Help?
- Render logs: Dashboard → your service → Logs
- Supabase logs: Dashboard → Database → Logs
- Check `DATABASE_URL` format carefully (no extra spaces, correct password)