# Kotak Neo Auto-Login Dashboard

Automated daily login to Kotak Neo Trade API with a web dashboard for Holdings, Positions, Orders, Trades, and Limits.

## Local development

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in credentials in .env
python app.py
# Open http://localhost:5000
```

## Cloud deployment (Render + GitHub Actions)

**Goal:** Dashboard live 24/7 at a public URL, auto-login at 9 AM IST Mon-Fri, zero dependency on your local PC.

### 1. Push code to GitHub (private repo)

Create a new **private** repo at github.com, then from the project folder:

```powershell
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

Confirm `.env` is NOT pushed (it's gitignored).

### 2. Deploy to Render

1. Go to https://render.com → Sign in with GitHub
2. New → Blueprint → connect your repo
3. Render reads `render.yaml` and creates the web service
4. Set environment variables (from your local `.env`):
   - KOTAK_CONSUMER_KEY
   - KOTAK_UCC
   - KOTAK_MOBILE
   - KOTAK_MPIN
   - KOTAK_TOTP_SECRET
5. Deploy. You'll get a URL like `https://kotak-dashboard-xxxx.onrender.com`

### 3. Configure GitHub Actions cron

In your GitHub repo:

1. Settings → Secrets and variables → Actions → New repository secret
2. Name: `RENDER_APP_URL`
3. Value: your Render URL (no trailing slash)

The workflow at `.github/workflows/daily-login.yml` will now run Mon-Fri at 9 AM IST.

To test immediately:
- Actions tab → "Daily Kotak Login at 9 AM IST" → "Run workflow"

### 4. Share with Ganesh

Send Ganesh the Render URL. He can bookmark it and check his holdings/positions any time.

## Files

- `app.py` - Flask dashboard (5 tabs)
- `auto_login.py` - Standalone login script (for local Task Scheduler)
- `run_login.bat` - Windows batch wrapper for Task Scheduler
- `setup_schedule.bat` - One-time Windows Task Scheduler setup
- `requirements.txt` - Python dependencies
- `render.yaml` - Render deployment config
- `.github/workflows/daily-login.yml` - GitHub Actions cron trigger
- `.env.example` - Template for credentials

## Security notes

- Never commit `.env` - it's in `.gitignore`
- On Render, credentials go in environment variables dashboard
- TOTP secret is the most sensitive value - rotate if leaked
- Use a private GitHub repo only
