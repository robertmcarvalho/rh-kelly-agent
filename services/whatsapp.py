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
from fastapi import FastAPI, Request, HTTPException
from typing import List, Dict, Any

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
        return int(request.query_params.get("hub.challenge"))
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
        entry = data["entry"][0]["changes"][0]["value"]
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
        # Encaminha a entrada ao agente
        agent_response = enviar_mensagem_ao_agente(from_number, texto_usuario)
        # Processa e envia de volta via WhatsApp
        processar_resposta_do_agente(from_number, agent_response)
        return {"status": "handled"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)