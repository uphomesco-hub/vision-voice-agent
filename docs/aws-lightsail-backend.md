# AWS Lightsail Backend Deploy

This backend can run on a single Lightsail Ubuntu instance as-is. The frontend can stay on Netlify and point `REACT_APP_BACKEND_URL` to this VM's public HTTPS URL.

## Recommended instance

- Start with the `512 MB` Linux plan if your current tiny VM is stable.
- Use `1 GB` if you want extra headroom for websocket sessions and image processing.

## 1. Create the instance

- Choose `Ubuntu 24.04 LTS` on Lightsail.
- Open ports `22`, `80`, and `443` in the Lightsail networking tab.
- Reserve a static IP and attach it to the instance.

## 2. Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
```

## 3. Upload the repo

```bash
sudo mkdir -p /opt/repair-assistant
sudo chown -R ubuntu:ubuntu /opt/repair-assistant
git clone <your-repo-url> /opt/repair-assistant
cd /opt/repair-assistant/backend
```

## 4. Create the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Set at least:

- `GOOGLE_API_KEY`
- `CORS_ORIGINS`

If you keep the default SQLite setup, data will live at `/opt/repair-assistant/backend/sessions.db`.

## 5. Start the backend once to verify

```bash
chmod +x start_server.sh
./start_server.sh
```

Check:

```bash
curl http://127.0.0.1:8001/api/health
```

## 6. Install the systemd service

```bash
sudo cp /opt/repair-assistant/deploy/lightsail/repair-assistant-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable repair-assistant-backend
sudo systemctl start repair-assistant-backend
sudo systemctl status repair-assistant-backend
```

Useful logs:

```bash
journalctl -u repair-assistant-backend -f
```

## 7. Put Nginx in front of it

Create `/etc/nginx/sites-available/repair-assistant`:

```nginx
server {
    listen 80;
    server_name api.example.com;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/repair-assistant /etc/nginx/sites-enabled/repair-assistant
sudo nginx -t
sudo systemctl reload nginx
```

## 8. Add HTTPS

Point your DNS `api` record at the Lightsail static IP, then:

```bash
sudo snap install core
sudo snap refresh core
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/bin/certbot
sudo certbot --nginx -d api.example.com
```

## 9. Connect Netlify frontend

In Netlify, set:

```text
REACT_APP_BACKEND_URL=https://api.example.com
```

The frontend will derive the websocket URL from that value.
