"""
Бизнес-логика обработки Excel-загрузки:
  1. Сохранение всех строк в `BonusEntry`.
  2. Агрегация по клиенту (сумма по всем группам: Гамма, Бета, Дельта, Альфа, ОТС, Офт).
  3. Апсерт `Doctor` (создаётся как PENDING-<phone>, если ещё не зарегистрирован).
  4. Накопление monthly_bonus / yearly_bonus / current_balance.
  5. Рассылка уведомлений зарегистрированным клиентам.
"""
import datetime
from collections import defaultdict

from database import get_session
from models import Doctor, BonusUpload, BonusEntry
from translations import get_text

# Канонический порядок отображения
GROUP_ORDER = ['Гамма', 'Бета', 'Дельта', 'Альфа', 'ОТС', 'Офт']

# (stem, canonical). Длинные stems перед короткими, чтобы избежать ложных совпадений.
GROUP_STEMS = [
    ('дельт', 'Дельта'), ('делт', 'Дельта'), ('delt', 'Дельта'), ('δ', 'Дельта'),
    ('гамм',  'Гамма'),  ('gamm', 'Гамма'),  ('γ', 'Гамма'),
    ('альф',  'Альфа'),  ('alph', 'Альфа'),  ('α', 'Альфа'),
    ('бет',   'Бета'),   ('bet',  'Бета'),   ('β', 'Бета'),
    ('отс',   'ОТС'),    ('otc',  'ОТС'),
    ('офт',   'Офт'),    ('oft',  'Офт'),
]


def _classify_group(name):
    if not name:
        return None
    n = name.strip().lower()
    if not n:
        return None
    for stem, canonical in GROUP_STEMS:
        if n.startswith(stem):
            return canonical
    return None


def _fmt_amount(v):
    """500.0 → '500'; 80.5 → '80.5'."""
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip('0').rstrip('.')


def _aggregate(rows):
    """
    Группирует строки по клиенту. Ключ — телефон (если есть), иначе ФИО (lower).
    """
    clients = defaultdict(lambda: {
        'groups': defaultdict(float),
        'name': None, 'phone': None, 'specialty': None,
        'unknown_groups': [],
    })
    for r in rows:
        key = r['phone'] if r['phone'] else f"name:{r['client_name'].strip().lower()}"
        cl = clients[key]
        if not cl['name']:
            cl['name'] = r['client_name']
        if not cl['phone'] and r['phone']:
            cl['phone'] = r['phone']
        if not cl['specialty'] and r.get('specialty'):
            cl['specialty'] = r['specialty']

        canonical = _classify_group(r.get('group_name'))
        amount = float(r.get('amount') or 0.0)
        if canonical:
            cl['groups'][canonical] += amount
        elif r.get('group_name'):
            cl['unknown_groups'].append(r['group_name'])
    return clients


def get_doctor_breakdown(session, doctor):
    """
    Возвращает разбивку по группам доктора из ПОСЛЕДНЕЙ загрузки Excel,
    в которой встречался его телефон. Накопительный баланс
    хранится отдельно в Doctor.current_balance.
    """
    result = {g: 0.0 for g in GROUP_ORDER}
    if not doctor or not doctor.phone:
        return result
    last_upload_id = (
        session.query(BonusEntry.upload_id)
        .filter(BonusEntry.phone == doctor.phone)
        .order_by(BonusEntry.upload_id.desc())
        .limit(1)
        .scalar()
    )
    if last_upload_id is None:
        return result
    rows = session.query(BonusEntry).filter(
        BonusEntry.phone == doctor.phone,
        BonusEntry.upload_id == last_upload_id,
    ).all()
    for r in rows:
        canonical = _classify_group(r.group_name)
        if canonical:
            result[canonical] += float(r.amount or 0.0)
    return result


def _mask_amount(v):
    """Маскирует ТОЛЬКО хвостовые нули звёздочками.

    1500 -> '15**'   500 -> '5**'   80 -> '8*'   100 -> '1**'
    1055 -> '1055'   12345 -> '12345'   0 -> '**'   80.5 -> '80.5'
    """
    s = _fmt_amount(v)
    if not s or s == '0':
        return '**'
    trailing = len(s) - len(s.rstrip('0'))
    if trailing == 0:
        return s
    return s[:-trailing] + '*' * trailing


def format_breakdown_lines(breakdown):
    """Строит блок строк 'Гамма 15**' / 'Бета 8*' / ... для отображения в /status."""
    lines = [f"{g} {_mask_amount(breakdown[g])}" for g in GROUP_ORDER if breakdown.get(g, 0) > 0]
    return '\n'.join(lines) if lines else '—'


def _build_message(lang, name, groups):
    parts = [f"{g} {_mask_amount(groups[g])}" for g in GROUP_ORDER if groups.get(g, 0) > 0]
    summary = ', '.join(parts) if parts else '—'
    return get_text(lang or 'ru', 'bonus_notification', name=name, summary=summary)


def process_upload(rows, file_name, admin_id, send_message_fn):
    """
    rows: list[dict] из excel_parser.parse_excel
    send_message_fn: callable(telegram_id: str, text: str) -> bool
    Возвращает dict со статистикой.
    """
    session = get_session()
    try:
        upload = BonusUpload(
            file_name=file_name,
            uploaded_by=str(admin_id) if admin_id else None,
            total_rows=len(rows),
        )
        session.add(upload)
        session.flush()

        clients = _aggregate(rows)

        # 1) Сохраняем все строки сырыми
        for r in rows:
            session.add(BonusEntry(
                upload_id=upload.id,
                row_date=r.get('row_date'),
                organization=r.get('organization'),
                manager=r.get('manager'),
                oblast=r.get('oblast'),
                region=r.get('region'),
                object_name=r.get('object_name'),
                group_name=r.get('group_name'),
                client_name=r.get('client_name'),
                phone=r.get('phone'),
                specialty=r.get('specialty'),
                amount=float(r.get('amount') or 0.0),
            ))

        # 2) Апсерт докторов + рассылка
        notified = pending = failed = 0
        notifications = []  # (telegram_id, message) — отправляем после commit

        for cl in clients.values():
            phone = cl['phone']
            doctor = None
            if phone:
                doctor = session.query(Doctor).filter_by(phone=phone).first()
                if not doctor:
                    doctor = session.query(Doctor).filter_by(
                        telegram_id=f"PENDING-{phone}"
                    ).first()

            if not doctor:
                placeholder = (
                    f"PENDING-{phone}" if phone
                    else f"PENDING-NAME-{cl['name'][:40]}-{datetime.datetime.utcnow().timestamp()}"
                )
                doctor = Doctor(
                    telegram_id=placeholder,
                    full_name=cl['name'],
                    phone=phone,
                    specialty=cl['specialty'],
                    language='ru',
                )
                session.add(doctor)
                session.flush()
            else:
                # Excel — источник истины: перезаписываем ФИО/специальность,
                # если они присутствуют в файле. Пустые поля не затираем.
                if cl['name']:
                    doctor.full_name = cl['name']
                if cl['specialty']:
                    doctor.specialty = cl['specialty']
                if phone and not doctor.phone:
                    doctor.phone = phone

            total = sum(cl['groups'].values())
            doctor.monthly_bonus = (doctor.monthly_bonus or 0.0) + total
            doctor.yearly_bonus = (doctor.yearly_bonus or 0.0) + total
            doctor.current_balance = (doctor.current_balance or 0.0) + total
            doctor.last_update = datetime.datetime.utcnow()

            is_registered = doctor.telegram_id and not doctor.telegram_id.startswith('PENDING')
            if is_registered:
                msg = _build_message(doctor.language, cl['name'], dict(cl['groups']))
                notifications.append((doctor.telegram_id, msg))
            else:
                pending += 1

        upload.unique_clients = len(clients)
        upload.pending_count = pending
        session.commit()

        # 3) Рассылка вне транзакции
        for tg_id, msg in notifications:
            try:
                ok = send_message_fn(tg_id, msg)
            except Exception as e:
                print(f"[bonus] send error to {tg_id}: {e}")
                ok = False
            if ok:
                notified += 1
            else:
                failed += 1

        # 4) Финальная статистика
        upload.notified_count = notified
        upload.failed_count = failed
        session.commit()

        return {
            'total_rows': len(rows),
            'unique_clients': len(clients),
            'notified': notified,
            'pending': pending,
            'failed': failed,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
