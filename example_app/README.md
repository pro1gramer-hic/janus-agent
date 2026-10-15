# Taskly

A small task tracking API.

## Setup

Set the `DB_URL` environment variable to your database connection string.

## Run

```bash
python run.py
```

The API serves on port 5000.

## Endpoints

- `GET /health`: health check
- `POST /tasks`: create a task
- `GET /tasks/{id}`: get a task
- `GET /stats`: task statistics

## Security

No secrets are stored in the code. All credentials come from environment variables.
