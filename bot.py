import html
import threading
import datetime
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from database import get_session
from models import Doctor, BonusAck, FeedbackMessage, Survey, SurveyQuestion, SurveyResponse, SurveyAnswer
from translations import get_text, TRANSLATIONS
from admin import is_admin, get_admin_lang, get_admin_menu_keyboard, handle_admin_message, register_admin_handlers
from excel_parser import normalize_phone
from bonus_service import get_doctor_breakdown, format_breakdown_lines
import config
import requests

# Инициализируем бота
bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)

# Thread-safe set of chat_ids ожидающих ввод feedback после нажатия "Обратная связь"
_awaiting_feedback_lock = threading.Lock()
_awaiting_feedback: set[int] = set()


def _add_awaiting_feedback(chat_id: int) -> None:
    with _awaiting_feedback_lock:
        _awaiting_feedback.add(chat_id)


def _clear_awaiting_feedback(chat_id: int) -> bool:
    """Атомарно убирает chat_id из set. Возвращает True, если он там был."""
    with _awaiting_feedback_lock:
        if chat_id in _awaiting_feedback:
            _awaiting_feedback.discard(chat_id)
            return True
        return False

def get_user_lang(user_id):
    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(user_id)).first()
        return doctor.language if doctor and doctor.language else 'ru'
    except:
        return 'ru'
    finally:
        session.close()

def get_main_menu_keyboard(lang):
    keyboard = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn_bonus = KeyboardButton(text=get_text(lang, 'bonus'))
    btn_contacts = KeyboardButton(text=get_text(lang, 'contacts'))
    btn_profile = KeyboardButton(text=get_text(lang, 'profile'))
    btn_lang = KeyboardButton(text=get_text(lang, 'change_lang'))

    keyboard.add(btn_bonus)
    keyboard.add(btn_contacts, btn_profile)
    keyboard.add(btn_lang)
    return keyboard

@bot.message_handler(commands=['start'])
def handle_start(message):
    """Приветственное сообщение и запрос номера телефона."""
    _clear_awaiting_feedback(message.chat.id)
    # Admin gets a separate panel
    if is_admin(message.chat.id):
        lang = get_admin_lang(message.chat.id)
        bot.send_message(
            message.chat.id,
            get_text(lang, 'admin_welcome'),
            parse_mode='HTML',
            reply_markup=get_admin_menu_keyboard(lang)
        )
        return

    lang = get_user_lang(message.chat.id)
    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(message.chat.id)).first()
        if doctor:
            bot.send_message(
                message.chat.id,
                get_text(lang, 'auth_success'),
                reply_markup=get_main_menu_keyboard(lang)
            )
            return
    finally:
        session.close()

    keyboard = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    button_phone = KeyboardButton(text=get_text(lang, 'req_phone'), request_contact=True)
    keyboard.add(button_phone)

    bot.send_message(
        message.chat.id,
        get_text(lang, 'welcome'),
        reply_markup=keyboard
    )

@bot.message_handler(content_types=['contact'])
def handle_contact(message):
    """Обработка полученного контакта."""
    lang = 'ru'
    phone_raw = message.contact.phone_number
    phone_norm = normalize_phone(phone_raw)
    telegram_id_str = str(message.chat.id)

    session = get_session()
    notify_admin = False
    try:
        # 1. Уже зарегистрирован?
        doctor = session.query(Doctor).filter_by(telegram_id=telegram_id_str).first()

        # 2. Иначе ищем PENDING-запись из Excel — по телефону или плейсхолдеру.
        if not doctor and phone_norm:
            doctor = session.query(Doctor).filter_by(phone=phone_norm).first()
            if not doctor:
                doctor = session.query(Doctor).filter_by(
                    telegram_id=f"PENDING-{phone_norm}"
                ).first()

            if doctor:
                # Привязываем существующую запись к этому Telegram-аккаунту.
                doctor.telegram_id = telegram_id_str
                doctor.phone = phone_norm
                if not doctor.full_name:
                    doctor.full_name = message.from_user.first_name
                if not doctor.language:
                    doctor.language = lang
                session.commit()
                notify_admin = True

        # 3. Иначе создаём нового.
        if not doctor:
            doctor = Doctor(
                telegram_id=telegram_id_str,
                full_name=message.from_user.first_name,
                language=lang,
                phone=phone_norm,
            )
            session.add(doctor)
            session.commit()
            notify_admin = True
        elif not doctor.phone:
            doctor.phone = phone_norm
            session.commit()

        if notify_admin and config.ADMIN_IDS:
            admin_msg = (
                f"🆕 Зарегистрирован пользователь!\n\n"
                f"👤 Имя: {doctor.full_name}\n"
                f"📞 Телефон: {doctor.phone}\n"
                f"🆔 ID: {doctor.telegram_id}"
            )
            for admin_id in config.ADMIN_IDS:
                try:
                    bot.send_message(admin_id, admin_msg)
                except Exception as e:
                    print(f"Ошибка отправки уведомления админу {admin_id}: {e}")

        lang = doctor.language or 'ru'
        bot.send_message(
            message.chat.id,
            get_text(lang, 'auth_success'),
            reply_markup=get_main_menu_keyboard(lang)
        )
    except Exception as e:
        bot.send_message(message.chat.id, get_text(lang, 'auth_error'))
        print(f"Error: {e}")
    finally:
        session.close()

@bot.message_handler(commands=['status'])
def handle_status(message):
    lang = get_user_lang(message.chat.id)
    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(message.chat.id)).first()
        if doctor and doctor.full_name:
            date_str = doctor.last_update.strftime("%d.%m.%Y %H:%M")
            breakdown = get_doctor_breakdown(session, doctor)
            summary = format_breakdown_lines(breakdown)
            text = get_text(lang, 'status_msg',
                            name=doctor.full_name,
                            summary=summary,
                            date=date_str)
            bot.reply_to(message, text)
        else:
            bot.reply_to(message, get_text(lang, 'not_found'))
    except Exception as e:
        bot.reply_to(message, get_text(lang, 'error'))
        print(f"Error in status command: {e}")
    finally:
        session.close()

@bot.message_handler(content_types=['text'])
def handle_text(message):
    # Route admin messages to the admin panel
    if is_admin(message.chat.id):
        handle_admin_message(bot, message)
        return

    # Feedback capture — пользователь нажал "Обратная связь" и пишет ответ.
    if _clear_awaiting_feedback(message.chat.id):
        _forward_feedback_to_admins(message)
        return

    # Survey answer capture — doctor is answering an active survey.
    if _process_survey_answer(message):
        return

    lang = get_user_lang(message.chat.id)
    text = message.text

    # Определяем, какую кнопку нажал пользователь, сравнивая со всеми языками
    if text in [TRANSLATIONS[l]['bonus'] for l in TRANSLATIONS]:
        handle_status(message)
    elif text in [TRANSLATIONS[l]['about'] for l in TRANSLATIONS]:
        bot.send_message(message.chat.id, get_text(lang, 'about_text'), parse_mode='HTML')
    elif text in [TRANSLATIONS[l]['contacts'] for l in TRANSLATIONS]:
        bot.send_message(message.chat.id, get_text(lang, 'contacts_text'), parse_mode='HTML')
    elif text in [TRANSLATIONS[l]['profile'] for l in TRANSLATIONS]:
        session = get_session()
        try:
            doctor = session.query(Doctor).filter_by(telegram_id=str(message.chat.id)).first()
            if doctor and doctor.full_name:
                profile_msg = get_text(lang, 'profile_text', 
                                       name=doctor.full_name or '-', 
                                       phone=doctor.phone or '-', 
                                       address=doctor.address or '-', 
                                       code=doctor.doctor_code or '-')
                
                keyboard = InlineKeyboardMarkup(row_width=1)
                keyboard.add(
                    InlineKeyboardButton(get_text(lang, 'edit_phone'), callback_data='edit_phone'),
                    InlineKeyboardButton(get_text(lang, 'edit_address'), callback_data='edit_address')
                )
                
                bot.send_message(message.chat.id, profile_msg, parse_mode='HTML', reply_markup=keyboard)
            else:
                bot.send_message(message.chat.id, get_text(lang, 'not_found'))
        finally:
            session.close()
    elif text in [TRANSLATIONS[l]['change_lang'] for l in TRANSLATIONS]:
        keyboard = InlineKeyboardMarkup(row_width=3)
        keyboard.add(
            InlineKeyboardButton('🇷🇺 Русский', callback_data='setlang_ru'),
            InlineKeyboardButton('🇹🇯 Тоҷикӣ', callback_data='setlang_tj'),
            InlineKeyboardButton('🇬🇧 English', callback_data='setlang_en')
        )
        bot.send_message(message.chat.id, get_text(lang, 'choose_lang'), reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data == 'bonus_thanks')
def handle_bonus_thanks(call):
    user_id = call.message.chat.id
    lang = get_user_lang(user_id)

    if is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return

    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(user_id)).first()
        name = (doctor.full_name if doctor and doctor.full_name else
                call.from_user.full_name)
        phone = (doctor.phone if doctor and doctor.phone else '-')
    finally:
        session.close()

    # 1. Снимаем кнопку и подтверждаем пользователю.
    try:
        bot.edit_message_reply_markup(
            chat_id=user_id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id, text=get_text(lang, 'bonus_thanks_acked'))
    try:
        bot.send_message(user_id, get_text(lang, 'bonus_thanks_acked'))
    except Exception:
        pass

    # 2. Уведомляем админов.
    admin_msg = get_text('ru', 'admin_bonus_acked',
                         name=html.escape(name or '-'),
                         phone=html.escape(phone or '-'))
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_msg, parse_mode='HTML')
        except Exception as e:
            print(f"[bonus_thanks] admin notify error {admin_id}: {e}")

    # 3. Persist ack in DB — idempotent on (telegram_id, message_id).
    session = get_session()
    try:
        msg_id = call.message.message_id
        exists = session.query(BonusAck).filter_by(
            telegram_id=str(user_id),
            message_id=msg_id,
        ).first()
        if not exists:
            doctor_row = session.query(Doctor).filter_by(telegram_id=str(user_id)).first()
            session.add(BonusAck(
                doctor_ref_id=doctor_row.id if doctor_row else None,
                telegram_id=str(user_id),
                message_id=msg_id,
            ))
            session.commit()
    except Exception as e:
        session.rollback()
        print(f"[bonus_thanks] persist error: {type(e).__name__}")
    finally:
        session.close()


@bot.callback_query_handler(func=lambda call: call.data == 'bonus_feedback')
def handle_bonus_feedback_btn(call):
    if is_admin(call.from_user.id):
        bot.answer_callback_query(call.id)
        return

    user_id = call.message.chat.id
    lang = get_user_lang(user_id)

    # Снимаем клавиатуру с уведомления, чтобы повторно не нажали.
    try:
        bot.edit_message_reply_markup(
            chat_id=user_id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except Exception:
        pass

    # Убиваем любой pending step-handler (edit_phone / edit_address и т.п.)
    # чтобы feedback-flow и step-handlers не конкурировали за следующее сообщение.
    try:
        bot.clear_step_handler_by_chat_id(user_id)
    except Exception:
        pass

    _add_awaiting_feedback(user_id)
    bot.answer_callback_query(call.id)
    bot.send_message(user_id, get_text(lang, 'bonus_feedback_prompt'))


def _forward_feedback_to_admins(message):
    user_id = message.chat.id
    lang = get_user_lang(user_id)

    # Если пользователь нажал "Отмена" вместо написания feedback — не форвардим.
    cancel_labels = {TRANSLATIONS[l].get('cancel', '') for l in TRANSLATIONS}
    cancel_labels.discard('')
    if message.text in cancel_labels:
        try:
            bot.send_message(message.chat.id, get_text(lang, 'action_cancelled'))
        except Exception as e:
            print(f"[feedback] cancel ACK send error to {message.chat.id}: {e}")
        return

    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(user_id)).first()
        name = (doctor.full_name if doctor and doctor.full_name
                else (message.from_user.full_name if message.from_user else '-'))
        phone = (doctor.phone if doctor and doctor.phone else '-')

        try:
            session.add(FeedbackMessage(
                doctor_ref_id=doctor.id if doctor else None,
                telegram_id=str(user_id),
                full_name=name,
                phone=phone,
                text=message.text or '',
            ))
            session.commit()
        except Exception as e:
            session.rollback()
            print(f"[feedback] persist error: {type(e).__name__}")
    finally:
        session.close()

    admin_text = get_text(
        'ru', 'admin_feedback_received',
        name=html.escape(name or '-'),
        phone=html.escape(phone or '-'),
        tg_id=str(user_id),
        text=html.escape(message.text or ''),
    )
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_text, parse_mode='HTML')
        except Exception as e:
            print(f"[feedback] admin notify error {admin_id}: {e}")

    try:
        bot.send_message(user_id, get_text(lang, 'bonus_feedback_sent'))
    except Exception as e:
        print(f"[feedback] ACK send error to {user_id}: {e}")

    # Resume active survey if doctor interrupted it with feedback.
    _resume_survey_if_active(user_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('setlang_'))
def handle_set_language_callback(call):
    lang_code = call.data.split('_')[1]
    user_id = call.message.chat.id
    
    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(user_id)).first()
        if doctor:
            doctor.language = lang_code
            session.commit()
            
            # Обновляем сообщение (убираем инлайн кнопки и пишем текст)
            bot.edit_message_text(
                chat_id=user_id,
                message_id=call.message.message_id,
                text=get_text(lang_code, 'lang_changed'),
                reply_markup=None
            )
            
            # Отправляем обновленное главное меню
            bot.send_message(
                user_id, 
                get_text(lang_code, 'auth_success'), 
                reply_markup=get_main_menu_keyboard(lang_code)
            )
    except Exception as e:
        print(f"Error setting language via callback: {e}")
    finally:
        session.close()

def set_language(user_id, lang_code):
    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(user_id)).first()
        if doctor:
            doctor.language = lang_code
            session.commit()
            bot.send_message(
                user_id, 
                get_text(lang_code, 'lang_changed'), 
                reply_markup=get_main_menu_keyboard(lang_code)
            )
    except Exception as e:
        print(f"Error setting language: {e}")
    finally:
        session.close()

@bot.callback_query_handler(func=lambda call: call.data in ['edit_phone', 'edit_address'])
def handle_edit_profile(call):
    lang = get_user_lang(call.message.chat.id)

    if _has_active_survey(call.message.chat.id):
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            get_text(lang, 'profile_edit_blocked_survey'),
        )
        return

    if call.data == 'edit_phone':
        _clear_awaiting_feedback(call.message.chat.id)
        keyboard = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
        keyboard.add(
            KeyboardButton(text=get_text(lang, 'req_phone'), request_contact=True),
            KeyboardButton(text=get_text(lang, 'cancel'))
        )
        msg = bot.send_message(call.message.chat.id, get_text(lang, 'enter_new_phone'), reply_markup=keyboard)
        bot.register_next_step_handler(msg, process_new_phone)
    elif call.data == 'edit_address':
        _clear_awaiting_feedback(call.message.chat.id)
        keyboard = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
        keyboard.add(KeyboardButton(text=get_text(lang, 'cancel')))
        msg = bot.send_message(call.message.chat.id, get_text(lang, 'enter_new_address'), reply_markup=keyboard)
        bot.register_next_step_handler(msg, process_new_address)

def process_new_phone(message):
    lang = get_user_lang(message.chat.id)
    if message.text in [TRANSLATIONS[l]['cancel'] for l in TRANSLATIONS]:
        bot.send_message(message.chat.id, get_text(lang, 'action_cancelled'), reply_markup=get_main_menu_keyboard(lang))
        return
        
    new_phone = None
    if message.contact:
        new_phone = message.contact.phone_number
    else:
        new_phone = message.text

    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(message.chat.id)).first()
        if doctor:
            doctor.phone = new_phone
            session.commit()
            bot.send_message(message.chat.id, get_text(lang, 'phone_updated'), reply_markup=get_main_menu_keyboard(lang))

            # Show updated profile
            profile_msg = get_text(lang, 'profile_text',
                                   name=doctor.full_name or '-',
                                   phone=doctor.phone or '-',
                                   address=doctor.address or '-',
                                   code=doctor.doctor_code or '-')
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                InlineKeyboardButton(get_text(lang, 'edit_phone'), callback_data='edit_phone'),
                InlineKeyboardButton(get_text(lang, 'edit_address'), callback_data='edit_address')
            )
            bot.send_message(message.chat.id, profile_msg, parse_mode='HTML', reply_markup=keyboard)
            _resume_survey_if_active(message.chat.id)
    except Exception as e:
        bot.send_message(message.chat.id, get_text(lang, 'error'), reply_markup=get_main_menu_keyboard(lang))
        print(f"Error updating phone: {e}")
    finally:
        session.close()

def process_new_address(message):
    lang = get_user_lang(message.chat.id)
    if message.text in [TRANSLATIONS[l]['cancel'] for l in TRANSLATIONS]:
        bot.send_message(message.chat.id, get_text(lang, 'action_cancelled'), reply_markup=get_main_menu_keyboard(lang))
        return
        
    new_address = message.text

    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(telegram_id=str(message.chat.id)).first()
        if doctor:
            doctor.address = new_address
            session.commit()
            bot.send_message(message.chat.id, get_text(lang, 'address_updated'), reply_markup=get_main_menu_keyboard(lang))

            # Show updated profile
            profile_msg = get_text(lang, 'profile_text',
                                   name=doctor.full_name or '-',
                                   phone=doctor.phone or '-',
                                   address=doctor.address or '-',
                                   code=doctor.doctor_code or '-')
            keyboard = InlineKeyboardMarkup(row_width=1)
            keyboard.add(
                InlineKeyboardButton(get_text(lang, 'edit_phone'), callback_data='edit_phone'),
                InlineKeyboardButton(get_text(lang, 'edit_address'), callback_data='edit_address')
            )
            bot.send_message(message.chat.id, profile_msg, parse_mode='HTML', reply_markup=keyboard)
            _resume_survey_if_active(message.chat.id)
    except Exception as e:
        bot.send_message(message.chat.id, get_text(lang, 'error'), reply_markup=get_main_menu_keyboard(lang))
        print(f"Error updating address: {e}")
    finally:
        session.close()


def _user_lang_for_doctor(session, telegram_id):
    """Returns the doctor's language preference, falling back to 'ru'."""
    doc = session.query(Doctor).filter_by(telegram_id=str(telegram_id)).first()
    return (doc.language if doc and doc.language else 'ru')


def _has_active_survey(user_id):
    """Returns True if doctor has an in-progress SurveyResponse."""
    session = get_session()
    try:
        return session.query(SurveyResponse).filter_by(
            telegram_id=str(user_id),
            status='in_progress',
        ).first() is not None
    finally:
        session.close()


def _process_survey_answer(message):
    """Save the message as an answer if doctor has an active SurveyResponse.
    Returns True if message was consumed."""
    user_id = message.chat.id
    if not message.text:
        return False  # silently skip non-text in survey context

    session = get_session()
    try:
        response = session.query(SurveyResponse).filter_by(
            telegram_id=str(user_id),
            status='in_progress',
        ).order_by(SurveyResponse.id.desc()).first()
        if not response:
            return False

        # Guard against race: another thread may have cancelled this response
        session.refresh(response)
        if response.status != 'in_progress':
            return False

        questions = session.query(SurveyQuestion).filter_by(
            survey_id=response.survey_id
        ).order_by(SurveyQuestion.order.asc()).all()
        if not questions:
            return False

        current_idx = response.current_question_idx or 1
        if current_idx > len(questions):
            # safety: mark completed if somehow idx overshot
            response.status = 'completed'
            response.completed_at = datetime.datetime.utcnow()
            session.commit()
            return False

        # Save answer for the current question — idempotent on re-delivery
        current_question = questions[current_idx - 1]
        existing = session.query(SurveyAnswer).filter_by(
            response_id=response.id,
            question_id=current_question.id,
        ).first()
        if existing:
            # Duplicate due to Telegram re-delivering the same update — silently consume
            return True
        session.add(SurveyAnswer(
            response_id=response.id,
            question_id=current_question.id,
            text=message.text,
        ))

        # Advance to next question
        next_idx = current_idx + 1
        if next_idx > len(questions):
            response.status = 'completed'
            response.completed_at = datetime.datetime.utcnow()
            response.current_question_idx = next_idx
            session.commit()
            doc_lang = _user_lang_for_doctor(session, user_id)
            try:
                bot.send_message(
                    user_id,
                    get_text(doc_lang, 'survey_completed', total=len(questions)),
                    parse_mode='HTML',
                )
            except Exception as e:
                print(f"[survey] send completion error to {user_id}: {type(e).__name__}")
            return True

        response.current_question_idx = next_idx
        session.commit()
        next_q = questions[next_idx - 1]
        doc_lang = _user_lang_for_doctor(session, user_id)
        try:
            bot.send_message(
                user_id,
                get_text(doc_lang, 'survey_question',
                         current=next_idx, total=len(questions),
                         text=html.escape(next_q.text)),
                parse_mode='HTML',
            )
        except Exception as e:
            print(f"[survey] send next-q error to {user_id}: {type(e).__name__}")
        return True
    except Exception as e:
        session.rollback()
        print(f"[survey] process_answer error: {type(e).__name__}: {e}")
        return False
    finally:
        session.close()


def _resume_survey_if_active(user_id):
    """If the doctor has an in-progress SurveyResponse, re-send the current question."""
    session = get_session()
    try:
        response = session.query(SurveyResponse).filter_by(
            telegram_id=str(user_id),
            status='in_progress',
        ).order_by(SurveyResponse.id.desc()).first()
        if not response:
            return
        questions = session.query(SurveyQuestion).filter_by(
            survey_id=response.survey_id
        ).order_by(SurveyQuestion.order.asc()).all()
        idx = response.current_question_idx or 1
        if idx > len(questions):
            return
        q = questions[idx - 1]
        doc_lang = _user_lang_for_doctor(session, user_id)
        try:
            bot.send_message(
                user_id,
                get_text(doc_lang, 'survey_resume',
                         current=idx, total=len(questions), text=html.escape(q.text)),
                parse_mode='HTML',
            )
        except Exception as e:
            print(f"[survey] resume send error to {user_id}: response_id={response.id}, "
                  f"survey_id={response.survey_id}, idx={idx}, type={type(e).__name__}")
    except Exception as e:
        print(f"[survey] resume error: {type(e).__name__}: {e}")
    finally:
        session.close()


# Register admin callback handlers (must come after all client handlers)
register_admin_handlers(bot)
