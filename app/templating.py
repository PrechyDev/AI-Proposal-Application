import html
import re

import markdown as markdown_lib
from fastapi.templating import Jinja2Templates
from markupsafe import Markup


def strip_duplicate_heading(text: str, title: str) -> str:
    """Every section is rendered under its own title (a heading the template
    already draws), but Claude occasionally opens a section's content with
    that exact title again as its own Markdown heading (a system-prompt
    instruction now asks it not to - see proposal_generation.py - but isn't
    airtight, and this also cleans up proposals generated before that
    instruction existed). Strips only a heading line at the very start of
    the content that matches the title - never touches a heading anywhere
    else in the body, so a genuine subsection heading later on is untouched.
    """
    pattern = rf"^\s*#{{1,6}}\s+{re.escape(title.strip())}\s*\n+"
    return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)


def render_markdown(text: str) -> Markup:
    """Converts section content (Markdown, per the system prompt in
    proposal_generation.py) to HTML for display. The source text is HTML-escaped
    *before* being handed to the Markdown parser - Markdown's own syntax
    (`**`, `#`, `-`, etc.) doesn't use `<`/`>`/`&`, so this only neutralizes
    literal HTML/script-like content that might appear in generated text
    (e.g. echoed from an intake field) into inert text, rather than letting
    it pass through as real markup. Every section-content field in every
    template must go through this filter, never be rendered raw.
    """
    escaped = html.escape(text)
    return Markup(markdown_lib.markdown(escaped, extensions=["nl2br"]))


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["markdown"] = render_markdown
templates.env.filters["strip_dup_heading"] = strip_duplicate_heading


def render_email(template_name: str, **context) -> str:
    """Renders an email template (app/templates/emails/*.html) to an HTML
    string. Plain `.render()`, not `TemplateResponse` - there's no `Request`
    object in an email-sending code path, and these templates never need
    Starlette's request-bound globals (url_for etc.).
    """
    return templates.env.get_template(f"emails/{template_name}").render(**context)
