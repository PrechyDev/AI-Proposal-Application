from fastapi import Depends, FastAPI, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

app = FastAPI(title="AI Proposal Application")

templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request, name="index.html", context={"title": "AI Proposal Application"}
    )
