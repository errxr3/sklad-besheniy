"""Настройки администратора: сотрудники, площадки, справочник, параметры (SPEC §4)."""

import logging
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.importer import import_top
from app.menu import ADMIN_SECTION_BY_KEY
from app.models import Article, Site, User
from app.seed import INVENTORY_SERVICE_NAME, RETURNS_SERVICE_NAME, SERVICE_ASSEMBLER_NAME
from app.services.admin import (
    AdminError,
    create_site,
    create_user,
    delete_user,
    reset_pin,
    set_article_active,
    set_subgroup_color,
    update_site,
    update_user,
)
from app.settings_store import get_flag, get_int, set_value
from app.statuses import ALL_ROLES
from app.web import GuardedRoute, page

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", route_class=GuardedRoute)


def _redirect(url: str, message: str = "", err: str = "") -> RedirectResponse:
    """Переход после POST с сообщением в адресе."""
    params: dict[str, str] = {}
    if message:
        params["ok"] = message
    if err:
        params["err"] = err
    query = ("?" + urlencode(params)) if params else ""
    return RedirectResponse(f"{url}{query}", status_code=303)


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request) -> Response:
    """Раздел «Администратор»: настройки и редкие операции."""
    return page(request, "admin/index.html", {"section": ADMIN_SECTION_BY_KEY["admin"]})


@router.get("/section/{key}", response_class=HTMLResponse)
def admin_section(request: Request, key: str) -> Response:
    """Раздел кабинета: действия одной роли плитками."""
    section = ADMIN_SECTION_BY_KEY.get(key)
    if section is None:
        return page(request, "error.html", {"message": "Раздел не найден", "status_code": 404})
    return page(request, "admin/index.html", {"section": section})


# Зачем нужны служебные записи: у каждого зеркала должен быть «сборщик», а эти зеркала никто не собирал
SERVICE_HINTS = {
    SERVICE_ASSEMBLER_NAME: "сборщик для зеркал из ввода остатков на запуске",
    RETURNS_SERVICE_NAME: "сборщик для возвратов без нашей этикетки",
    INVENTORY_SERVICE_NAME: "сборщик для лишних зеркал, найденных на инвентаризации",
}


@router.get("/employees", response_class=HTMLResponse)
def employees(request: Request, db: Session = Depends(get_db), ok: str = "", err: str = "") -> Response:
    """Список сотрудников."""
    users = db.scalars(
        select(User).where(User.deleted_at.is_(None)).order_by(User.is_service, User.active.desc(), User.name)
    ).all()
    sites = db.scalars(select(Site).order_by(Site.sort_order, Site.name)).all()
    return page(
        request,
        "admin/employees.html",
        {
            "users": users,
            "sites": sites,
            "roles": ALL_ROLES,
            "ok": ok,
            "err": err,
            "SERVICE_HINTS": SERVICE_HINTS,
        },
    )


@router.post("/employees/create")
def employees_create(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    login: str = Form(""),
    role: str = Form(...),
    password: str = Form(""),
    home_site_id: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Создать сотрудника."""
    try:
        user = create_user(
            db,
            actor_id=request.state.user.id,
            first_name=first_name,
            last_name=last_name,
            login=login,
            role=role,
            pin=password,
            home_site_id=int(home_site_id) if home_site_id else None,
        )
    except (AdminError, ValueError) as exc:
        return _redirect("/admin/employees", err=str(exc))
    return _redirect("/admin/employees", message=f"Сотрудник «{user.name}» создан, логин: {user.login}")


@router.post("/employees/update")
def employees_update(
    request: Request,
    user_id: int = Form(...),
    first_name: str = Form(""),
    last_name: str = Form(""),
    login: str = Form(""),
    role: str = Form(...),
    active: str = Form(""),
    home_site_id: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Изменить сотрудника."""
    try:
        update_user(
            db,
            actor_id=request.state.user.id,
            user_id=user_id,
            first_name=first_name,
            last_name=last_name,
            login=login,
            role=role,
            active=active == "1",
            home_site_id=int(home_site_id) if home_site_id else None,
        )
    except (AdminError, ValueError) as exc:
        return _redirect("/admin/employees", err=str(exc))
    return _redirect("/admin/employees", message="Сохранено")


@router.post("/employees/delete")
def employees_delete(request: Request, user_id: int = Form(...), db: Session = Depends(get_db)) -> Response:
    """Удалить сотрудника, история остаётся."""
    try:
        user = delete_user(db, actor_id=request.state.user.id, user_id=user_id)
    except AdminError as exc:
        return _redirect("/admin/employees", err=str(exc))
    message = f"Сотрудник «{user.name}» удалён, его история в журнале осталась"
    return _redirect("/admin/employees", message=message)


@router.post("/employees/reset-pin")
def employees_reset_pin(
    request: Request,
    user_id: int = Form(...),
    password: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Задать сотруднику новый пароль."""
    try:
        reset_pin(db, actor_id=request.state.user.id, user_id=user_id, pin=password)
    except AdminError as exc:
        return _redirect("/admin/employees", err=str(exc))
    return _redirect("/admin/employees", message="Пароль изменён")


@router.get("/sites", response_class=HTMLResponse)
def sites_page(request: Request, db: Session = Depends(get_db), ok: str = "", err: str = "") -> Response:
    """Площадки."""
    sites = db.scalars(select(Site).order_by(Site.sort_order, Site.name)).all()
    return page(request, "admin/sites.html", {"sites": sites, "ok": ok, "err": err})


@router.post("/sites/create")
def sites_create(request: Request, name: str = Form(...), db: Session = Depends(get_db)) -> Response:
    """Добавить площадку."""
    try:
        create_site(db, actor_id=request.state.user.id, name=name)
    except AdminError as exc:
        return _redirect("/admin/sites", err=str(exc))
    return _redirect("/admin/sites", message=f"Площадка «{name}» добавлена")


@router.post("/sites/update")
def sites_update(
    request: Request,
    site_id: int = Form(...),
    name: str = Form(""),
    active: str = Form(""),
    sort_order: int = Form(0),
    geo_lat: str = Form(""),
    geo_lon: str = Form(""),
    geo_radius_m: int = Form(300),
    geo_check_enabled: str = Form(""),
    labeling_done: str = Form(""),
    article_prefixes: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    """Изменить площадку."""
    try:
        update_site(
            db,
            actor_id=request.state.user.id,
            site_id=site_id,
            name=name or None,
            active=active == "1",
            sort_order=sort_order,
            geo_lat=float(geo_lat) if geo_lat.strip() else None,
            geo_lon=float(geo_lon) if geo_lon.strip() else None,
            geo_radius_m=geo_radius_m,
            geo_check_enabled=geo_check_enabled == "1",
            article_prefixes=article_prefixes,
            labeling_done=labeling_done == "1",
        )
    except (AdminError, ValueError) as exc:
        return _redirect("/admin/sites", err=str(exc))
    return _redirect("/admin/sites", message="Сохранено")


@router.get("/articles", response_class=HTMLResponse)
def articles_page(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    ok: str = "",
    err: str = "",
) -> Response:
    """Справочник артикулов и импорт TOP.xlsx."""
    query = select(Article).order_by(Article.sort_order)
    if q.strip():
        query = query.where(Article.name.ilike(f"%{q.strip()}%"))
    articles = db.scalars(query.limit(400)).all()
    total = db.scalar(select(func.count()).select_from(Article).where(Article.kind == "article")) or 0
    subgroups = db.execute(
        select(Article.group, Article.subgroup, func.min(Article.color_hex), func.count())
        .where(Article.kind == "article")
        .group_by(Article.group, Article.subgroup)
        .order_by(func.min(Article.sort_order))
    ).all()
    return page(
        request,
        "admin/articles.html",
        {
            "articles": articles,
            "total": total,
            "subgroups": subgroups,
            "q": q,
            "ok": ok,
            "err": err,
        },
    )


@router.post("/articles/import")
async def articles_import(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    """Загрузить TOP.xlsx и обновить справочник."""
    content = await file.read()
    if not content:
        return _redirect("/admin/articles", err="Файл пустой")
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    try:
        report = import_top(db, temp_path)
    except Exception as exc:
        logger.exception("Импорт справочника не удался")
        return _redirect("/admin/articles", err=f"Файл не прочитан: {exc}")
    finally:
        temp_path.unlink(missing_ok=True)
    return _redirect("/admin/articles", message=report.as_text())


@router.post("/articles/toggle")
def articles_toggle(
    request: Request,
    article_id: int = Form(...),
    active: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Включить или выключить артикул."""
    try:
        set_article_active(db, actor_id=request.state.user.id, article_id=article_id, active=active == "1")
    except AdminError as exc:
        return _redirect("/admin/articles", err=str(exc))
    return _redirect("/admin/articles", message="Сохранено")


@router.post("/articles/color")
def articles_color(
    request: Request,
    group: str = Form(...),
    subgroup: str = Form(""),
    color_hex: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Поменять цвет подгруппы."""
    try:
        count = set_subgroup_color(
            db,
            actor_id=request.state.user.id,
            group=group,
            subgroup=subgroup or None,
            color_hex=color_hex,
        )
    except AdminError as exc:
        return _redirect("/admin/articles", err=str(exc))
    return _redirect("/admin/articles", message=f"Цвет изменён у {count} строк")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), ok: str = "", err: str = "") -> Response:
    """Настройки этикетки и этапов внедрения."""
    return page(
        request,
        "admin/settings.html",
        {
            "label_width_mm": get_int(db, "label_width_mm"),
            "label_height_mm": get_int(db, "label_height_mm"),
            "require_mp_barcode": get_flag(db, "require_mp_barcode"),
            "verify_order": get_flag(db, "verify_order"),
            "ship_cancel_minutes": get_int(db, "ship_cancel_minutes"),
            "ok": ok,
            "err": err,
        },
    )


@router.post("/settings")
def settings_save(
    request: Request,
    label_width_mm: int = Form(58),
    label_height_mm: int = Form(40),
    require_mp_barcode: str = Form(""),
    verify_order: str = Form(""),
    ship_cancel_minutes: int = Form(10),
    db: Session = Depends(get_db),
) -> Response:
    """Сохранить настройки."""
    if not (20 <= label_width_mm <= 210) or not (20 <= label_height_mm <= 297):
        return _redirect("/admin/settings", err="Размер этикетки вне разумных пределов")
    set_value(db, "label_width_mm", str(label_width_mm))
    set_value(db, "label_height_mm", str(label_height_mm))
    set_value(db, "require_mp_barcode", "1" if require_mp_barcode == "1" else "0")
    set_value(db, "verify_order", "1" if verify_order == "1" else "0")
    set_value(db, "ship_cancel_minutes", str(max(1, ship_cancel_minutes)))
    return _redirect("/admin/settings", message="Настройки сохранены")
