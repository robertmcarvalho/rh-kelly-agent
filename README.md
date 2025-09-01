# RH Kelly Agent

This project contains services for integrating a FastAPI application with the WhatsApp Business API.

## Required environment variables

The WhatsApp service requires the following variables to be defined before startup:

- `WHATSAPP_ACCESS_TOKEN` – Permanent token for the WhatsApp Business API.
- `WHATSAPP_PHONE_NUMBER_ID` – Phone number ID associated with the Business account.
- `VERIFY_TOKEN` – Secret token used to validate the webhook with Meta.
- `GOOGLE_API_KEY` – API key for Google Generative AI services.
- `ADK_API_URL` – URL of the ADK API server used by the agent runner.

Optional variables such as `REDIS_URL`, `INTERNAL_API_TOKEN`, `GSHEETS_SERVICE_ACCOUNT_JSON`, and others may further customize behavior.

The application validates these variables during startup and will log an error and refuse to run if any are missing. Configure them in your deployment environment or a `.env` file to ensure the service initializes correctly.
