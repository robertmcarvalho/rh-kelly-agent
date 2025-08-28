"""
whatsapp.py – Integração entre o agente ADK e a API oficial do WhatsApp.

Este módulo define utilitários para enviar mensagens de texto e menus interativos
(com botões) via WhatsApp Business API, além de um serviço web (FastAPI) que
recebe webhooks de mensagens de usuários, encaminha a entrada ao ADK e envia a
resposta apropriada de volta ao usuário. Ajuste as rotas e lógicas conforme sua
necessidade de negócio.

Para funcionar, defina as seguintes variáveis de ambiente no .env ou no sistema:

WHATSAPP_ACCESS_TOKEN=<seu token permanente do WhatsApp Business>
WHATSAPP_PHONE_NUMBER_ID=<ID do número de telefone WhatsApp Business>
VERIFY_TOKEN=<uma string secreta para a verificação do webhook>
ADK_API_URL=http://localhost:8000/apps/rh_kelly_agent  # URL do api_server do ADK
"""

import os
import requests
from urllib.parse import urlparse
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional

# ---------------------------------------------------------------------------
# Funções utilitárias de envio de mensagem
# ---------------------------------------------------------------------------

def _get_auth_headers() -> Dict[str, str]:
    """Obtém os cabeçalhos de autenticação para a API do WhatsApp."""
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

def send_text_message(destino: str, texto: str) -> None:
    """Envia uma mensagem de texto simples."""
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": destino,
        "type": "text",
        "text": {"body": texto},
    }
    response = requests.post(url, headers=_get_auth_headers(), json=payload)
    response.raise_for_status()

def send_button_message(destino: str, corpo: str, botoes: List[str]) -> None:
    """
    Envia uma mensagem interativa do tipo "button".

    Args:
        destino: telefone do destinatário (formato internacional, ex.: '5511999999999').
        corpo: texto exibido na mensagem (pergunta).
        botoes: lista de rótulos dos botões a serem exibidos. Cada botão recebe um id
                sequencial (opcao_1, opcao_2, etc.) para identificar a resposta.
    """
    phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    buttons_payload = [
        {"type": "reply", "reply": {"id": f"opcao_{i+1}", "title": label}}
        for i, label in enumerate(botoes)
    ]
    payload = {
        "messaging_product": "whatsapp",
        "to": destino,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": corpo},
            "action": {"buttons": buttons_payload},
        },
    }
    response = requests.post(url, headers=_get_auth_headers(), json=payload)
    response.raise_for_status()

# ---------------------------------------------------------------------------
# Funções para comunicação com o ADK via api_server
# ---------------------------------------------------------------------------

from rh_kelly_agent.agent import root_agent

def enviar_mensagem_ao_agente(user_id: str, mensagem: str) -> Dict[str, Any]:
    """
    Envia a entrada do usuário para o agente ADK e retorna a resposta.
    """
    # Processa a mensagem diretamente com o agente
    response = root_agent.process_message(user_id, mensagem)
    return response

def processar_resposta_do_agente(destino: str, resposta: Dict[str, Any]) -> None:
    """
    Processa a resposta do ADK e envia ao usuário via WhatsApp.

    A resposta do ADK pode incluir:
      - 'content': texto simples a ser enviado.
      - 'function_call': chamada de função (caso o agente indique que deve chamar uma ferramenta).
      - 'options': lista de strings sugeridas como botões (customização própria).
    Esta função interpreta esses campos e decide se envia texto ou menu.
    Ajuste conforme o formato das respostas do seu agente.
    """
    # Exemplo simplificado:
    content = resposta.get("content")
    options = resposta.get("options")  # Ex.: lista de strings para botões
    if options:
        # Envia menu com botões
        send_button_message(destino, content or "Selecione uma opção:", options)
    else:
        send_text_message(destino, content or "Desculpe, não consegui entender.")

# ---------------------------------------------------------------------------
# Servidor FastAPI para webhooks
# ---------------------------------------------------------------------------

app = FastAPI()

@app.get("/")
def healthcheck():
    return {"status": "ok"}

@app.get("/webhook")
def verify_webhook(request: Request):
    """
    Endpoint para a verificação do webhook do WhatsApp (conforme documentação da Meta).
    """
    verify_token = os.environ.get("VERIFY_TOKEN")
    if (
        request.query_params.get("hub.mode") == "subscribe"
        and request.query_params.get("hub.verify_token") == verify_token
    ):
        challenge = request.query_params.get("hub.challenge", "")
        return PlainTextResponse(content=str(challenge))
    raise HTTPException(status_code=403, detail="Invalid verification token")

@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Endpoint para receber eventos do WhatsApp.

    - Extrai o número do remetente e a mensagem ou ID do botão.
    - Envia a entrada ao agente ADK.
    - Processa a resposta do agente e envia de volta via WhatsApp.

    Este exemplo suporta mensagens de texto e cliques em botões (reply.id).
    """
    data = await request.json()
    try:
        # A estrutura real do webhook pode variar; ajuste conforme a configuração do Meta.
        entry = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {})
        messages = entry.get("messages")
        if not messages:
            return {"status": "ignored"}
        msg = messages[0]
        from_number = msg["from"]  # telefone do usuário
        # Determina se é uma resposta a botão
        if msg.get("type") == "button":
            # 'button' contém o id do botão, ex.: 'opcao_1'
            payload = msg["button"]["payload"]
            texto_usuario = payload  # mapeie para a string real conforme seu fluxo
        elif msg.get("type") == "text":
            texto_usuario = msg["text"]["body"]
        else:
            texto_usuario = ""

        # Ajuste de compatibilidade com WhatsApp Cloud API (interactive replies)
        # e robustez para o campo 'from'. Recalcula se necessário antes de chamar o agente.
        try:
            from_number = msg.get("from", from_number)
        except Exception:
            from_number = msg.get("from", "")
        try:
            if not texto_usuario and msg.get("type") == "interactive":
                interactive = msg.get("interactive", {})
                itype = interactive.get("type")
                if itype == "button_reply":
                    texto_usuario = (
                        interactive.get("button_reply", {}).get("id")
                        or interactive.get("button_reply", {}).get("title", "")
                    )
                elif itype == "list_reply":
                    texto_usuario = (
                        interactive.get("list_reply", {}).get("id")
                        or interactive.get("list_reply", {}).get("title", "")
                    )
        except Exception:
            pass
        # Encaminha a entrada ao agente com tratamento de falhas
        try:
            agent_response = enviar_mensagem_ao_agente(from_number, texto_usuario)
            # Processa e envia de volta via WhatsApp
            processar_resposta_do_agente(from_number, agent_response)
        except Exception as inner_exc:
            print(f"Agent pipeline error: {inner_exc}")
            # Envia fallback simples para o usuário e segue com 200
            try:
                send_text_message(
                    from_number,
                    "Não consegui processar sua mensagem agora. Tente novamente em instantes.",
                )
            except Exception as send_err:
                print(f"Fallback send error: {send_err}")
        return {"status": "handled"}
    except Exception as exc:
        print(f"Webhook error: {exc}")
        return {"status": "ignored", "error": str(exc)}

# ---------------------------------------------------------------------------
# Endpoint auxiliar para testes de envio de texto
# ---------------------------------------------------------------------------

class SendTextRequest(BaseModel):
    to: str
    text: str


@app.post("/send-text")
def send_text_endpoint(payload: SendTextRequest, authorization: Optional[str] = Header(default=None)):
    """Endpoint opcional para disparar uma mensagem de texto de teste.

    Se a varivel de ambiente INTERNAL_API_TOKEN estiver definida, exige cabealho
    Authorization: Bearer <token>. Caso no esteja, o endpoint fica aberto.
    """
    required_token = os.environ.get("INTERNAL_API_TOKEN")
    if required_token:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = authorization.split(" ", 1)[1]
        if token != required_token:
            raise HTTPException(status_code=403, detail="Invalid token")
    try:
        send_text_message(payload.to, payload.text)
        return {"status": "sent"}
    except requests.HTTPError as http_err:
        # Retorna o corpo de erro da API Meta, se disponvel
        status = getattr(http_err.response, "status_code", 500)
        detail = getattr(http_err.response, "text", str(http_err))
        raise HTTPException(status_code=status, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/config-check")
def config_check():
    """Retorna o status das variáveis de ambiente críticas (sem expor segredos)."""
    wa_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    wa_phone = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    verify = os.environ.get("VERIFY_TOKEN")
    api_key = os.environ.get("GOOGLE_API_KEY")
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")
    redis_url = os.environ.get("REDIS_URL")
    internal_token = os.environ.get("INTERNAL_API_TOKEN")
    port = os.environ.get("PORT")

    phone_is_digits = bool(wa_phone and wa_phone.isdigit())
    redis_parsed = None
    if redis_url:
        try:
            u = urlparse(redis_url)
            redis_parsed = {
                "scheme": u.scheme,
                "host_set": bool(u.hostname),
                "port_set": bool(u.port),
                "has_user": bool(u.username),
                "has_password": bool(u.password),
            }
        except Exception:
            redis_parsed = {"error": "invalid_url"}

    return {
        "status": "ok",
        "whatsapp": {
            "access_token_set": bool(wa_token),
            "phone_number_id_set": bool(wa_phone),
            "phone_number_id_digits": phone_is_digits,
            "verify_token_set": bool(verify),
        },
        "google_genai": {
            "use_vertexai": str(use_vertex).upper() if use_vertex is not None else None,
            "api_key_set": bool(api_key),
        },
        "redis": {
            "redis_url_set": bool(redis_url),
            "parsed": redis_parsed,
        },
        "internal_api": {
            "internal_api_token_set": bool(internal_token),
        },
        "runtime": {
            "port": port,
        },
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
