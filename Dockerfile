FROM python:3.11-slim

# Не писать .pyc, не буферизовать логи.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MINIAPP_HOST=0.0.0.0 \
    MINIAPP_PORT=8080

WORKDIR /app

# Сначала зависимости — для кэша слоёв.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код проекта.
COPY . .

# Папка для SQLite (монтируется volume'ом в compose).
RUN mkdir -p /app/data

EXPOSE 8080

# BOT_TOKEN и DOMAIN приходят из окружения (.env). Секретов в образе нет.
CMD ["python", "-m", "bot.main"]
