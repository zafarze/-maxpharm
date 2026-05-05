from flask import Flask, request, jsonify
from bot import bot
from scheduler import start_scheduler, fetch_data_from_1c_and_notify
from database import get_session
from models import Doctor
import threading
import config
import datetime

app = Flask(__name__)

@app.route('/api/trigger_sync', methods=['POST'])
def trigger_sync():
    """
    API endpoint для ручного инициирования массового опроса базы 1С.
    (Например, по запросу админа).
    """
    # Запускаем в отдельном потоке, чтобы не блокировать ответ API
    threading.Thread(target=fetch_data_from_1c_and_notify).start()
    return jsonify({
        "status": "ok", 
        "message": "Синхронизация запущена в фоновом режиме."
    })

@app.route('/api/update_doctor', methods=['POST'])
def update_doctor():
    """
    Альтернативный endpoint для получения данных напрямую от 1С (webhook).
    1С может отправлять сюда JSON-пакет при обновлении бонусов.
    """
    data = request.json
    if not data or not data.get("doctor_id"):
        return jsonify({"status": "error", "message": "Неверные данные"}), 400
        
    session = get_session()
    try:
        doctor = session.query(Doctor).filter_by(doctor_id=data.get("doctor_id")).first()
        if not doctor:
            return jsonify({"status": "error", "message": "Врач не найден"}), 404
            
        doctor.full_name = data.get("full_name", doctor.full_name)
        doctor.current_balance = data.get("price", doctor.current_balance)
        doctor.monthly_bonus = data.get("bonus", doctor.monthly_bonus)
        doctor.last_update = datetime.datetime.now()
        session.commit()
        
        # Опционально: можно сразу отправить уведомление
        # bot.send_message(doctor.telegram_id, "Ваши данные были обновлены...")
        
        return jsonify({"status": "ok"})
    except Exception as e:
        session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

def run_flask():
    app.run(host='0.0.0.0', port=config.PORT, debug=False, use_reloader=False)

if __name__ == '__main__':
    # 1. Запуск планировщика (Cron Job для опроса 1С раз в месяц)
    scheduler = start_scheduler()

    # 2. Запуск Flask сервера (в отдельном потоке)
    # Используется для предоставления HTTP-сервиса (webhook / ручной триггер)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 3. Запуск Telegram Бота
    print("Бот запущен...")
    try:
        # infinity_polling сам перехватывает KeyboardInterrupt и завершается
        bot.infinity_polling()
    except KeyboardInterrupt:
        pass
    finally:
        print("Останавливаю бота и планировщик...")
        try:
            bot.stop_polling()
        except Exception:
            pass
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        print("Завершено.")
