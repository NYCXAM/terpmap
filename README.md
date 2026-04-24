# UMD Map

A Python/Flask campus incident map where signed-in students can pin campus events such as traffic accidents, hazards, closures, activities, and safety notices.

## Features

- Google OAuth login, including optional `@umd.edu` hosted-domain hint.
- UMD account login for development/demo using an `@umd.edu` email verification code printed to the server console.
- Browser geolocation after login, with a "my location" marker.
- SQLite-backed campus reports with category, severity, description, timestamp, and author.
- Leaflet/OpenStreetMap map UI centered on University of Maryland.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

## Google OAuth

Create an OAuth client in Google Cloud Console and add this redirect URI:

```text
http://127.0.0.1:5000/auth/google/callback
```

Then run:

```bash
export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-client-secret"
export SECRET_KEY="a-long-random-secret"
python app.py
```

Real UMD SSO normally requires an institution-issued SAML/OIDC/CAS client. This app includes an `@umd.edu` email-code login flow so UMD account behavior can be tested locally without storing passwords.
