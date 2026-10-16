"""Admin routes: Dashboard, pricing management, and CSV / Master ZIP downloads."""
import csv
import io
import os
import re
import zipfile
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..i18n import t
from ..models import Deck, Order
from ..pricing import format_cents_eur, get_pricing_settings, update_pricing_settings

router = APIRouter(prefix="/admin")

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

def sanitize_slug(name: str) -> str:
    """Creates a filesystem-safe letters-only or alphanumeric slug."""
    clean = re.sub(r"[^A-Za-z0-9]", "", name)
    return clean or "Deck"

def is_admin_authenticated(admin_session: Optional[str] = Cookie(None)) -> bool:
    return admin_session == settings.admin_password

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None, lang: str = "en"):
    return templates.TemplateResponse(request, "admin/login.html", {
        "error": error,
        "lang": lang,
        "t": lambda key, *args: t(key, lang, *args),
    })

@router.post("/login")
def login_submit(password: str = Form(...), lang: str = Form("en")):
    if password == settings.admin_password:
        response = RedirectResponse(url=f"/admin?lang={lang}", status_code=303)
        response.set_cookie(key="admin_session", value=settings.admin_password, httponly=True, samesite="lax")
        return response
    return RedirectResponse(url=f"/admin/login?error=invalid&lang={lang}", status_code=303)

@router.post("/logout")
def logout_submit():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(key="admin_session")
    return response

@router.get("", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    admin_session: Optional[str] = Cookie(None),
    lang: str = "en",
    db: Session = Depends(get_db)
):
    if not is_admin_authenticated(admin_session):
        return RedirectResponse(url=f"/admin/login?lang={lang}", status_code=303)
        
    rates = get_pricing_settings(db)
    orders = db.query(Order).order_by(Order.created_at.desc()).all()
    
    total_orders = len(orders)
    total_decks = sum(o.total_lists for o in orders)
    total_cards = sum(o.total_cards for o in orders)
    total_revenue_cents = sum(o.total_price_cents for o in orders)
    
    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "lang": lang,
        "t": lambda key, *args: t(key, lang, *args),
        "rates": rates,
        "orders": orders,
        "total_orders": total_orders,
        "total_decks": total_decks,
        "total_cards": total_cards,
        "total_revenue_cents": total_revenue_cents,
        "format_cents": format_cents_eur,
    })

@router.post("/pricing")
def update_pricing(
    price_per_card_cents: int = Form(...),
    price_per_list_cents: int = Form(...),
    order_fee_cents: int = Form(...),
    admin_session: Optional[str] = Cookie(None),
    lang: str = Form("en"),
    db: Session = Depends(get_db)
):
    if not is_admin_authenticated(admin_session):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    update_pricing_settings(db, price_per_card_cents, price_per_list_cents, order_fee_cents)
    return RedirectResponse(url=f"/admin?lang={lang}&saved=1", status_code=303)

@router.post("/order/{order_id}/toggle-paid")
def toggle_order_paid(
    order_id: str,
    admin_session: Optional[str] = Cookie(None),
    lang: str = Form("en"),
    db: Session = Depends(get_db)
):
    if not is_admin_authenticated(admin_session):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if order:
        order.status = "PAID" if order.status != "PAID" else "SUBMITTED"
        db.commit()
    return RedirectResponse(url=f"/admin?lang={lang}", status_code=303)

@router.post("/order/{order_id}/delete")
def delete_order(
    order_id: str,
    admin_session: Optional[str] = Cookie(None),
    lang: str = Form("en"),
    db: Session = Depends(get_db)
):
    if not is_admin_authenticated(admin_session):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if order:
        db.delete(order)
        db.commit()
@router.post("/cycle/new")
def start_new_order_cycle(
    admin_session: Optional[str] = Cookie(None),
    lang: str = Form("en"),
    db: Session = Depends(get_db)
):
    if not is_admin_authenticated(admin_session):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    db.query(Deck).delete()
    db.query(Order).delete()
    db.commit()
    return RedirectResponse(url=f"/admin?lang={lang}&cycle_reset=1", status_code=303)

@router.get("/export/csv")
def export_summary_csv(
    admin_session: Optional[str] = Cookie(None),
    db: Session = Depends(get_db)
):
    """Exports cost breakdown summary CSV."""
    if not is_admin_authenticated(admin_session):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    orders = db.query(Order).order_by(Order.created_at.asc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # 1. Person Summary
    writer.writerow(["=== Person Summary ==="])
    writer.writerow([
        "Person", "Decks Count", "Total Cards",
        "Price per Card (€)", "Price per Deck (€)", "Order Fee (€)",
        "Total (€)", "Status", "Reference Code", "Date"
    ])
    
    for o in orders:
        writer.writerow([
            o.submitter_name,
            o.total_lists,
            o.total_cards,
            f"{o.price_per_card_cents / 100.0:.2f}",
            f"{o.price_per_list_cents / 100.0:.2f}",
            f"{o.order_fee_cents / 100.0:.2f}",
            f"{o.total_price_cents / 100.0:.2f}",
            o.status,
            o.reference_code,
            o.created_at.strftime("%Y-%m-%d %H:%M"),
        ])
        
    writer.writerow([])
    # 2. Detailed Decks Breakdown
    writer.writerow(["=== Decks Breakdown ==="])
    writer.writerow(["Person", "Deck Name", "Cards", "Language", "Custom Back", "Moxfield URL"])
    
    for o in orders:
        for d in o.decks:
            writer.writerow([
                o.submitter_name,
                d.name,
                d.card_count,
                d.preferred_language,
                "Yes" if d.custom_back_data else "No",
                d.moxfield_url or "",
            ])
            
    output.seek(0)
    filename = f"proxy_order_summary_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@router.get("/export/zip")
def export_master_zip(
    admin_session: Optional[str] = Cookie(None),
    db: Session = Depends(get_db)
):
    """
    Exports complete ready-to-print ZIP with per-deck directories:
    decks/{Person}{Ordinal}_{DeckName}/
      ├── decklist.txt
      └── back.jpg (if uploaded)
    """
    if not is_admin_authenticated(admin_session):
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    orders = db.query(Order).order_by(Order.submitter_name.asc(), Order.created_at.asc()).all()
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Summary CSV
        summary_csv = io.StringIO()
        writer = csv.writer(summary_csv)
        writer.writerow(["Person", "Deck Name", "Cards", "Language", "Total (€)", "Reference Code"])
        
        taken_folders = set()
        for o in orders:
            person_slug = sanitize_slug(o.submitter_name)
            for d in o.decks:
                deck_slug = sanitize_slug(d.name)
                folder_name = f"decks/{person_slug}{d.ordinal}_{deck_slug}"
                if folder_name in taken_folders:
                    folder_name = f"decks/{person_slug}_{o.id}_{d.ordinal}_{deck_slug}"
                taken_folders.add(folder_name)
                
                # Write decklist.txt
                zf.writestr(f"{folder_name}/decklist.txt", d.converted_text)
                
                # Write custom back image if present
                if d.custom_back_data:
                    ext = ".jpg"
                    if d.custom_back_content_type and "png" in d.custom_back_content_type:
                        ext = ".png"
                    zf.writestr(f"{folder_name}/back{ext}", d.custom_back_data)
                    
                writer.writerow([
                    o.submitter_name, d.name, d.card_count, d.preferred_language,
                    f"{o.total_price_cents / 100.0:.2f}", o.reference_code
                ])
                
        zf.writestr("summary_costs.csv", summary_csv.getvalue().encode("utf-8"))
        
    zip_buffer.seek(0)
    filename = f"master_print_run_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
