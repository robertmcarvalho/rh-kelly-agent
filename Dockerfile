# Use a lightweight Python image
FROM python:3.10-slim-buster

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Cloud Run will set the PORT environment variable
# The application should listen on this port
ENV PORT 8080

# Command to run the application
# Make sure to run from the 'services' directory where whatsapp.py is located
CMD ["uvicorn", "services.whatsapp:app", "--host", "0.0.0.0", "--port", "8080"]
