# 🚀 Deploy to Render — Job Scraper & AI Resume Matcher

## Step 1: Push to GitHub

```bash
cd d:\job-scraper-agent
git init
git add .
git commit -m "Initial commit - Job Scraper & AI Resume Matcher"
git remote add origin https://github.com/YOUR_USERNAME/job-scraper-agent.git
git push -u origin main
```

## Step 2: Create Render Web Service

1. Go to [render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub repo
3. Configure:
   - **Name**: `job-scraper-agent`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`

## Step 3: Set Environment Variables on Render

In **Environment** tab, add these:

| Key | Value |
|-----|-------|
| `DATABASE_URL` | your neon.tech PostgreSQL URL |
| `GROQ_API_KEY` | your Groq API key |
| `SECRET_KEY` | any random secret string |

> ⚠️ Do NOT commit `.env` to GitHub. It's in `.gitignore`.

## Step 4: Deploy

Click **Deploy** and wait ~2-3 minutes. Your app will be live at:
`https://job-scraper-agent.onrender.com`

## Files Created for Deployment

| File | Purpose |
|------|---------|
| `Procfile` | Tells Render how to start the app (gunicorn) |
| `runtime.txt` | Specifies Python 3.11 |
| `requirements.txt` | Includes `gunicorn` and `psycopg2-binary` |
| `.gitignore` | Excludes `.env`, `__pycache__`, `*.db` |

## Architecture

```
Render Web Service
    ↓
gunicorn (2 workers)
    ↓
Flask app (app.py)
    ↓
PostgreSQL (Neon.tech) ← DATABASE_URL
    ↓
Groq API ← GROQ_API_KEY (AI job parsing + resume matching)
```
