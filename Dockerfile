# Use a lightweight Python image
FROM python:3.10-slim-buster

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Cloud Run sets $PORT. Default to 8080 locally.
ENV PORT=8080

# Start the FastAPI app. Use $PORT when provided by Cloud Run.
CMD ["/bin/sh", "-c", "uvicorn services.whatsapp:app --host 0.0.0.0 --port ${PORT:-8080}"]
