import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_can_create, require_user
from app.db import get_db
from app.models import Proposal, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals")
templates = Jinja2Templates(directory="app/templates")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Required per assets/intake-form-fields.md - blank is a form-validation
# error; filler/placeholder *content* is Claude's job to catch at
# generation time (spec section 7), not this form's.
REQUIRED_TEXT_FIELDS = [
    ("client_name", "Client name"),
    ("company_name", "Company name"),
    ("client_needs_summary", "Summary of client's needs"),
    ("project_scope", "Project scope"),
    ("goals_and_objectives", "Goals and objectives"),
    ("recommended_services", "Recommended services or deliverables"),
    ("proposed_timeline", "Proposed timeline"),
    ("estimated_pricing", "Estimated pricing"),
]


@router.get("/new")
def new_proposal_form(request: Request, user: User = Depends(require_can_create)):
    return templates.TemplateResponse(
        request=request, name="proposal_new.html", context={"error": None, "values": {}}
    )


@router.post("/new")
def create_proposal(
    request: Request,
    # Defaults ("" rather than Form(...)) matter here: Starlette's
    # urlencoded-form parser drops blank-valued fields entirely rather than
    # keeping them as "", so a required Form(...) field left empty in the
    # browser would 422 before ever reaching the validation below.
    client_name: str = Form(""),
    client_email: str = Form(""),
    company_name: str = Form(""),
    date_of_call: str = Form(""),
    client_needs_summary: str = Form(""),
    project_scope: str = Form(""),
    goals_and_objectives: str = Form(""),
    recommended_services: str = Form(""),
    proposed_timeline: str = Form(""),
    estimated_pricing: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    values = {
        "client_name": client_name,
        "client_email": client_email,
        "company_name": company_name,
        "date_of_call": date_of_call,
        "client_needs_summary": client_needs_summary,
        "project_scope": project_scope,
        "goals_and_objectives": goals_and_objectives,
        "recommended_services": recommended_services,
        "proposed_timeline": proposed_timeline,
        "estimated_pricing": estimated_pricing,
    }

    errors = []
    for field, label in REQUIRED_TEXT_FIELDS:
        if not values[field].strip():
            errors.append(f"{label} is required.")

    if not EMAIL_RE.match(client_email.strip()):
        errors.append("Client email must be a valid email address.")

    parsed_date = None
    try:
        parsed_date = datetime.strptime(date_of_call.strip(), "%Y-%m-%d")
    except ValueError:
        errors.append("Date of call must be a valid date.")

    if errors:
        return templates.TemplateResponse(
            request=request,
            name="proposal_new.html",
            context={"error": " ".join(errors), "values": values},
            status_code=400,
        )

    proposal = Proposal(
        client_name=client_name.strip(),
        client_email=client_email.strip().lower(),
        company_name=company_name.strip(),
        date_of_call=parsed_date,
        client_needs_summary=client_needs_summary.strip(),
        project_scope=project_scope.strip(),
        goals_and_objectives=goals_and_objectives.strip(),
        recommended_services=recommended_services.strip(),
        proposed_timeline=proposed_timeline.strip(),
        estimated_pricing=estimated_pricing.strip(),
        created_by=user.id,
        status="draft",
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    logger.info("User id=%s created proposal id=%s", user.id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.get("/{proposal_id}")
def view_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    if proposal.created_by != user.id and not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to view this proposal")
    return templates.TemplateResponse(
        request=request, name="proposal_detail.html", context={"proposal": proposal}
    )
