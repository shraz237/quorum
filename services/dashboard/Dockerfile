# ---------------------------------------------------------------------------
# Stage 1: Build React frontend
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

COPY services/dashboard/frontend/package.json ./
RUN npm install

COPY services/dashboard/frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Python backend + serve static frontend via FastAPI / uvicorn
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS backend

WORKDIR /app

# Copy shared library
COPY shared/ ./shared/

# Copy backend
COPY services/dashboard/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/dashboard/backend/ ./

# Copy built frontend into static directory served by FastAPI
COPY --from=frontend-builder /frontend/dist ./static

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
