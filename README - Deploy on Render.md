# Deploying KSE Quantum on Render (free)

Goal: live URL like https://kse-quantum.onrender.com - free, no credit card.

## Step 1 - Push the files to your GitHub repo

The repo already has app.py etc. Add these two files from this folder:
- render.yaml
- (requirements.txt / .streamlit/config.toml are already there)

Upload via GitHub web: Add file -> Upload files / Create new file.

## Step 2 - Create the Render account

1. Go to https://dashboard.render.com/register
2. Sign up with GitHub (email verification may apply).
3. Grant access to your repositories.

## Step 3 - Create the web service

1. Click New + -> Web Service
2. Connect your `kse-quantum` repository.
3. Settings (auto-filled if render.yaml is present):
   - Runtime: Python 3
   - Build command: pip install -r requirements.txt
   - Start command: streamlit run app.py --server.port $PORT --server.headless true
   - Instance type: Free
4. Click Create Web Service.

## Step 4 - Wait 5-10 minutes

Watch the deploy logs. When it says "Your service is live",
your URL is in the top bar (https://kse-quantum.onrender.com).

## Updating later

Edit app.py on GitHub -> Render redeploys automatically.

## Free-tier notes

- Service sleeps after ~15 min of no visitors; wake takes ~1 min.
- 750 free instance-hours per month - enough for one always-on service.
- Cache resets on redeploy; the stale-data banner covers gaps.

## Troubleshooting

| Problem | Fix |
|---|---|
| Build error on streamlit-autorefresh | remove that line from requirements.txt |
| App crashes at boot | check logs for missing package; add to requirements.txt |
| Stale-data banner | PSX throttles datacenter IPs sometimes; cache fallback is working |
