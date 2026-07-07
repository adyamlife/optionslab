# OptionLab — Deployment Guide

Server: admin@192.168.1.199 (Ubuntu 24.04, local network)

## URLs after deploy
- OptionLab:  http://192.168.1.199:8001
- Honerfit:   http://192.168.1.199:80

## Step-by-step (first deploy)

### 1. Server setup (run ONCE on server)
SSH into the server and run setup:
```bash
ssh admin@192.168.1.199
bash /home/admin/deploy/setup.sh
```
> setup.sh must be on the server first — copy it manually or include it in the first bundle.

### 2. Bundle and transfer code (run on Windows)
```powershell
cd "C:\Project Y"
.\deploy\bundle.ps1
```
This creates a tarball (excluding data/, .env, venv/) and scps it to the server.

### 3. Transfer data files (run on Windows, first deploy only)
```powershell
cd "C:\Project Y"
.\deploy\transfer_data.ps1
```
Copies: `.env`, `data/ml_training.duckdb`, `data/models/*.joblib`

### 4. Deploy on server
```bash
ssh admin@192.168.1.199
bash /home/admin/deploy/deploy.sh
```
Extracts bundle, sets up venv, installs deps, restarts service.

---

## Updating (subsequent deploys)
Only steps 2 and 4 are needed for code updates.
Only step 3 is needed when you retrain models locally and want to push to server.

---

## Useful server commands
```bash
# Service status
sudo systemctl status optionlab

# Live logs
tail -f /opt/optionlab/data/optionlab.log

# Restart
sudo systemctl restart optionlab

# nginx status
sudo systemctl status nginx
sudo nginx -t          # test config

# Check both apps are running
curl -s -o /dev/null -w "%{http_code}" http://localhost:5001/
curl -s -o /dev/null -w "%{http_code}" http://localhost:2108/
```

---

## E*TRADE OAuth note
Since this server is on a local network (no public URL), register
`http://192.168.1.199:8001/etrade/callback` as the OAuth callback URL
in E*TRADE developer settings. Daily re-auth is done by visiting
http://192.168.1.199:8001 from any browser on the local network.
