import os
import tempfile

from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from sqlalchemy import func
from database import get_session
from models import Doctor
from translations import get_text, TRANSLATIONS
from excel_parser import parse_excel
from bonus_service import process_upload
import config

# In-memory state
_broadcast_pending = {}  # {chat_id: (text, lang)}
_admin_lang = {}         # {chat_id: lang_code}


def is_admin(user_id):
    """Return True if the given Telegram user ID matches the configured admin."""
    return str(user_id) in config.ADMIN_IDS


def get_admin_lang(chat_id):
    return _admin_lang.get(int(chat_id), 'ru')


def _set_admin_lang(chat_id, lang):
    _admin_lang[int(chat_id)] = lang


def get_admin_menu_keyboard(lang):
    kb = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    kb.add(
        KeyboardButton(get_text(lang, 'admin_stats')),
        KeyboardButton(get_text(lang, 'admin_broadcast')),
        KeyboardButton(get_text(lang, 'admin_doctors')),
        KeyboardButton(get_text(lang, 'admin_upload_excel'))
    )
    kb.add(KeyboardButton(get_text(lang, 'change_lang')))
    return kb


def _matches(text, key):
    """Check if text matches the translation of key in any language."""
    return text in [TRANSLATIONS[l].get(key, '') for l in TRANSLATIONS]


def handle_admin_message(bot, message):
    """Route an admin text message to the correct feature handler."""
    lang = get_admin_lang(message.chat.id)
    text = message.text

    if _matches(text, 'admin_stats'):
        _send_stats(bot, message.chat.id, lang)
    elif _matches(text, 'admin_broadcast'):
        _start_broadcast(bot, message, lang)
    elif _matches(text, 'admin_doctors'):
        _show_doctors_list(bot, message.chat.id, lang)
    elif _matches(text, 'admin_upload_excel'):
        _start_excel_upload(bot, message, lang)
    elif _matches(text, 'change_lang'):
        _show_lang_selector(bot, message.chat.id, lang)
    else:
        bot.send_message(
            message.chat.id,
            get_text(lang, 'admin_menu_hint'),
            reply_markup=get_admin_menu_keyboard(lang)
        )


# ─── LANGUAGE CHANGE ─────────────────────────────────────────────────────────

def _show_lang_selector(bot, chat_id, lang):
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton('🇷🇺 Русский', callback_data='asetlang_ru'),
        InlineKeyboardButton('🇹🇯 Тоҷикӣ', callback_data='asetlang_tj'),
        InlineKeyboardButton('🇬🇧 English', callback_data='asetlang_en')
    )
    bot.send_message(chat_id, get_text(lang, 'choose_lang'), reply_markup=kb)


# ─── STATISTICS ──────────────────────────────────────────────────────────────

def _send_stats(bot, chat_id, lang):
    session = get_session()
    try:
        total = session.query(Doctor).count()
        linked = session.query(Doctor).filter(
            ~Doctor.telegram_id.like('PENDING%')
        ).count()
        total_bonus = session.query(func.sum(Doctor.monthly_bonus)).scalar() or 0.0
        total_balance = session.query(func.sum(Doctor.current_balance)).scalar() or 0.0
        last_sync = session.query(func.max(Doctor.last_update)).scalar()
        last_sync_str = last_sync.strftime("%d.%m.%Y %H:%M") if last_sync else "—"

        text = get_text(
            lang, 'admin_stats_msg',
            total=total, linked=linked, unlinked=total - linked,
            bonus=total_bonus, balance=total_balance, last_sync=last_sync_str
        )
        bot.send_message(chat_id, text, parse_mode='HTML')
    finally:
        session.close()


# ─── BROADCAST ───────────────────────────────────────────────────────────────

def _start_broadcast(bot, message, lang):
    kb = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    kb.add(KeyboardButton(get_text(lang, 'admin_broadcast_cancel_btn')))
    msg = bot.send_message(
        message.chat.id,
        get_text(lang, 'admin_broadcast_prompt'),
        parse_mode='HTML',
        reply_markup=kb
    )
    bot.register_next_step_handler(msg, lambda m: _process_broadcast_input(bot, m, lang))


def _process_broadcast_input(bot, message, lang):
    cancel_labels = [TRANSLATIONS[l].get('admin_broadcast_cancel_btn', '') for l in TRANSLATIONS]
    if message.text in cancel_labels:
        bot.send_message(
            message.chat.id,
            get_text(lang, 'admin_cancelled'),
            reply_markup=get_admin_menu_keyboard(lang)
        )
        return

    _broadcast_pending[message.chat.id] = (message.text, lang)

    session = get_session()
    try:
        count = session.query(Doctor).count()
    finally:
        session.close()

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(get_text(lang, 'admin_broadcast_send_btn'), callback_data='bc_confirm'),
        InlineKeyboardButton(get_text(lang, 'admin_broadcast_cancel_btn'), callback_data='bc_cancel')
    )
    bot.send_message(
        message.chat.id,
        get_text(lang, 'admin_broadcast_preview', text=message.text, count=count),
        parse_mode='HTML',
        reply_markup=kb
    )
    bot.send_message(
        message.chat.id,
        get_text(lang, 'admin_broadcast_hint'),
        reply_markup=get_admin_menu_keyboard(lang)
    )


def _do_broadcast(bot, chat_id):
    pending = _broadcast_pending.pop(chat_id, None)
    if not pending:
        bot.send_message(chat_id, get_text(get_admin_lang(chat_id), 'admin_error'))
        return

    text, lang = pending

    session = get_session()
    success = failed = 0
    try:
        doctors = session.query(Doctor).filter(
            ~Doctor.telegram_id.like('PENDING%')
        ).all()
        for doc in doctors:
            try:
                bot.send_message(
                    int(doc.telegram_id),
                    get_text(doc.language or 'ru', 'admin_broadcast_notify', text=text),
                    parse_mode='HTML'
                )
                success += 1
            except Exception:
                failed += 1
    finally:
        session.close()

    bot.send_message(
        chat_id,
        get_text(lang, 'admin_broadcast_done', success=success, failed=failed),
        parse_mode='HTML'
    )


# ─── DOCTORS MANAGEMENT ──────────────────────────────────────────────────────

def _show_doctors_list(bot, chat_id, lang, search=None):
    session = get_session()
    try:
        query = session.query(Doctor)
        if search:
            query = query.filter(
                Doctor.full_name.ilike(f'%{search}%') |
                Doctor.phone.ilike(f'%{search}%')
            )
        total = query.count()
        doctors = query.order_by(Doctor.id.desc()).limit(15).all()

        if not doctors:
            key = 'admin_doctors_not_found' if search else 'admin_doctors_empty'
            bot.send_message(chat_id, get_text(lang, key))
            return

        if search:
            header = get_text(lang, 'admin_doctors_search_header', total=total, search=search)
        else:
            header = get_text(lang, 'admin_doctors_header', total=total)

        kb = InlineKeyboardMarkup(row_width=1)
        for doc in doctors:
            registered = doc.telegram_id and not doc.telegram_id.startswith('PENDING')
            icon = "✅" if registered else "⏳"
            label = f"{icon} {doc.full_name or 'Без имени'} | {doc.phone or '—'}"
            kb.add(InlineKeyboardButton(label, callback_data=f'dr_{doc.id}'))

        kb.add(InlineKeyboardButton(get_text(lang, 'admin_doctors_search_btn'), callback_data='dr_search'))
        bot.send_message(chat_id, header, parse_mode='HTML', reply_markup=kb)
    finally:
        session.close()


def _show_doctor_detail(bot, call, lang):
    doc_id = int(call.data[3:])
    session = get_session()
    try:
        doc = session.query(Doctor).filter_by(id=doc_id).first()
        if not doc:
            bot.answer_callback_query(call.id, "Not found")
            return

        registered = doc.telegram_id and not doc.telegram_id.startswith('PENDING')
        linked_key = 'admin_doctor_linked' if registered else 'admin_doctor_not_linked'
        last_upd = doc.last_update.strftime("%d.%m.%Y %H:%M") if doc.last_update else "—"

        text = get_text(
            lang, 'admin_doctor_detail',
            name=doc.full_name or '—',
            phone=doc.phone or '—',
            address=doc.address or '—',
            code=doc.doctor_code or '—',
            bonus=doc.monthly_bonus,
            balance=doc.current_balance,
            linked=get_text(lang, linked_key),
            telegram_id=doc.telegram_id,
            last_upd=last_upd
        )
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton(get_text(lang, 'admin_doctors_back_btn'), callback_data='dr_back')
        )
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            parse_mode='HTML', reply_markup=kb
        )
    finally:
        session.close()


# ─── EXCEL UPLOAD ────────────────────────────────────────────────────────────

def _excel_cancel_kb(lang):
    kb = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    kb.add(KeyboardButton(get_text(lang, 'admin_broadcast_cancel_btn')))
    return kb


def _is_cancel(text):
    return text in [TRANSLATIONS[l].get('admin_broadcast_cancel_btn', '') for l in TRANSLATIONS]


def _start_excel_upload(bot, message, lang):
    msg = bot.send_message(
        message.chat.id,
        get_text(lang, 'admin_excel_prompt'),
        parse_mode='HTML',
        reply_markup=_excel_cancel_kb(lang)
    )
    bot.register_next_step_handler(msg, lambda m: _receive_excel(bot, m, lang))


def _receive_excel(bot, message, lang):
    if message.text and _is_cancel(message.text):
        bot.send_message(
            message.chat.id,
            get_text(lang, 'admin_cancelled'),
            reply_markup=get_admin_menu_keyboard(lang)
        )
        return

    doc = getattr(message, 'document', None)
    if not doc or not (doc.file_name or '').lower().endswith(('.xlsx', '.xlsm')):
        msg = bot.send_message(
            message.chat.id,
            get_text(lang, 'admin_excel_invalid'),
            reply_markup=_excel_cancel_kb(lang)
        )
        bot.register_next_step_handler(msg, lambda m: _receive_excel(bot, m, lang))
        return

    bot.send_message(message.chat.id, get_text(lang, 'admin_excel_processing'))

    tmp_path = None
    try:
        file_info = bot.get_file(doc.file_id)
        data = bot.download_file(file_info.file_path)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as f:
            f.write(data)
            tmp_path = f.name

        rows, errors = parse_excel(tmp_path)
        if not rows:
            bot.send_message(
                message.chat.id,
                get_text(lang, 'admin_excel_empty'),
                reply_markup=get_admin_menu_keyboard(lang)
            )
            return

        def _send(tg_id, text):
            try:
                # Язык получателя — для подписи кнопки.
                _s = get_session()
                try:
                    rcpt = _s.query(Doctor).filter_by(telegram_id=str(tg_id)).first()
                    rcpt_lang = (rcpt.language if rcpt and rcpt.language else 'ru')
                finally:
                    _s.close()
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton(
                    get_text(rcpt_lang, 'bonus_thanks_btn'),
                    callback_data='bonus_thanks'
                ))
                bot.send_message(int(tg_id), text, parse_mode='HTML', reply_markup=kb)
                return True
            except Exception as e:
                print(f"[excel] send error to {tg_id}: {e}")
                return False

        stats = process_upload(
            rows=rows,
            file_name=doc.file_name,
            admin_id=message.chat.id,
            send_message_fn=_send,
        )
        bot.send_message(
            message.chat.id,
            get_text(
                lang, 'admin_excel_done',
                total=stats['total_rows'],
                clients=stats['unique_clients'],
                notified=stats['notified'],
                pending=stats['pending'],
                failed=stats['failed'],
            ),
            parse_mode='HTML',
            reply_markup=get_admin_menu_keyboard(lang)
        )
    except ValueError as e:
        bot.send_message(
            message.chat.id,
            get_text(lang, 'admin_excel_error', error=str(e)),
            reply_markup=get_admin_menu_keyboard(lang)
        )
    except Exception as e:
        print(f"[excel] unexpected: {e}")
        bot.send_message(
            message.chat.id,
            get_text(lang, 'admin_excel_error', error=str(e)),
            reply_markup=get_admin_menu_keyboard(lang)
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ─── CALLBACK HANDLER REGISTRATION ───────────────────────────────────────────

def register_admin_handlers(bot):
    """Register all admin callback query handlers into the provided bot instance."""

    @bot.callback_query_handler(
        func=lambda c: c.data.startswith('asetlang_') and is_admin(c.from_user.id)
    )
    def cb_set_lang(call):
        lang_code = call.data.split('_')[1]
        _set_admin_lang(call.message.chat.id, lang_code)
        bot.edit_message_text(
            get_text(lang_code, 'lang_changed'),
            call.message.chat.id, call.message.message_id
        )
        bot.send_message(
            call.message.chat.id,
            get_text(lang_code, 'admin_welcome'),
            parse_mode='HTML',
            reply_markup=get_admin_menu_keyboard(lang_code)
        )

    @bot.callback_query_handler(
        func=lambda c: c.data in ('bc_confirm', 'bc_cancel') and is_admin(c.from_user.id)
    )
    def cb_broadcast(call):
        lang = get_admin_lang(call.message.chat.id)
        if call.data == 'bc_cancel':
            bot.edit_message_text(
                get_text(lang, 'admin_broadcast_cancelled'),
                call.message.chat.id, call.message.message_id
            )
            _broadcast_pending.pop(call.message.chat.id, None)
        else:
            bot.edit_message_text(
                get_text(lang, 'admin_broadcast_sending'),
                call.message.chat.id, call.message.message_id
            )
            _do_broadcast(bot, call.message.chat.id)

    @bot.callback_query_handler(
        func=lambda c: c.data == 'dr_search' and is_admin(c.from_user.id)
    )
    def cb_doctor_search(call):
        lang = get_admin_lang(call.message.chat.id)
        msg = bot.send_message(
            call.message.chat.id,
            get_text(lang, 'admin_doctors_search_prompt')
        )
        bot.register_next_step_handler(
            msg, lambda m: _show_doctors_list(bot, m.chat.id, get_admin_lang(m.chat.id), search=m.text)
        )

    @bot.callback_query_handler(
        func=lambda c: (
            c.data.startswith('dr_')
            and c.data not in ('dr_search', 'dr_back')
            and is_admin(c.from_user.id)
        )
    )
    def cb_doctor_detail(call):
        lang = get_admin_lang(call.message.chat.id)
        _show_doctor_detail(bot, call, lang)

    @bot.callback_query_handler(
        func=lambda c: c.data == 'dr_back' and is_admin(c.from_user.id)
    )
    def cb_doctor_back(call):
        lang = get_admin_lang(call.message.chat.id)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        _show_doctors_list(bot, call.message.chat.id, lang)
