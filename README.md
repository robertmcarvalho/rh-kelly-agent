# Agente de Recrutamento RH Kelly — Aethera AI

Projeto mantido pela Aethera AI. Este projeto implementa um agente de recrutamento via WhatsApp usando FastAPI, Google ADK (Agent Development Kit) e Google Generative AI (Gemini). O agente conduz o funil inicial de seleção, checa requisitos, aplica questionário DISC e apresenta vagas, integrando com Google Sheets e (opcionalmente) Redis para contexto.

## Funcionalidades

- Interação via WhatsApp: envio de texto, botões e listas (API Oficial da Meta).
- Funil guiado: apresentação inicial, seleção de cidade, verificação de requisitos, DISC e oferta de vagas.
- Integração com planilha: leitura de vagas via Google Sheets (CSV).
- Contexto persistente: suporte a Redis para estado por usuário (opcional).

## Tecnologias

- Google ADK, Google Generative AI (Gemini)
- FastAPI, Uvicorn
- WhatsApp Business API
- Google Sheets (CSV export)
- Redis (opcional)

## Configuração

Defina as variáveis no ambiente ou em um `.env` na raiz do projeto.

Obrigatórias:
- `WHATSAPP_ACCESS_TOKEN`: token permanente da API WhatsApp Business.
- `WHATSAPP_PHONE_NUMBER_ID`: ID do número WhatsApp Business.
- `VERIFY_TOKEN`: segredo para validação do webhook.
- `GOOGLE_API_KEY`: chave da API Generative AI (modo API Key).

Opcionais:
- `REDIS_URL`: URL do Redis para persistência de contexto (`redis://...`).
- `INTERNAL_API_TOKEN`: protege endpoints internos de teste.
- `INTRO_BEFORE_CITY`: `true/false` para exibir roteiro introdutório antes da escolha da cidade (padrão: `true`).

## Execução local

1) Instale dependências:
```bash
pip install -r requirements.txt
```

2) Inicie o serviço WhatsApp (FastAPI):
```bash
python -m services.whatsapp
```

3) Webhook: exponha `http://localhost:8080/webhook` (ex.: `ngrok http 8080`) e configure na Meta.

Rotas úteis:
- `GET /` healthcheck
- `GET /config-check` diagnóstico das variáveis
- `GET /llm-ping` teste rápido do modelo

## Deploy (Cloud Run)

Com `gcloud` autenticado e `project/region` definidos:
```bash
gcloud run deploy rh-kelly-agent \
  --source . \
  --platform managed \
  --region southamerica-east1 \
  --allow-unauthenticated
```
Depois, configure as variáveis de ambiente no serviço (Console ou `gcloud run services update`).

## Observações

- O arquivo `rh_kelly_agent/data/roteiro_intro.json` contém o roteiro de abertura usado no WhatsApp.
- O arquivo de log de interesse foi removido do versionamento e está ignorado no `.gitignore`.
- Endpoints internos de teste podem exigir `INTERNAL_API_TOKEN` quando habilitado.
