from fastapi import FastAPI, HTTPException

from app.config import settings
from app.notifications import notify
from app.reports import top_tasks
from app.services import TASKS, calculate_priority, create_task

app = FastAPI(title="Taskly")


@app.get("/health")
def health():
    return {"status": "ok", "env": settings["APP_ENV"]}


@app.post("/tasks")
def add_task(title: str, due_in_days: int = 7, urgent: bool = False):
    task = create_task(title, due_in_days, urgent)
    task["priority"] = calculate_priority(task)
    notify(f"New task: {title}")
    return task


@app.get("/report/top")
def report_top(limit: int = 3):
    return {"top": top_tasks(limit)}


@app.get("/tasks/{task_id}")
def get_task(task_id: int):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")
    return TASKS[task_id]
