# Traps planted in example_app (answer key)

Originally written for Janus (MIT, Copyright (c) 2026 pro1gramer-hic), translated to English.
This file lives outside `example_app/`, so it is never scanned and never sent to a model.

The secrets in `example_app/app/config.py` are **fake and intentional**. They exist so the
secrets scanner has something to find. They do not match any real provider key format.

1. **Configuration**: `NOTIFY_WEBHOOK_URL` is read in `app/notifications.py` but missing from
   `.env.example` and `deploy/production.env`. `POST /tasks` crashes in production.
   `PORT` is read in `app/config.py` but missing from `.env.example`.
2. **Documentation**: the README names `DB_URL` (the code reads `DATABASE_URL`), runs
   `python run.py` (the file does not exist; the app starts with `uvicorn app.main:app`),
   says port 5000 (the app uses 8000), documents `/stats` (the real route is `/report/top`),
   and claims that no secret is stored in the code.
3. **Secrets**: `JWT_SECRET` and `ADMIN_PASSWORD` are hardcoded in `app/config.py`.
4. **Impact** (no scanner yet): `calculate_priority` is called from `main.py` and `reports.py`
   and has no test. `POST /tasks` and `/report/top` have no test either.
5. **Failure triage** (no scanner yet): in `ci_logs/failed_run.txt`, the first real error is
   the `KeyError: 'NOTIFY_WEBHOOK_URL'` (`notifications.py` line 7, called from `main.py`
   line 20). The four failures after it follow from it.

The three scanners report 11 findings for traps 1 to 3:

| Scanner | Count | Findings |
|---|---|---|
| config | 3 | `NOTIFY_WEBHOOK_URL` missing from `deploy/production.env` (critical) and from `.env.example`; `PORT` missing from `.env.example` |
| secrets | 2 | `JWT_SECRET`, `ADMIN_PASSWORD` |
| docs | 6 | `DB_URL`, `python run.py`, port 5000, `GET /stats`, false security claim, undocumented `GET /report/top` |
