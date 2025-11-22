FROM python:3.11-slim

# Install system deps required for PyNaCl
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsodium-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app
COPY . /app

# Install Python deps
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Run the bot
CMD ["python", "voice_tracker_bot.py"]
