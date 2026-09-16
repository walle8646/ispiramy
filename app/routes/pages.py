"""Pagine statiche informative/legali (Chi Siamo, FAQ, Contatti, Privacy, Termini)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return request.app.state.templates.TemplateResponse("about.html", {"request": request})


@router.get("/come-funziona", response_class=HTMLResponse)
async def come_funziona(request: Request):
    return request.app.state.templates.TemplateResponse("come_funziona.html", {"request": request})


@router.get("/faq", response_class=HTMLResponse)
async def faq(request: Request):
    return request.app.state.templates.TemplateResponse("faq.html", {"request": request})


@router.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    return request.app.state.templates.TemplateResponse("contact.html", {"request": request})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return request.app.state.templates.TemplateResponse("privacy.html", {"request": request})


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return request.app.state.templates.TemplateResponse("terms.html", {"request": request})
