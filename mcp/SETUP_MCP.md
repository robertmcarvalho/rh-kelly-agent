# Setup dos MCPs na IDE (recomendação imediata)

Este guia orienta a instalar e usar MCPs úteis para este projeto. Não instala nada automaticamente; serve como checklist e referência rápida.

Pré‑requisitos gerais
- gcloud SDK autenticado: `gcloud auth login` e `gcloud config set project rh-coopmob-bot` (região padrão: us-central1)
- Docker Desktop instalado e rodando
- GitHub CLI (gh) autenticado: `gh auth login`
- Acesso ao Postgres/Redis/GCS conforme ambientes

MCPs de produtividade
- GCP (Cloud Run/Logs/Secrets/Artifact Registry)
  - Ações: listar serviços, ver logs, atualizar env vars e secrets
  - Comandos úteis:
    - `gcloud run services list --region us-central1`
    - `gcloud run services describe <svc> --region us-central1`
    - `gcloud logs tail --project rh-coopmob-bot --service <svc>`
    - `gcloud secrets versions access latest --secret <name>`
- GCS (Storage)
  - Ações: listar buckets/objetos, checar ACLs, testar URLs assinadas
  - `gcloud storage buckets list`; `gcloud storage ls gs://<bucket>/`
- Postgres
  - Ferramenta: MCP/cliente SQL da IDE apontando para `DATABASE_URL`
  - Consultas frequentes: `SELECT * FROM leads ORDER BY created_at DESC LIMIT 50;`
- Redis
  - Ferramenta: MCP de Redis/CLI; checar contexto e dedupe
  - Chaves: `lead_ctx:*` e `seen_msg:*`
- HTTP/GraphQL
  - Ferramentas: REST/GraphQL Client na IDE (ex.: REST Client, Thunder Client)
  - Coleções: ver `http/panel-api.http`
- Git/GitHub
  - MCP para abrir PRs, reviews, releases sem sair do editor
- Docker
  - Build/push de imagens (Cloud Run) com cache local

MCPs específicos do stack (wrappers)
- WhatsApp (Graph API)
  - Tools: `send_template`, `check_window`, `send_text`
  - Env: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`
- Autentique
  - Tools: `create_request`, `get_status`
  - Env: `AUTENTIQUE_TOKEN`
- Notifications
  - Tools: agendar com Cloud Tasks / Scheduler (ou filas internas); idempotência
- Vagas/Leads (Core API)
  - Tools: CRUD/consultas rápidas para QA e debugging

Configuração sugerida de ambientes (env vars)
- Agente WhatsApp: `FORM_URL_BASE`, `REDIS_URL`, `LEAD_TTL_DAYS=30`
- Panel API: `DATABASE_URL`, `GCS_BUCKET`, `INTERNAL_API_TOKEN`, `AUTENTIQUE_TOKEN`
- Panel Web: URL do API em `NEXT_PUBLIC_API_BASE`

Validações rápidas
- Cloud Run: `gcloud run services list --region us-central1`
- API: `GET https://coopmob-panel-api-.../health`
- Leads: `POST /api/leads` com body JSON (exemplo na coleção http)
- Signed URL: `POST /api/upload/signed-url` com Bearer `INTERNAL_API_TOKEN`

Notas
- MCPs na IDE usam as mesmas credenciais locais (gcloud, gh, Docker). Centralize segredos em Secret Manager em produção.

