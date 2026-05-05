import requests
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from database import get_session
from models import Doctor
from bot import bot
import config

def fetch_data_from_1c_and_notify():
    """
    Массовый опрос базы 1С для каждого врача,
    обновление информации и рассылка уведомлений.
    """
    print(f"[{datetime.datetime.now()}] Начинаем опрос 1С...")
    session = get_session()
    
    try:
        doctors = session.query(Doctor).filter(Doctor.doctor_id.isnot(None)).all()
        
        for doctor in doctors:
            try:
                # В реальности мы бы отправляли токен авторизации или другие параметры
                response = requests.get(f"{config.ONEC_API_URL}?doctor_id={doctor.doctor_id}", timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if data.get("status") == "ok":
                        # Обновление данных
                        doctor.full_name = data.get("full_name", doctor.full_name)
                        doctor.current_balance = data.get("price", doctor.current_balance)
                        doctor.monthly_bonus = data.get("bonus", doctor.monthly_bonus)
                        doctor.last_update = datetime.datetime.now()
                        
                        session.commit()
                        
                        # Формирование и отправка уведомления по шаблону из ТЗ
                        date_str = doctor.last_update.strftime("%d.%m.%Y")
                        msg = (
                            f"📊 ВАШ ОТЧЕТ ЗА МЕСЯЦ\n\n"
                            f"👤 Врач: {doctor.full_name}\n"
                            f"💰 Начислено бонусов: {doctor.monthly_bonus} сомони\n"
                            f"💳 Текущая стоимость/баланс: {doctor.current_balance} сомони\n\n"
                            f"Данные актуальны на {date_str}. Спасибо за работу!"
                        )
                        
                        try:
                            bot.send_message(doctor.telegram_id, msg)
                        except Exception as e:
                            print(f"Ошибка отправки сообщения пользователю {doctor.telegram_id}: {e}")
                    else:
                        print(f"1С вернул ошибку для врача {doctor.doctor_id}: {data}")
                else:
                    print(f"Неудачный запрос к 1С для врача {doctor.doctor_id}, статус: {response.status_code}")
                    
            except requests.RequestException as e:
                print(f"Сетевая ошибка при запросе к 1С для доктора {doctor.doctor_id}: {e}")
            except Exception as e:
                print(f"Непредвиденная ошибка при обработке доктора {doctor.doctor_id}: {e}")
                
    except Exception as e:
        print(f"Ошибка при работе с БД: {e}")
    finally:
        session.close()
    
    print(f"[{datetime.datetime.now()}] Опрос 1С завершен.")

def start_scheduler():
    """
    Настройка и запуск планировщика задач.
    """
    scheduler = BackgroundScheduler()
    
    # Если в конфиге установлена звездочка, можно использовать интервалы, 
    # но в ТЗ сказано "раз в месяц".
    # Настраиваем по cron.
    
    kwargs = {}
    if config.SCHEDULER_DAY != '*': kwargs['day'] = config.SCHEDULER_DAY
    if config.SCHEDULER_HOUR != '*': kwargs['hour'] = config.SCHEDULER_HOUR
    if config.SCHEDULER_MINUTE != '*': kwargs['minute'] = config.SCHEDULER_MINUTE
    
    scheduler.add_job(
        fetch_data_from_1c_and_notify, 
        'cron', 
        **kwargs
    )
    
    # Запуск планировщика в фоновом режиме
    scheduler.start()
    print("Планировщик запущен. Ожидание расписания...")
    return scheduler
