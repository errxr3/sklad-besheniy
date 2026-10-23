"""Удаление сотрудника: вход закрыт, в списке не виден, история остаётся (2026-10-23)."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Movement, User
from app.services.units import create_printed_units
from app.statuses import ROLE_ADMIN, ROLE_ASSEMBLER
from tests.helpers import get_csrf, login, make_article, make_user, site_by_name


def _delete(client: TestClient, user_id: int) -> str:
    token = get_csrf(client, "/admin/employees")
    response = client.post(
        "/admin/employees/delete", data={"csrf_token": token, "user_id": str(user_id)}, follow_redirects=True
    )
    assert response.status_code == 200
    return response.text


def test_deleted_assembler_keeps_history(client: TestClient, db: Session) -> None:
    admin = make_user(db, name="Админ", role=ROLE_ADMIN)
    worker = make_user(db, name="Пётр Уволенный", role=ROLE_ASSEMBLER)
    article = make_article(db, name="Zerkalo_60x160")
    site = site_by_name(db)
    units = create_printed_units(
        db, article_id=article.id, qty=2, site_id=site.id, assembler_id=worker.id, actor_id=admin.id
    )
    db.commit()

    login(client, admin)
    html = _delete(client, worker.id)
    assert "удалён" in html
    assert "Пётр Уволенный" not in html.split("Список", 1)[1]  # из списка пропал

    db.expire_all()
    gone = db.get(User, worker.id)
    assert gone is not None and gone.deleted_at is not None and not gone.active
    assert all(db.get(type(u), u.id).assembler_id == worker.id for u in units)  # зеркала за ним
    assert db.scalars(select(Movement).where(Movement.user_id == admin.id)).first() is not None

    client.cookies.clear()
    login_page = client.get("/login").text
    assert "Пётр Уволенный" not in login_page


def test_cannot_delete_self_or_service(client: TestClient, db: Session) -> None:
    admin = make_user(db, name="Админ", role=ROLE_ADMIN)
    service = db.scalar(select(User).where(User.is_service.is_(True)))
    login(client, admin)
    assert "Себя удалить нельзя" in _delete(client, admin.id)
    assert service is not None
    assert "Служебного сотрудника удалить нельзя" in _delete(client, service.id)
    db.expire_all()
    assert db.get(User, admin.id).deleted_at is None
