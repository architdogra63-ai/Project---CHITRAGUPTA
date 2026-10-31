"""Frontend router — serves static HTML with dynamically injected bridge script."""

from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["frontend"])

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


def _serve_injected_html(filename: str) -> HTMLResponse:
    """Read HTML file from frontend directory and inject the client bridge script."""
    file_path = FRONTEND_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Frontend file {filename} not found")

    content = file_path.read_text(encoding="utf-8")
    injection = '<script src="/static/bridge.js"></script>\n</body>'
    if "</body>" in content:
        content = content.replace("</body>", injection, 1)
    else:
        content += injection

    return HTMLResponse(content=content)


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index_root():
    """Serve index.html at root path."""
    return _serve_injected_html("index.html")


@router.api_route("/index.html", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index_page():
    """Serve index.html."""
    return _serve_injected_html("index.html")


@router.api_route("/dashboard.html", methods=["GET", "HEAD"], response_class=HTMLResponse)
def dashboard_page():
    """Serve dashboard.html."""
    return _serve_injected_html("dashboard.html")
