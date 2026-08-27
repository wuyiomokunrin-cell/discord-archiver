# Discord server archiver.
#
# The archive (SQLite + attachments + exports) lives under $ARCHIVER_DATA_DIR,
# which is a volume so your data survives container rebuilds. Credentials come
# from the environment (or an env_file in compose), never from the image.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Point storage at the mounted volume. data/ layout inside it is created
# automatically: archive.sqlite3, attachments/, exports/.
ENV ARCHIVER_DATA_DIR=/data
VOLUME ["/data"]

# Default to live capture; override the command for backfill/dashboard, e.g.
#   docker run ... image python main.py backfill
#   docker run ... image python main.py dashboard --port 8080
CMD ["python", "main.py", "listen"]
