FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходники
COPY main.py bitrix24_notifier.py map.json ./

# Тома для персистентных данных (last_booking_id.txt, status_history.json)
VOLUME ["/app/data"]

# Запуск бота
CMD ["python", "main.py"]