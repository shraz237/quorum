FROM python:3.12-slim

WORKDIR /app

# Copy shared package first so it can be installed as a local dependency
COPY shared/ /app/shared/

# Copy service files
COPY services/analyzer/ /app/

# Install dependencies (shared is installed via file:// path in requirements.txt)
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]
