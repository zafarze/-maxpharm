import html
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from database import get_session
from models import Doctor
from translations import get_text, TRANSLATIONS
from admin import is_admin, get_admin_lang, get_admin_menu_keyboard, handle_admin_message, register_admin_handlers
from excel_parser import normalize_phone
from bonus_service import get_doctor_breakdown, format_breakdown_lines, _fmt_amount
import config
import requests

# Инициализируем бота
bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)

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
    btn_about = KeyboardButton(text=get_text(lang, 'about'))
    btn_contacts = KeyboardButton(text=get_text(lang, 'contacts'))
    btn_profile = KeyboardButton(text=get_text(lang, 'profile'))
    btn_lang = KeyboardButton(text=get_text(lang, 'change_lang'))
    
    keyboard.add(btn_bonus, btn_about)
    keyboard.add(btn_contacts, btn_profile)
    keyboard.add(btn_lang)
    return keyboard

@bot.message_handler(commands=['start'])
def handle_start(message):
    """Приветственное сообщение и запрос номера телефона."""
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
            balance_total = doctor.current_balance or 0.0
            text = get_text(lang, 'status_msg',
                            name=doctor.full_name,
                            summary=summary,
                            balance=_fmt_amount(balance_total),
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
    
    if call.data == 'edit_phone':
        keyboard = ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
        keyboard.add(
            KeyboardButton(text=get_text(lang, 'req_phone'), request_contact=True),
            KeyboardButton(text=get_text(lang, 'cancel'))
        )
        msg = bot.send_message(call.message.chat.id, get_text(lang, 'enter_new_phone'), reply_markup=keyboard)
        bot.register_next_step_handler(msg, process_new_phone)
    elif call.data == 'edit_address':
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
    except Exception as e:
        bot.send_message(message.chat.id, get_text(lang, 'error'), reply_markup=get_main_menu_keyboard(lang))
        print(f"Error updating address: {e}")
    finally:
        session.close()


# Register admin callback handlers (must come after all client handlers)
register_admin_handlers(bot)
