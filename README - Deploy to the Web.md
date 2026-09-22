# Deploying KSE Quantum to the Web — step by step

Goal: a public URL like `https://kse-quantum.streamlit.app` that anyone can open,
running at zero cost, updated automatically whenever you change the code.

You need: a free GitHub account (github.com) and nothing else.

## Step 1 — Create the GitHub repository

1. Go to https://github.com/new
2. Repository name: `kse-quantum` (or anything you like)
3. Choose **Public** (required for the free Streamlit Community Cloud).
4. Do NOT tick "Add a README" — we already have our files.
5. Click **Create repository**.

## Step 2 — Upload the files

1. On the new repo page, click **"uploading an existing file"**.
2. Drag in ALL files from the `Web Deployment` folder — including the
   `.streamlit` folder.
3. Click **Commit changes**.

(If drag-and-drop misses hidden items like `.streamlit`, install GitHub
Desktop — but the web upload usually works.)

## Step 3 — Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in **with your GitHub account**.
2. Click **Create app → Paste a GitHub URL**.
3. Repository: `YOUR-USERNAME/kse-quantum` · Branch: `main`
4. Main file path: `app.py`
5. App URL: choose something like `kse-quantum` (this becomes your link).
6. Click **Deploy**. First build takes 3–5 minutes.

## Step 4 — Share it

Your live link is `https://kse-quantum.streamlit.app`. Test it yourself,
then post it in r/PakStocks, LinkedIn, and investor Facebook groups.

## Step 5 — Update later

Edit `app.py` (or ask me to edit it), re-upload/commit to GitHub,
Streamlit Cloud redeploys automatically within a minute or two.

## Notes & limitations of the free tier

- The app sleeps after ~7 days of no traffic; the first visitor after a sleep
  waits ~1 minute. Use it daily yourself, or post a "daily market recap"
  that keeps it warm.
- The disk is ephemeral: `.kse_cache` may reset on redeploy. Fine — the cache
  is a convenience, not a database.
- Free tier has ~1 GB RAM. With hundreds of simultaneous users you may see
  slowness; at that point, upgrade to a paid host (still cheap).

## Troubleshooting

| Problem | Fix |
|---|---|
| Build fails on `streamlit-autorefresh` | It has no wheel on some builders; the app runs fine without it — remove that line from requirements.txt |
| Deps error mentioning `pyarrow` | Nothing in the app needs it; ignore |
| App shows stale-data banner | Expected when PSX/SBP is unreachable from the cloud IP — the cache fallback is doing its job |
