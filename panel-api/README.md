CoopMob Panel API (FastAPI)

Objetivo
- API central para leads, documentos, assinaturas, notificações e eventos (auditoria).

Rodar localmente
- Python 3.11+
- pip install -r requirements.txt
- uvicorn app.main:app --reload --port 8081

Variáveis esperadas
- DATABASE_URL: postgres://user:pass@host:5432/db
- GCS_BUCKET: bucket privado no GCS
- GOOGLE_APPLICATION_CREDENTIALS (local) ou Workload Identity (prod)
- INTERNAL_API_TOKEN: opcional para proteger endpoints sensíveis

Endpoints iniciais
- GET /health
- POST /api/leads (upsert por telefone)
- GET /api/leads (filtros básicos)
- GET /api/leads/{id}
- POST /api/upload/signed-url (GCS v4, upload/download)

