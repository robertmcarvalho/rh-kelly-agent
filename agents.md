# Agents, MCPs e Fluxo – Guia de Produção

Objetivo: consolidar decisões, integrações e defaults para reduzir ambiguidade e acelerar a entrega. Este documento orienta como os agentes interagem, quais MCPs usar, variáveis de ambiente, endpoints e guardrails.

## 1) Arquitetura Geral (sem Pipefy)

- Frontend: Next.js (App Router) para formulário público e painel administrativo (Cloud Run, us-central1).
- Core API: FastAPI (Cloud Run, us-central1) – leads, vagas, documentos, assinatura, notificações.
- Banco: Cloud SQL Postgres (UTF-8).
- Sessão/chat: Redis para contexto transiente (persistência entre conversas).
- Arquivos: Google Cloud Storage (bucket privado) com URLs assinadas para upload/download.
- Assinatura: Autentique (webhooks → Core API).
- Mensageria: WhatsApp Business (templates HSM para lembretes/status).
- Observabilidade: Cloud Logging/Error Reporting + tabela `events` (auditoria).

## 2) Agentes

- Agente WhatsApp (existente – `rh-kelly-agent`)
  - Capta lead, envia link do formulário (`FORM_URL_BASE/f/{token}`), responde dúvidas básicas, reenvia menus.
  - Salva/recupera contexto no Redis; persiste estado de negócio no Postgres via Core API.

- Agente Backoffice (opcional – futuro)
  - Orquestra pendências (docs/assinatura), gera sugestões e dispara ações sob políticas do Core API.
  - Usa MCPs para acessar serviços (DB, Storage, WhatsApp, Autentique).

## 3) MCPs Recomendados (para IDE e/ou agentes)

MCPs de produtividade na IDE (prontos)
- Git/GitHub: operar branches/PRs, reviews e releases sem sair do editor.
- Files/Search: navegação, grep/rename em larga escala e refactors consistentes.
- HTTP/GraphQL: testar rapidamente endpoints REST/GraphQL (form API, Autentique), salvar queries e variáveis.
- OpenAPI: importar specs e gerar clientes/requests de teste para a Core API.
- GCP: Cloud Run (deploy/rollout/logs), Secret Manager (variáveis), Artifact Registry, Cloud Logging.
- GCS (Storage): listar buckets/objetos, gerar URLs assinadas de teste, validar ACLs para uploads do formulário.
- Postgres: consultar/ajustar schema (leads, documents, signatures, events), rodar migrações e inspeções ad‑hoc.
- Redis: inspecionar contexto de sessão (`lead_ctx:*`), TTLs e chaves de dedupe (`seen_msg:*`).
- Docker: build/tag/push de imagens que vão para o Cloud Run, com cache local.

MCPs específicos do stack (podem ser wrappers leves sobre a Core API)
- WhatsApp (Graph API): enviar templates de lembrete (HSM), checar janela de 24h, simular corpo/failovers.
- Autentique: criar/consultar pedidos de assinatura, puxar status e links; útil para testes ponta a ponta.
- Notifications: orquestrar Cloud Tasks/Scheduler (agendamentos e dedupe de lembretes).
- Vagas/Leads (sua API): CRUD e consultas rápidas diretamente do editor (QA e debugging).

MCPs para agentes (executados pelo backoffice/assistentes)
- `whatsapp-mcp`
  - Tools: `send_template(lead_id, template_id, vars)`, `check_window(phone)`, `send_text(phone, text)`.
  - Guardrails: templates fora da janela de 24h somente; idempotência por `dedup_key`.

- `leads-db-mcp`
  - Tools: `get_by_phone(phone)`, `get_by_id(id)`, `upsert_status(id, status, step)`, `create_form_token(id)`.
  - Guardrails: RBAC; toda escrita gera evento de auditoria.

- `storage-mcp`
  - Tools: `signed_url(kind, lead_id, filename, mode=upload|download)`, `list_docs(lead_id)`.
  - Guardrails: URLs com expiração curta; prefixo por lead; antivírus opcional.

- `autentique-mcp`
  - Tools: `create_request(lead_id, template)`, `get_status(request_id)`, `cancel_request(request_id)`.
  - Guardrails: idempotência por `request_key`; callbacks autenticados.

- `vacancies-mcp`
  - Tools: `create/update/delete vacancy`, `list(city, active)`.

- `gcp-mcp` (qualidade de vida em dev)
  - Logs Cloud Run, Secret Manager, Artifact Registry, GCS.

Nota: MCPs chamam a Core API; políticas e validações vivem na Core API. Toda chamada relevante gera um evento em `events`.

## 4) Estado e Contexto entre Conversas

- Redis (transiente/rápido)
  - Chaves: `lead_ctx:{phone}` (JSON com stage, menus, marcadores); TTL `LEAD_TTL_DAYS` (padrão 30 dias).
  - Dedup: `seen_msg:{msg_id}` (TTL curto) para evitar reprocesso de webhooks.

- Postgres (durável/negócio)
  - `leads(id, phone, nome, cidade, email, step, status, owner_id, form_token, last_whatsapp_at, ...)`.
  - `documents`, `signatures`, `vacancies`, `applications`, `notifications`, `events`, `users/roles`, `deliverers` (matriculados).

Resolução: no início de cada mensagem, hidratar o contexto Redis com o estado oficial do Postgres. Sempre que houver avanço de etapa, persistir no DB e refletir no Redis.

## 5) Variáveis de Ambiente (defaults de produção)

- Região/Plataforma: us-central1 / Cloud Run.
- WhatsApp: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `VERIFY_TOKEN`.
- LLM: `GOOGLE_API_KEY`, `AGENT_MODEL` (padrão `gemini-1.5-flash`).
- Sessão: `REDIS_URL=rediss://user:pass@host:port/0`, `LEAD_TTL_DAYS=30`.
- Core/Painel: `DATABASE_URL` (Postgres), `GCS_BUCKET`, `GCS_SIGNER_SA` (service account que assina URLs).
- Formulário: `FORM_URL_BASE=https://<cloud-run-web>`.
- Assinatura: `AUTENTIQUE_TOKEN`.
- Notificações (opcional): `TASKS_QUEUE`, `SCHEDULER_CRON_*`.
- Interno: `INTERNAL_API_TOKEN` (proteger endpoints admin/teste).

## 6) Endpoints (contratos de alto nível)

Core API (FastAPI):
- `POST /api/leads` – cria/atualiza lead; retorna `lead_id` e `form_token`.
- `POST /api/upload/signed-url` – retorna URL assinada GCS (upload/download) por documento.
- `POST /api/signatures` – cria solicitação no Autentique; `GET /api/signatures/{id}` status.
- `POST /api/notifications` – agenda lembretes; `DELETE /api/notifications/{id}` cancela.
- `GET /api/leads` – filtros (data/cidade/status/step/owner); paginação; export CSV.
- `GET /api/leads/{id}` – detalhe com documentos, assinatura, timeline.

Formulário Web (Next.js):
- `GET /f/{token}` – multi-etapas; uploads para GCS via URL assinada; reCAPTCHA/OTP.

Agente WhatsApp (existente):
- Usa Redis e chama Core API para persistir `step/status` e gerar `form_token`.

## 7) Templates WhatsApp (exemplos)

- `form_pendente`: "{nome}, vimos que seu cadastro está quase lá. Conclua aqui: {link_form}"
- `assinatura_enviada`: "{nome}, enviamos seu documento para assinatura: {link_assinatura}"
- `assinatura_pendente`: "{nome}, seu documento aguarda assinatura. Precisa de ajuda?"
- `cadastro_concluido`: "{nome}, cadastro concluído! Em breve o time operacional entrará em contato."

Respeitar janela de 24h: fora da janela, sempre template HSM.

## 8) Guardrails & Boas Práticas

- Idempotência: toda escrita com `idempotency_key` (ex.: `wa:<msg_id>` ou `sig:<lead_id>`).
- RBAC: papéis (admin/ops/auditor) – toda ação crítica checada no Core API.
- Auditoria: inserir evento em `events` para: mudança de status/etapa, envio de notificação, assinatura criada/assinada, download de dossiê.
- PII: mascarar campos sensíveis na UI; downloads com URL assinada e expiração curta.
- Logs: nunca registrar tokens/segredos; correlacionar por `lead_id` e `phone` ofuscado.

## 9) Fluxos de Referência

Captação → Formulário:
1) WhatsApp identifica lead (phone) e chama Core API → `form_token`.
2) Envia link `FORM_URL_BASE/f/{token}` por template.
3) Form conclui; Core atualiza `leads.step=status='FORM_DONE'` e cria checklist de documentos.

Documentos → Assinatura → Operacional:
1) Painel valida documentos (aprova/reprova); Core atualiza `documents.status`.
2) Core cria solicitação no Autentique; webhook marca `signatures.status`.
3) Concluído → cria registro em `deliverers` e agenda handoff para Operacional.

Lembretes (Cloud Tasks/Scheduler):
1) Ao criar lead/form, agenda T+24h/T+48h se `status` pendente.
2) Ao enviar assinatura, agenda lembrete antes do `expires_at`.

## 10) Operacional em Paralelo ao Prod Atual

- Serviços novos em Cloud Run: painel-web e painel-api com seus próprios segredos.
- Feature flag no WhatsApp: `FEATURE_SEND_FORM_LINK` + allowlist para testes.
- Deploys sempre em `us-central1`.

## 11) Quick Checklist (para não esquecer)

- [ ] `REDIS_URL` setado e `/config-check` acusa `redis_url_set=true`.
- [ ] `FORM_URL_BASE` definido no agente de WhatsApp.
- [ ] Templates HSM aprovados no WhatsApp Business.
- [ ] Bucket GCS privado + SA assinante configurado.
- [ ] `DATABASE_URL` e migrações aplicadas (schemas mínimos).
- [ ] Webhooks do Autentique apontando para Core API.
- [ ] RBAC mínimo ativo no painel; `INTERNAL_API_TOKEN` para endpoints sensíveis.

## 12) Anexos

- Modelo de dados mínimo (resumo): ver seção 4.
- Variáveis de ambiente principais: ver seção 5.
- MCPs e ferramentas: ver seção 3.

Fim.

## 13) Melhorias Recomendadas

Produto e UX
- Formulário com salvamento automático de rascunho, retomada por token e etapa de revisão antes do envio.
- Lembretes inteligentes por etapa (form/docs/assinatura) usando templates HSM, respeitando janela de 24h.
- Deduplicação de leads: normalização de telefone + fuzzy por nome/cidade; mesclagem manual no painel.
- Reprovação guiada de documentos: motivos padronizados e instruções de reenvio.
- Dossiê ZIP do entregador: metadados + documentos com URL assinada e expiração.

Dados e Automação
- Tabela `events` para auditoria de todas as ações (quem/quando/o quê) e reconstrução de timeline.
- ETL diário para BigQuery/BI (funil, SLA, conversões, produtividade) e relatórios agendados.
- Regras de SLA derivadas (ex.: form > 48h pendente, assinatura > 72h) com alertas.
- Catálogo de vagas: `vacancies` com slots e filtros por cidade/turno.

Segurança e LGPD
- RBAC (admin/ops/auditor) com checagem por ação crítica (aprovar doc, reenviar assinatura, download).
- PII segura: mascarar RG/PIS/telefone na UI; KMS em bucket GCS; trilha de quem baixou dossiês.
- Retenção/expurgo por tipo de documento e atendimento a requisições LGPD.
- Proteções no formulário: reCAPTCHA + OTP por WhatsApp/SMS.

Confiabilidade e Performance
- Redis global para contexto, `idempotency_key` em toda escrita e dedupe consistente.
- Cloud Run com `min-instances`, `startup-cpu-boost`, `timeout` e `concurrency` ajustados.
- Uploads via URLs assinadas (TTL curto), validação de MIME/tamanho e antivírus opcional em job.
- Templates WhatsApp aprovados previamente (form_pendente, assinatura_enviada, assinatura_pendente, cadastro_concluido).

Observabilidade
- Logs estruturados com `lead_id`, `phone_hash`, `event_kind`, `idempotency_key`.
- Alertas para falhas/latência do Core API, filas de notificações e status de assinatura.
- Sentry/OpenTelemetry para rastrear o fluxo ponta a ponta (WhatsApp → API → Storage → Autentique).

DevEx, CI/CD e Guardrails
- Ambientes dev/stage/prod (us-central1) com segredos isolados; feature flags no agente.
- GitHub Actions: build/test/deploy Cloud Run; revisão de migrações antes de promover.
- Checklists de PR: templates WhatsApp, mudanças de schema e migrações.
- Testes: contratos (REST/GraphQL), e2e do formulário (Playwright) e webhooks Autentique simulados.

MCP – uso prático
- WhatsApp MCP: `send_template` com dedupe e janela; suporte a "dry-run" para pré-visualização.
- Leads DB MCP: `get_by_phone`, `upsert_status`, `create_form_token` com RBAC e auditoria.
- Storage MCP: `signed_url(upload/download)` e `list_docs` com TTL curto e escopos por lead.
- Autentique MCP: `create_request`, `get_status`, `cancel_request`; callbacks autenticados.
- Notifications MCP: agenda/remarca envios (Cloud Tasks) com idempotência.

## 14) Prioridades Iniciais (Shortlist)

- RBAC + `events` + Redis global + templates WhatsApp (HSM).
- Formulário com salvamento de rascunho + uploads seguros (GCS URLs assinadas).
- Painel: lista/filtros de leads, detalhe com checklist de documentos e dossiê ZIP.
- CI/CD (GitHub Actions) e observabilidade mínima (logs estruturados + alertas básicos).
