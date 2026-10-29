"""Партии BLAGO OTGRUZKI и решение «что едет» по остаткам (решения владельца 2026-10-28/29).

BLAGO OTGRUZKI отдаёт складу две вещи:

* **заказы дня** (`receive_demand`): склад решает, какие из них едут. Остаток делится так:
  сначала старые дни, внутри дня — СГТ > WB > СДЭК > Ozon, внутри канала — по времени заказа.
  Заказ из нескольких зеркал едет целиком или не едет. Если на складе мало, а артикул
  в работе у сборщиков, — заказ «под вопросом», и начальнику производства задаётся вопрос
  (`ShipQuestion`): доделают к сроку или нет;
* **партии** (`receive_batch`): этикетки, которые программа скачала, — одна машина Ozon,
  поставка WB и т. п. По ним грузчик сканирует при погрузке (`app.services.shipping`).

Резерв не хранится, а пересчитывается каждый раз: сначала заказы, на которые уже скачаны
этикетки (их не отбираем), потом остальные по правилу выше.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.articles import normalize_article
from app.models import Article, ShipBatch, ShipCode, ShipDemand, ShipItem, ShipQuestion, Site, User
from app.services.stock import active_sites, stock_by_article_site
from app.statuses import (
    CHANNEL_PRIORITY,
    CHANNELS,
    MARKETS,
    OZON,
    OZON_TRUCKS,
    SHIP_LOADED,
    SHIP_REMOVED,
    SHIP_WAIT,
)
from app.timeutil import MOSCOW, now_utc, to_moscow, today_msk

GO, ASK, NO = "go", "ask", "no"
STATE_ORDER = {SHIP_WAIT: 0, SHIP_REMOVED: 1, SHIP_LOADED: 2}
MAX_CODES = 8


class BatchError(Exception):
    """Партия не принята — текст уходит в BLAGO OTGRUZKI."""


# ---------- артикулы и производства ----------

_LATIN = str.maketrans("хХаАоОсСеЕуУрРкКмМтТ", "xXaAoOcCeEyYpPkKmMtT")


def canonical_article(name: str) -> str:
    """Артикул к одному виду, как в BLAGO OTGRUZKI: русские буквы-двойники → латиница,
    bp_Zerkalo_50x160 = Zerkalo_50x160_bp, Zerkalo_200x80 = Zerkalo_80x200, регистр не важен."""
    text = " ".join(str(name).split()).translate(_LATIN).replace("Zerkalo.", "Zerkalo_")
    if text.lower().startswith("bp_"):
        text = text[3:] + "_bp"
    match = re.match(r"^(zerkalo)_(\d+)x(\d+)(_.*)?$", text, re.I)
    if match:
        low, high = sorted((int(match.group(2)), int(match.group(3))))
        text = f"Zerkalo_{low}x{high}{match.group(4) or ''}"
    return "_".join(part for part in text.split("_") if part).lower()


class Catalog:
    """Поиск артикула и производства по названию из маркетплейса."""

    def __init__(self, session: Session) -> None:
        articles = session.scalars(select(Article).where(Article.kind == "article")).all()
        self.by_norm = {a.name_norm: a for a in articles if a.name_norm}
        self.by_canon = {canonical_article(a.name): a for a in articles}
        self.sites = active_sites(session)

    def article(self, name: str) -> Article | None:
        return self.by_norm.get(normalize_article(name)) or self.by_canon.get(canonical_article(name))

    def site(self, production: str | None, article_name: str) -> Site | None:
        if production:
            wanted = production.strip().lower()
            for site in self.sites:
                if site.name.lower() == wanted:
                    return site
        makers = [site for site in self.sites if site.prefixes and site.makes(article_name)]
        return makers[0] if len(makers) == 1 else None


def channel_of(market: str, channel: str, truck: str = "") -> tuple[str, str]:
    """Проверить канал и машину: Ozon — Озон1…Озон4, остальные без машины."""
    if market not in MARKETS:
        raise BatchError(f"Неизвестный маркетплейс «{market}»: " + ", ".join(MARKETS))
    if channel not in CHANNELS:
        raise BatchError(f"Неизвестный канал «{channel}»: " + ", ".join(CHANNELS))
    if channel == OZON:
        truck = truck or OZON_TRUCKS[0]
        if truck not in OZON_TRUCKS:
            raise BatchError("Машина Ozon: " + ", ".join(OZON_TRUCKS))
        return channel, truck
    return channel, ""


def batch_title(channel: str, truck: str) -> str:
    return "Озон " + truck.removeprefix("Озон") if truck else channel


def clean_ship_code(raw: object) -> str:
    """Код с этикетки маркетплейса в одном виде: без пробелов, латинские буквы заглавные."""
    return re.sub(r"\s+", "", str(raw or "")).upper()[:80]


# ---------- приём партии ----------


@dataclass
class BatchResult:
    """Ответ BLAGO OTGRUZKI на партию."""

    batch: ShipBatch
    added: int = 0
    updated: int = 0
    kept: list[str] = field(default_factory=list)  # уже в другой партии или погружены — не трогаем
    unknown: list[str] = field(default_factory=list)  # артикула нет в справочнике склада


def receive_batch(
    session: Session,
    *,
    day: date,
    market: str,
    channel: str,
    truck: str = "",
    source: str = "",
    items: list[dict],
) -> BatchResult:
    """Принять партию этикеток. Повторная отправка той же волны дополняет её.

    Этикетка, которая уже погружена, или уже лежит в другой сегодняшней партии и ждёт там,
    остаётся где была. Снятая с отгрузки или из партии прошлого дня — переезжает в эту.
    """
    channel, truck = channel_of(market, channel, truck)
    batch = session.scalar(
        select(ShipBatch).where(ShipBatch.day == day, ShipBatch.channel == channel, ShipBatch.truck == truck)
    )
    if batch is None:
        batch = ShipBatch(day=day, market=market, channel=channel, truck=truck, source=source[:160] or None)
        session.add(batch)
        session.flush()
    else:
        batch.updated_at = now_utc()
        if source:
            batch.source = source[:160]
    catalog = Catalog(session)
    result = BatchResult(batch=batch)
    for raw in items:
        posting = str(raw.get("posting") or "").strip()[:64]
        article_name = str(raw.get("article") or "").strip()[:200]
        if not posting or not article_name:
            continue
        article = catalog.article(article_name)
        if article is None:
            result.unknown.append(f"{posting} {article_name}")
        site = catalog.site(raw.get("production"), article.name if article else article_name)
        item = session.scalar(select(ShipItem).where(ShipItem.market == market, ShipItem.posting == posting))
        if item is not None:
            same = item.batch_id == batch.id
            busy = item.state == SHIP_LOADED or (
                item.state == SHIP_WAIT and not same and item.batch.day >= day
            )
            if busy:
                where = "погружено" if item.state == SHIP_LOADED else item.batch.title
                result.kept.append(f"{posting}: {where}")
                continue
            item.batch_id = batch.id
            item.state = SHIP_WAIT
            item.removed_reason = item.removed_by = item.removed_at = None
            result.updated += 1
        else:
            item = ShipItem(batch_id=batch.id, market=market, posting=posting, state=SHIP_WAIT)
            session.add(item)
            result.added += 1
        item.article_name = article_name
        item.article_id = article.id if article else None
        item.site_id = site.id if site else None
        item.ordered_at = _parse_time(raw.get("ordered_at"))
        session.flush()
        codes = {clean_ship_code(posting)}
        codes.update(clean_ship_code(code) for code in (raw.get("codes") or [])[:MAX_CODES])
        for code in codes - {""}:
            owner = session.get(ShipCode, code)
            if owner is None:
                session.add(ShipCode(code=code, item_id=item.id))
            elif owner.item_id != item.id:
                owner.item_id = item.id
    session.flush()
    allocate(session, day=day)
    return result


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MOSCOW)  # время из отчётов Ozon и WB — московское
    return moment


# ---------- заказы дня и решение по остаткам ----------


def receive_demand(
    session: Session, *, day: date, lines: list[dict], replace_markets: list[str] | None = None
) -> list[ShipDemand]:
    """Принять заказы дня и сразу решить, что едет.

    `replace_markets` — утренний полный список: заказы этих маркетплейсов, которых нет в новом
    списке и на которые ещё не скачаны этикетки, убираются (их отменили или уже отгрузили).
    """
    catalog = Catalog(session)
    seen: dict[tuple[str, str, str], ShipDemand] = {}
    for raw in lines:
        market = str(raw.get("market") or "").strip()
        channel, truck = channel_of(market, str(raw.get("channel") or ""), str(raw.get("truck") or ""))
        posting = str(raw.get("posting") or "").strip()[:64]
        article_name = str(raw.get("article") or "").strip()[:200]
        if not posting or not article_name:
            continue
        key = (market, posting, article_name)
        try:
            qty = max(1, int(raw.get("qty") or 1))
        except (TypeError, ValueError):
            qty = 1
        if key in seen:
            seen[key].qty += qty  # одна строка отчёта на штуку — складываем
            continue
        line = session.scalar(
            select(ShipDemand).where(
                ShipDemand.market == market,
                ShipDemand.posting == posting,
                ShipDemand.article_name == article_name,
            )
        )
        if line is None:
            line = ShipDemand(market=market, posting=posting, article_name=article_name)
            session.add(line)
        seen[key] = line
        article = catalog.article(article_name)
        site = catalog.site(raw.get("production"), article.name if article else article_name)
        line.day = min(line.day, day) if line.day else day
        line.channel, line.truck, line.qty = channel, truck, qty
        line.article_id = article.id if article else None
        line.site_id = site.id if site else None
        line.ordered_at = _parse_time(raw.get("ordered_at")) or line.ordered_at
        line.updated_at = now_utc()
    session.flush()
    if replace_markets:
        printed = set(session.scalars(select(ShipItem.posting)).all())
        for line in session.scalars(select(ShipDemand).where(ShipDemand.market.in_(replace_markets))).all():
            if (line.market, line.posting, line.article_name) not in seen and line.posting not in printed:
                session.delete(line)
        session.flush()
    allocate(session, day=day)
    return list(
        session.scalars(
            select(ShipDemand).where(
                ShipDemand.posting.in_({posting for _, posting, _ in seen} or {""})
            )
        ).all()
    )


def in_work_by_article_site(session: Session, *, day: date) -> dict[tuple[int, int], int]:
    """Сколько ещё не сделано по задачам дня: (артикул, площадка) → штук."""
    from app.services.shipment_report import tasks_done_by_article_site
    from app.services.tasks import plan_sum_by_site

    plans = plan_sum_by_site(session, day=day)
    done = tasks_done_by_article_site(session, day=day)
    return {key: max(0, plan - done.get(key, 0)) for key, plan in plans.items() if plan > done.get(key, 0)}


def _priority(line: ShipDemand) -> tuple[date, int, int, float, str]:
    """Старые дни раньше; внутри дня СГТ > WB > СДЭК > Ozon; внутри канала — по времени заказа."""
    moment = line.ordered_at
    order_day = to_moscow(moment).date() if moment else line.day
    rank = CHANNEL_PRIORITY.index(line.channel) if line.channel in CHANNEL_PRIORITY else len(CHANNEL_PRIORITY)
    return (order_day, rank, 0 if moment else 1, moment.timestamp() if moment else 0.0, line.posting)


def allocate(session: Session, *, day: date | None = None) -> None:
    """Пересчитать, что едет: решения заказов и вопросы начальникам."""
    day = day or today_msk()
    items = {
        (item.market, item.posting): item for item in session.scalars(select(ShipItem)).all()
    }
    lines = [
        line
        for line in session.scalars(select(ShipDemand)).all()
        if items.get((line.market, line.posting)) is None
        or items[(line.market, line.posting)].state == SHIP_WAIT
    ]
    for line in session.scalars(select(ShipDemand)).all():
        item = items.get((line.market, line.posting))
        if item is not None and item.state == SHIP_LOADED:
            line.decision, line.reason = GO, "погружено"
        elif item is not None and item.state == SHIP_REMOVED:
            line.decision, line.reason = NO, f"снято с отгрузки: {item.removed_reason or ''}".strip(": ")

    postings: dict[tuple[str, str], list[ShipDemand]] = defaultdict(list)
    for line in lines:
        postings[(line.market, line.posting)].append(line)

    # Этикетки уже скачаны (заказ в партии и ждёт) — эти зеркала не отбираем
    def order(key: tuple[str, str]) -> tuple:
        committed = key in items and items[key].state == SHIP_WAIT
        return (0 if committed else 1, *_priority(min(postings[key], key=_priority)))

    free = dict(stock_by_article_site(session))
    work = in_work_by_article_site(session, day=day)
    questions = {
        (q.site_id, q.article_id): q
        for q in session.scalars(select(ShipQuestion).where(ShipQuestion.day == day)).all()
    }
    promised = {key: q.promised for key, q in questions.items() if q.answer == "yes"}
    refused = {key for key, q in questions.items() if q.answer == "no"}
    asked: dict[tuple[int, int], int] = defaultdict(int)  # нужно взять из «в работе», ответа нет
    covered: dict[tuple[int, int], int] = defaultdict(int)  # взято из обещанного начальником
    need: dict[tuple[int, int], int] = defaultdict(int)
    stock_seen: dict[tuple[int, int], int] = {}
    where: dict[tuple[int, int], set[str]] = defaultdict(set)

    for key in sorted(postings, key=order):
        group = postings[key]
        committed = key in items and items[key].state == SHIP_WAIT
        wanted: dict[tuple[int, int], int] = defaultdict(int)
        broken = ""
        for line in group:
            if line.article_id is None:
                broken = f"артикула «{line.article_name}» нет в справочнике склада"
            elif line.site_id is None:
                broken = f"не понятно производство для «{line.article_name}»"
            else:
                wanted[(line.site_id, line.article_id)] += line.qty
        if broken:
            _decide(group, NO, broken)
            continue
        for stock_key, qty in wanted.items():
            ak = (stock_key[1], stock_key[0])
            need[stock_key] += qty
            stock_seen.setdefault(stock_key, free.get(ak, 0))
            where[stock_key].add(batch_title(group[0].channel, group[0].truck))
        if committed:
            for stock_key, qty in wanted.items():
                ak = (stock_key[1], stock_key[0])
                free[ak] = free.get(ak, 0) - qty
            _decide(group, GO, "этикетки скачаны")
            continue
        shorts = {k: max(0, q - max(0, free.get((k[1], k[0]), 0))) for k, q in wanted.items()}
        if not any(shorts.values()):
            for stock_key, qty in wanted.items():
                ak = (stock_key[1], stock_key[0])
                free[ak] = free.get(ak, 0) - qty
            _decide(group, GO, "есть на складе")
            continue
        # Не хватает: смотрим, обещал ли начальник и есть ли в работе
        verdict, reason = GO, "начальник обещал доделать"
        for stock_key, short in shorts.items():
            if not short:
                continue
            if stock_key in refused:
                verdict, reason = NO, "начальник ответил: не успеем"
                break
            if promised.get(stock_key, 0) >= short:
                continue
            in_work = work.get((stock_key[1], stock_key[0]), 0)
            left_in_work = in_work - asked[stock_key] - promised.get(stock_key, 0)
            if left_in_work >= short:
                verdict, reason = ASK, "на складе мало, артикул в работе — вопрос начальнику"
            else:
                verdict, reason = NO, "не хватает на складе и в работе"
                break
        if verdict == GO:
            for stock_key, short in shorts.items():
                promised[stock_key] = promised.get(stock_key, 0) - short
                covered[stock_key] += short
        if verdict in (GO, ASK):
            for stock_key, qty in wanted.items():
                ak = (stock_key[1], stock_key[0])
                take = min(qty, max(0, free.get(ak, 0)))
                free[ak] = free.get(ak, 0) - take
                if verdict == ASK:
                    asked[stock_key] += qty - take
        _decide(group, verdict, reason)

    _sync_questions(session, day=day, questions=questions, asked=asked, covered=covered,
                    need=need, stock=stock_seen, work=work, where=where)
    session.flush()


def _decide(group: list[ShipDemand], decision: str, reason: str) -> None:
    for line in group:
        line.decision, line.reason = decision, reason[:200]


def _sync_questions(
    session: Session,
    *,
    day: date,
    questions: dict[tuple[int, int], ShipQuestion],
    asked: dict[tuple[int, int], int],
    covered: dict[tuple[int, int], int],
    need: dict[tuple[int, int], int],
    stock: dict[tuple[int, int], int],
    work: dict[tuple[int, int], int],
    where: dict[tuple[int, int], set[str]],
) -> None:
    """Вопросы начальнику: открыть новые, обновить цифры, закрыть ненужные."""
    for key, short in asked.items():
        if short <= 0:
            continue
        question = questions.get(key)
        if question is None:
            question = ShipQuestion(day=day, site_id=key[0], article_id=key[1], answer="open")
            session.add(question)
            questions[key] = question
        elif question.answer == "yes":
            question.answer = "open"  # заказов прибавилось — обещанного уже мало
    for key, question in questions.items():
        # Сколько всего нужно доделать: уже обещанное и то, на что ответа ещё нет
        question.short = covered.get(key, 0) + asked.get(key, 0)
        if question.answer == "open" and not asked.get(key, 0) and question.promised:
            question.answer = "yes"
        question.need = need.get(key, 0)
        question.stock = max(0, stock.get(key, 0))
        question.in_work = work.get((key[1], key[0]), 0)
        question.channels = ", ".join(sorted(where.get(key, set())))[:120] or None
        question.updated_at = now_utc()


def open_questions(session: Session, *, site_ids: list[int] | None = None) -> list[ShipQuestion]:
    """Вопросы без ответа на сегодня (для начальника своей площадки)."""
    query = select(ShipQuestion).where(
        ShipQuestion.day == today_msk(), ShipQuestion.answer == "open", ShipQuestion.short > 0
    )
    if site_ids is not None:
        query = query.where(ShipQuestion.site_id.in_(site_ids or [0]))
    return list(session.scalars(query.order_by(ShipQuestion.created_at)).all())


def count_open_questions(session: Session, *, site_ids: list[int] | None = None) -> int:
    query = select(func.count()).select_from(ShipQuestion).where(
        ShipQuestion.day == today_msk(), ShipQuestion.answer == "open", ShipQuestion.short > 0
    )
    if site_ids is not None:
        query = query.where(ShipQuestion.site_id.in_(site_ids or [0]))
    return int(session.scalar(query) or 0)


def answer_question(
    session: Session,
    *,
    question_id: int,
    actor: User,
    yes: bool,
    assembler_id: int | None = None,
    qty: int = 0,
    due_at: datetime | None = None,
) -> ShipQuestion:
    """Ответ начальника. «Сделаем»: срочная задача сборщику, заказы едут. «Нет» — не едут."""
    from app.services.tasks import check_task_site, set_urgent

    question = session.get(ShipQuestion, question_id)
    if question is None:
        raise BatchError("Вопрос не найден")
    check_task_site(session, actor, question.site_id)
    if yes:
        if not assembler_id:
            raise BatchError("Выберите сборщика")
        if due_at is None:
            raise BatchError("Укажите, к какому времени")
        qty = qty or max(0, question.short - question.promised)
        if qty < 1:
            raise BatchError("Укажите, сколько сделать")
        set_urgent(
            session, actor=actor, day=question.day, site_id=question.site_id, user_id=assembler_id,
            article_id=question.article_id, qty=qty, due_at=due_at,
            note=f"на {question.channels}" if question.channels else "на отгрузку",
        )
        question.promised += qty
        question.due_at = due_at
        question.answer = "yes"
    else:
        question.answer = "no"
    question.answered_by = actor.id
    question.answered_at = now_utc()
    session.flush()
    allocate(session, day=question.day)
    return question


# ---------- партии на экране начальника ----------


@dataclass
class BatchSummary:
    """Итог партии за день."""

    batch: ShipBatch
    total: int = 0
    loaded: int = 0
    removed: int = 0
    moved_out: int = 0  # погружено в другую машину
    items: list[ShipItem] = field(default_factory=list)

    @property
    def left(self) -> int:
        return self.total - self.loaded - self.removed


def day_batches(session: Session, *, day: date, site_ids: list[int] | None = None) -> list[BatchSummary]:
    """Партии дня с их этикетками (по своим площадкам у начальника с площадкой)."""
    batches = session.scalars(select(ShipBatch).where(ShipBatch.day == day).order_by(ShipBatch.id)).all()
    result = []
    for batch in sorted(batches, key=lambda b: (b.channel != OZON, b.truck, b.channel)):
        query = select(ShipItem).where(ShipItem.batch_id == batch.id)
        if site_ids is not None:
            query = query.where(ShipItem.site_id.in_(site_ids or [0]))
        items = sorted(
            session.scalars(query).all(),
            key=lambda i: (STATE_ORDER[i.state], i.article_name, i.posting),
        )
        if not items:
            continue
        summary = BatchSummary(batch=batch, items=items, total=len(items))
        summary.loaded = sum(1 for i in items if i.state == SHIP_LOADED)
        summary.removed = sum(1 for i in items if i.state == SHIP_REMOVED)
        summary.moved_out = sum(1 for i in items if i.moved_note)
        result.append(summary)
    return result


def remove_items(
    session: Session, *, actor: User, item_ids: list[int], reason: str, site_ids: list[int] | None = None
) -> int:
    """Снять этикетки с отгрузки: брак, нет на складе, потеряно. Погруженные не трогаем."""
    reason = reason.strip()[:200]
    if not reason:
        raise BatchError("Укажите причину")
    removed = 0
    for item_id in item_ids:
        item = session.get(ShipItem, item_id)
        if item is None or item.state != SHIP_WAIT:
            continue
        if site_ids is not None and item.site_id not in site_ids:
            continue
        item.state = SHIP_REMOVED
        item.removed_reason = reason
        item.removed_by = actor.id
        item.removed_at = now_utc()
        removed += 1
    session.flush()
    if removed:
        allocate(session)
    return removed


def return_items(session: Session, *, item_ids: list[int], site_ids: list[int] | None = None) -> int:
    """Вернуть снятое обратно в отгрузку (сняли по ошибке)."""
    back = 0
    for item_id in item_ids:
        item = session.get(ShipItem, item_id)
        if item is None or item.state != SHIP_REMOVED:
            continue
        if site_ids is not None and item.site_id not in site_ids:
            continue
        item.state = SHIP_WAIT
        item.removed_reason = item.removed_by = item.removed_at = None
        back += 1
    session.flush()
    if back:
        allocate(session)
    return back


def moved_items(session: Session, *, day: date) -> list[ShipItem]:
    """Погружено не в свою машину за день — жёлтым на «Отгрузке»."""
    from app.timeutil import msk_day_bounds

    start, end = msk_day_bounds(day)
    return list(
        session.scalars(
            select(ShipItem)
            .where(ShipItem.moved_note.is_not(None), ShipItem.loaded_at >= start, ShipItem.loaded_at < end)
            .order_by(ShipItem.loaded_at)
        ).all()
    )
