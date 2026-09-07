import logging

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

_TIMEOUT_MS = 30_000


class PdfRenderError(Exception):
    """Raised when headless-browser PDF rendering fails."""


def render_pdf(url: str) -> bytes:
    """Renders `url` to PDF bytes via headless Chromium (Playwright).

    Deliberately hits the same live route/template that serves the in-app
    preview (spec section 2: "one template, not two parallel renderers") -
    this isn't a second HTML-to-PDF pipeline, it's a real browser printing
    the exact page a viewer would see.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
                return page.pdf(print_background=True)
            finally:
                browser.close()
    except Exception as exc:
        logger.exception("PDF rendering failed for url=%s", url)
        raise PdfRenderError(str(exc)) from None
